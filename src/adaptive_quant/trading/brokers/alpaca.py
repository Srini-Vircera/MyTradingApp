"""Alpaca **paper** trading adapter (Trading API v2).

Safety properties
-----------------
* Paper only: the base URL must be Alpaca's paper endpoint; anything else is
  refused (``SafetyViolation``). Live trading needs a separate, deliberately
  enabled adapter (not part of this version).
* Credentials travel only in headers and never appear in errors or ``repr``.
* Reads (account, positions, orders, clock, assets) are retried on transport
  errors, 429 and 5xx. **Order submission is never retried**: any transport
  failure, timeout or 5xx raises :class:`BrokerError`, and the caller must mark
  the order UNKNOWN and investigate by ``client_order_id``.
* A duplicate ``client_order_id`` (HTTP 422 "client_order_id must be unique")
  raises :class:`DuplicateClientOrderId`; a definitive refusal (403 insufficient
  buying power, 422 validation) raises :class:`OrderRejectedByBroker`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from adaptive_quant.core.clock import Clock, SystemClock
from adaptive_quant.core.enums import OrderSide, OrderState
from adaptive_quant.core.errors import BrokerError, SafetyViolation
from adaptive_quant.core.models import (
    AccountSnapshot,
    MarketClock,
    OrderRequest,
    OrderSnapshot,
    Position,
)
from adaptive_quant.trading.brokers.base import (
    BrokerAdapter,
    BrokerCapabilities,
    DuplicateClientOrderId,
    OrderRejectedByBroker,
    TradableAsset,
)

PAPER_HOST = "paper-api.alpaca.markets"
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

#: Alpaca order status -> internal state
STATUS_MAP: Mapping[str, OrderState] = {
    "pending_new": OrderState.SUBMITTED,
    "accepted": OrderState.ACKNOWLEDGED,
    "new": OrderState.ACKNOWLEDGED,
    "accepted_for_bidding": OrderState.ACKNOWLEDGED,
    "pending_cancel": OrderState.ACKNOWLEDGED,
    "pending_replace": OrderState.ACKNOWLEDGED,
    "calculated": OrderState.ACKNOWLEDGED,
    "stopped": OrderState.ACKNOWLEDGED,
    "suspended": OrderState.ACKNOWLEDGED,
    "done_for_day": OrderState.ACKNOWLEDGED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELLED,
    "expired": OrderState.CANCELLED,
    "replaced": OrderState.CANCELLED,
    "rejected": OrderState.REJECTED,
}


class AlpacaPaperBroker(BrokerAdapter):
    def __init__(
        self,
        *,
        key_id: SecretStr,
        secret_key: SecretStr,
        base_url: str = f"https://{PAPER_HOST}",
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Clock | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != PAPER_HOST:
            raise SafetyViolation(
                f"AlpacaPaperBroker only talks to https://{PAPER_HOST} "
                f"(got host {parsed.hostname!r})",
                hint="live trading is not enabled in this version; see docs/SAFETY.md",
            )
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "APCA-API-KEY-ID": key_id.get_secret_value(),
                "APCA-API-SECRET-KEY": secret_key.get_secret_value(),
            },
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"AlpacaPaperBroker({self._client.base_url})"

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            name="alpaca-paper",
            is_live=False,
            supports_fractional=True,
            supports_client_order_id=True,
            supports_closing_auction=True,
        )

    # ------------------------------------------------------------------ reads
    def get_account(self) -> AccountSnapshot:
        a = self._get("/v2/account")
        return AccountSnapshot(
            as_of=self._clock.now(),
            equity=Decimal(str(a["equity"])),
            cash=Decimal(str(a["cash"])),
            buying_power=Decimal(str(a["buying_power"])),
            is_paper=True,
            trading_blocked=bool(a.get("trading_blocked") or a.get("account_blocked")),
        )

    def get_positions(self) -> list[Position]:
        return [
            Position(
                symbol=p["symbol"],
                quantity=Decimal(str(p["qty"])),
                avg_entry_price=Decimal(str(p["avg_entry_price"])),
                market_value=Decimal(str(p["market_value"])),
            )
            for p in self._get_list("/v2/positions")
        ]

    def get_open_orders(self) -> list[OrderSnapshot]:
        return [
            _snapshot(o) for o in self._get_list("/v2/orders", {"status": "open", "limit": 500})
        ]

    def get_order(self, broker_order_id: str) -> OrderSnapshot:
        return _snapshot(self._get(f"/v2/orders/{broker_order_id}"))

    def get_order_by_client_id(self, client_order_id: str) -> OrderSnapshot | None:
        try:
            return _snapshot(
                self._get("/v2/orders:by_client_order_id", {"client_order_id": client_order_id})
            )
        except _NotFound:
            return None

    def get_market_clock(self) -> MarketClock:
        c = self._get("/v2/clock")
        return MarketClock(
            as_of=datetime.fromisoformat(c["timestamp"]),
            is_open=bool(c["is_open"]),
            next_open=datetime.fromisoformat(c["next_open"]),
            next_close=datetime.fromisoformat(c["next_close"]),
        )

    def get_tradeable_assets(self, symbols: list[str]) -> dict[str, TradableAsset]:
        out = {}
        for sym in symbols:
            try:
                a = self._get(f"/v2/assets/{sym}")
            except _NotFound:
                continue
            out[sym] = TradableAsset(
                symbol=sym,
                tradable=bool(a.get("tradable")) and a.get("status") == "active",
                fractionable=bool(a.get("fractionable")),
                shortable=bool(a.get("shortable")),
            )
        return out

    # ------------------------------------------------------------------ writes
    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        body: dict[str, Any] = {
            "symbol": request.symbol,
            "qty": str(request.quantity),
            "side": request.side.value,
            "type": _ORDER_TYPE[request.order_type.value],
            # on-close order types are expressed by Alpaca as time_in_force "cls"
            "time_in_force": "cls"
            if request.order_type.value.endswith("_on_close")
            else request.time_in_force.value,
            "client_order_id": request.client_order_id,
        }
        if request.limit_price is not None:
            body["limit_price"] = str(request.limit_price)
        try:
            r = self._client.post("/v2/orders", json=body)
        except httpx.TransportError as exc:  # includes timeouts: outcome UNKNOWN
            raise BrokerError(
                f"alpaca: order {request.client_order_id} submission outcome unknown "
                f"({type(exc).__name__})",
                hint="mark UNKNOWN and investigate by client_order_id; never resubmit",
            ) from exc
        if r.status_code == 422 and "client_order_id" in r.text and "unique" in r.text:
            raise DuplicateClientOrderId(f"alpaca already has order {request.client_order_id}")
        if r.status_code in (403, 422):
            raise OrderRejectedByBroker(f"alpaca rejected {request.client_order_id}: {_message(r)}")
        if r.status_code in (401,):
            raise BrokerError(
                "alpaca: authentication failed", hint="check ALPACA_API_KEY_ID / SECRET"
            )
        if r.status_code >= 400:
            raise BrokerError(
                f"alpaca: order {request.client_order_id} submission outcome unknown "
                f"(HTTP {r.status_code})",
                hint="mark UNKNOWN and investigate by client_order_id; never resubmit",
            )
        return _snapshot(r.json())

    def cancel_order(self, broker_order_id: str) -> None:
        try:
            r = self._client.delete(f"/v2/orders/{broker_order_id}")
        except httpx.TransportError as exc:
            raise BrokerError(f"alpaca: cancel of {broker_order_id} outcome unknown") from exc
        if r.status_code >= 400 and r.status_code != 422:  # 422: already terminal
            raise BrokerError(f"alpaca: cancel of {broker_order_id} failed: HTTP {r.status_code}")

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ http
    def _get(self, url: str, params: Mapping[str, str | int] | None = None) -> dict[str, Any]:
        data = self._request(url, params)
        if not isinstance(data, dict):
            raise BrokerError(f"alpaca: expected an object from {url}")
        return data

    def _get_list(
        self, url: str, params: Mapping[str, str | int] | None = None
    ) -> list[dict[str, Any]]:
        data = self._request(url, params)
        if not isinstance(data, list):
            raise BrokerError(f"alpaca: expected a list from {url}")
        return data

    def _request(self, url: str, params: Mapping[str, str | int] | None) -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                r = self._client.get(url, params=dict(params or {}))
            except httpx.TransportError as exc:
                if attempt > self._max_retries:
                    raise BrokerError(f"alpaca: {url} unreachable ({type(exc).__name__})") from exc
                self._sleep(self._backoff * 2 ** (attempt - 1))
                continue
            if r.status_code in _RETRY_STATUS and attempt <= self._max_retries:
                self._sleep(self._backoff * 2 ** (attempt - 1))
                continue
            if r.status_code == 404:
                raise _NotFound(url)
            if r.status_code in (401, 403):
                raise BrokerError(
                    f"alpaca: request rejected (HTTP {r.status_code})",
                    hint="check ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY (paper keys)",
                )
            if r.status_code >= 400:
                raise BrokerError(f"alpaca: {url} failed with HTTP {r.status_code}")
            try:
                return r.json()
            except ValueError as exc:
                raise BrokerError(f"alpaca: {url} returned invalid JSON") from exc


class _NotFound(BrokerError):
    pass


_ORDER_TYPE = {
    "market": "market",
    "limit": "limit",
    "market_on_close": "market",
    "limit_on_close": "limit",
}


def _message(r: httpx.Response) -> str:
    try:
        return str(r.json().get("message", r.text[:200]))
    except ValueError:
        return r.text[:200]


def _snapshot(o: Mapping[str, Any]) -> OrderSnapshot:
    status = str(o.get("status", ""))
    if status not in STATUS_MAP:
        raise BrokerError(f"alpaca: unknown order status {status!r}")
    avg = o.get("filled_avg_price")
    return OrderSnapshot(
        client_order_id=str(o["client_order_id"]),
        broker_order_id=str(o["id"]),
        symbol=str(o["symbol"]),
        side=OrderSide(o["side"]),
        quantity=Decimal(str(o["qty"])),
        filled_quantity=Decimal(str(o.get("filled_qty") or "0")),
        avg_fill_price=None if avg in (None, "") else Decimal(str(avg)),
        state=STATUS_MAP[status],
        updated_at=datetime.fromisoformat(
            str(o.get("updated_at") or o["submitted_at"]).replace("Z", "+00:00")
        ),
        raw={k: o[k] for k in ("status", "type", "time_in_force") if k in o},
    )
