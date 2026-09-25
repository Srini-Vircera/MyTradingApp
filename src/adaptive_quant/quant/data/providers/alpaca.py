"""Alpaca Market Data API v2 adapter (bars) and v1 corporate actions.

Endpoints
    ``GET /v2/stocks/{symbol}/bars``  (timeframe, start, end, adjustment, feed, page_token)
    ``GET /v1/corporate-actions``      (symbols, types, start, end, page_token)

Notes
    * Daily bars are stamped by Alpaca at 00:00 America/New_York of the session
      date; intraday bars at their start. Both are converted to canonical bar
      *end* times; pre/post-market intraday bars are dropped.
    * The free ``iex`` feed reports IEX-only volume (a small fraction of the
      consolidated tape). Prices are fine for daily signals; use ``sip`` for
      consolidated volume if your subscription allows it.
    * Index data (NDX) is not available from Alpaca.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pandas as pd
from pydantic import SecretStr

from adaptive_quant.config.schema import AlpacaConfig
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

_TIMEFRAMES = {
    Frequency.DAILY: "1Day",
    Frequency.MINUTE_1: "1Min",
    Frequency.MINUTE_5: "5Min",
    Frequency.MINUTE_15: "15Min",
    Frequency.MINUTE_30: "30Min",
}
MAX_PAGES = 1000
PAGE_LIMIT = 10_000


class AlpacaDataProvider(MarketDataProvider):
    def __init__(
        self,
        *,
        key_id: SecretStr,
        secret_key: SecretStr,
        config: AlpacaConfig,
        calendar: TradingCalendar,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._calendar = calendar
        self._http = JsonHttpClient(
            provider="alpaca",
            base_url=config.data_base_url,
            headers={
                "APCA-API-KEY-ID": key_id.get_secret_value(),
                "APCA-API-SECRET-KEY": secret_key.get_secret_value(),
                "Accept": "application/json",
            },
            timeout_seconds=config.request_timeout_seconds,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            transport=transport,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="alpaca",
            frequencies=frozenset(_TIMEFRAMES),
            adjustments=frozenset(Adjustment),
            corporate_actions=True,
            notes=(f"feed={self._config.data_feed}", "no index data"),
        )

    # ------------------------------------------------------------ bars
    def fetch_bars(self, request: BarRequest) -> pd.DataFrame:
        if not self.supports(request):
            raise DataProviderError(f"alpaca cannot serve {request}")
        params: dict[str, str | int] = {
            "timeframe": _TIMEFRAMES[request.frequency],
            "start": request.start.isoformat(),
            # end is exclusive of later stamps; ask for one extra day, trim below
            "end": (request.end + timedelta(days=1)).isoformat(),
            "adjustment": request.adjustment.value,
            "feed": self._config.data_feed,
            "limit": PAGE_LIMIT,
        }
        rows: list[dict[str, Any]] = []
        for page in self._pages(f"/v2/stocks/{request.symbol}/bars", params):
            bars = page.get("bars") or []
            if not isinstance(bars, list):
                raise DataProviderError("alpaca: 'bars' is not a list")
            rows.extend(bars)
        if not rows:
            return empty_bars()
        try:
            stamps = [_parse_time(r["t"]) for r in rows]
            if request.frequency is Frequency.DAILY:
                index = daily_bar_ends([market_date(t) for t in stamps], self._calendar)
            else:
                index = intraday_bar_ends(stamps, request.frequency)
            frame = make_bars(
                index,
                open=[r["o"] for r in rows],
                high=[r["h"] for r in rows],
                low=[r["l"] for r in rows],
                close=[r["c"] for r in rows],
                volume=[r["v"] for r in rows],
                vwap=[r.get("vw", float("nan")) for r in rows],
                trade_count=[r.get("n", float("nan")) for r in rows],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataProviderError(f"alpaca: malformed bar payload ({exc})") from exc
        if request.frequency.is_intraday:
            frame = regular_session_only(frame, self._calendar)
        return restrict_dates(frame, request.start, request.end)

    # ------------------------------------------------------------ corporate actions
    def fetch_corporate_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        params: dict[str, str | int] = {
            "symbols": symbol,
            "types": "forward_split,reverse_split,cash_dividend",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 1000,
        }
        actions: list[CorporateAction] = []
        for page in self._pages("/v1/corporate-actions", params):
            groups = page.get("corporate_actions") or {}
            if not isinstance(groups, dict):
                raise DataProviderError("alpaca: 'corporate_actions' is not an object")
            try:
                for kind in ("forward_splits", "reverse_splits"):
                    for item in groups.get(kind) or []:
                        if item.get("symbol") != symbol:
                            continue  # never apply another symbol's action to ours
                        actions.append(
                            CorporateAction(
                                symbol=symbol,
                                ex_date=date.fromisoformat(item["ex_date"]),
                                type=ActionType.SPLIT,
                                ratio=float(item["new_rate"]) / float(item["old_rate"]),
                                source="alpaca",
                            )
                        )
                for item in groups.get("cash_dividends") or []:
                    if item.get("symbol") != symbol:
                        continue
                    actions.append(
                        CorporateAction(
                            symbol=symbol,
                            ex_date=date.fromisoformat(item["ex_date"]),
                            type=ActionType.CASH_DIVIDEND,
                            amount=float(item["rate"]),
                            source="alpaca",
                        )
                    )
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                raise DataProviderError(f"alpaca: malformed corporate action ({exc})") from exc
        return [a for a in actions if start <= a.ex_date <= end]

    # ------------------------------------------------------------ helpers
    def _pages(self, path: str, params: dict[str, str | int]) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        seen: set[str] = set()
        token: str | None = None
        for _ in range(MAX_PAGES):
            query = dict(params)
            if token:
                query["page_token"] = token
            page = self._http.get_json(path, query)
            pages.append(page)
            token = page.get("next_page_token")
            if not token:
                return pages
            if token in seen:
                raise DataProviderError("alpaca: pagination token repeated; aborting")
            seen.add(token)
        raise DataProviderError(f"alpaca: more than {MAX_PAGES} pages; narrow the request")

    def close(self) -> None:
        self._http.close()


def _parse_time(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError(f"naive timestamp {value!r}")
    return ts
