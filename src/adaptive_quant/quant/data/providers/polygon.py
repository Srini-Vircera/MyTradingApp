"""Polygon.io (now also branded "Massive") REST adapter.

Endpoints
    ``GET /v2/aggs/ticker/{ticker}/range/{mult}/{span}/{from}/{to}``  (bars)
    ``GET /v3/reference/splits``     (splits)
    ``GET /v3/reference/dividends``  (cash distributions)

Notes
    * Authentication uses the ``Authorization: Bearer`` header, so the key is
      never embedded in URLs (``next_url`` pages are followed with the header).
    * ``adjusted=false`` gives raw prices, ``adjusted=true`` split-adjusted
      prices; Polygon does not dividend-adjust, so ``Adjustment.ALL`` is derived
      locally from raw bars + dividends.
    * Index symbols use Polygon's ticker scheme via ``symbol_map`` (``NDX`` ->
      ``I:NDX``; requires an indices subscription).
    * The base URL is configurable in case the vendor's domain changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pandas as pd
from pydantic import SecretStr

from adaptive_quant.core.errors import DataProviderError
from adaptive_quant.quant.data.bars import Adjustment, Frequency, empty_bars, make_bars
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.quant.data.corporate_actions import ActionType, CorporateAction
from adaptive_quant.quant.data.normalize import (
    daily_bar_ends,
    intraday_bar_ends,
    market_date,
    regular_session_only,
    restrict_dates,
)
from adaptive_quant.quant.data.providers.base import (
    BarRequest,
    MarketDataProvider,
    ProviderCapabilities,
)
from adaptive_quant.quant.data.providers.http import JsonHttpClient

_SPANS = {
    Frequency.DAILY: (1, "day"),
    Frequency.MINUTE_1: (1, "minute"),
    Frequency.MINUTE_5: (5, "minute"),
    Frequency.MINUTE_15: (15, "minute"),
    Frequency.MINUTE_30: (30, "minute"),
}
MAX_PAGES = 1000


class PolygonDataProvider(MarketDataProvider):
    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str,
        timeout_seconds: float,
        calendar: TradingCalendar,
        symbol_map: Mapping[str, str] | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._calendar = calendar
        self._symbol_map = dict(symbol_map or {})
        self._http = JsonHttpClient(
            provider="polygon",
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key.get_secret_value()}",
                "Accept": "application/json",
            },
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            transport=transport,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="polygon",
            frequencies=frozenset(_SPANS),
            adjustments=frozenset({Adjustment.RAW, Adjustment.SPLIT}),
            corporate_actions=True,
            notes=("dividend adjustment derived locally",),
        )

    def ticker(self, symbol: str) -> str:
        return self._symbol_map.get(symbol, symbol)

    # ------------------------------------------------------------ bars
    def fetch_bars(self, request: BarRequest) -> pd.DataFrame:
        if not self.supports(request):
            raise DataProviderError(
                f"polygon cannot serve {request.frequency}/{request.adjustment} bars",
                hint="request raw bars; total-return adjustment is derived locally",
            )
        mult, span = _SPANS[request.frequency]
        path = (
            f"/v2/aggs/ticker/{self.ticker(request.symbol)}/range/{mult}/{span}/"
            f"{request.start.isoformat()}/{request.end.isoformat()}"
        )
        params: dict[str, str | int] = {
            "adjusted": "true" if request.adjustment is Adjustment.SPLIT else "false",
            "sort": "asc",
            "limit": 50_000,
        }
        rows: list[dict[str, Any]] = []
        for page in self._pages(path, params):
            status = page.get("status")
            if status not in ("OK", "DELAYED"):
                raise DataProviderError(f"polygon: unexpected status {status!r}")
            results = page.get("results") or []
            if not isinstance(results, list):
                raise DataProviderError("polygon: 'results' is not a list")
            rows.extend(results)
        if not rows:
            return empty_bars()
        try:
            starts = [datetime.fromtimestamp(int(r["t"]) / 1000, tz=UTC) for r in rows]
            if request.frequency is Frequency.DAILY:
                index = daily_bar_ends([market_date(t) for t in starts], self._calendar)
            else:
                index = intraday_bar_ends(starts, request.frequency)
            frame = make_bars(
                index,
                open=[r["o"] for r in rows],
                high=[r["h"] for r in rows],
                low=[r["l"] for r in rows],
                close=[r["c"] for r in rows],
                volume=[r.get("v", 0.0) for r in rows],
                vwap=[r.get("vw", float("nan")) for r in rows],
                trade_count=[r.get("n", float("nan")) for r in rows],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataProviderError(f"polygon: malformed aggregate payload ({exc})") from exc
        if request.frequency.is_intraday:
            frame = regular_session_only(frame, self._calendar)
        return restrict_dates(frame, request.start, request.end)

    # ------------------------------------------------------------ corporate actions
    def fetch_corporate_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        ticker = self.ticker(symbol)
        actions: list[CorporateAction] = []
        try:
            for page in self._pages(
                "/v3/reference/splits",
                {
                    "ticker": ticker,
                    "execution_date.gte": start.isoformat(),
                    "execution_date.lte": end.isoformat(),
                    "limit": 1000,
                },
            ):
                for item in page.get("results") or []:
                    if item.get("ticker", ticker) != ticker:
                        continue  # never apply another ticker's action to ours
                    actions.append(
                        CorporateAction(
                            symbol=symbol,
                            ex_date=date.fromisoformat(item["execution_date"]),
                            type=ActionType.SPLIT,
                            ratio=float(item["split_to"]) / float(item["split_from"]),
                            source="polygon",
                        )
                    )
            for page in self._pages(
                "/v3/reference/dividends",
                {
                    "ticker": ticker,
                    "ex_dividend_date.gte": start.isoformat(),
                    "ex_dividend_date.lte": end.isoformat(),
                    "limit": 1000,
                },
            ):
                for item in page.get("results") or []:
                    amount = float(item["cash_amount"])
                    if amount <= 0 or item.get("ticker", ticker) != ticker:
                        continue
                    actions.append(
                        CorporateAction(
                            symbol=symbol,
                            ex_date=date.fromisoformat(item["ex_dividend_date"]),
                            type=ActionType.CASH_DIVIDEND,
                            amount=amount,
                            source="polygon",
                        )
                    )
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise DataProviderError(f"polygon: malformed corporate action ({exc})") from exc
        return actions

    # ------------------------------------------------------------ helpers
    def _pages(self, path: str, params: dict[str, str | int]) -> list[dict[str, Any]]:
        pages = [self._http.get_json(path, params)]
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            next_url = pages[-1].get("next_url")
            if not next_url:
                return pages
            if next_url in seen:
                raise DataProviderError("polygon: pagination URL repeated; aborting")
            seen.add(next_url)
            pages.append(self._http.get_json(str(next_url)))
        raise DataProviderError(f"polygon: more than {MAX_PAGES} pages; narrow the request")

    def close(self) -> None:
        self._http.close()
