"""CSV / Parquet historical datasets on disk.

File naming inside ``directory``:

* daily bars:      ``<SYMBOL>.parquet`` or ``<SYMBOL>.csv``
* intraday bars:   ``<SYMBOL>_<frequency>.parquet|csv``  (e.g. ``QQQ_5min.csv``)
* corporate actions (optional): ``<SYMBOL>_actions.csv`` with columns
  ``ex_date,type,ratio,amount`` (type = ``split`` | ``cash_dividend``)

Column names are matched case-insensitively with common aliases
(``date``/``timestamp``/``datetime``; ``o``/``open``; ``v``/``volume`` ...).

Daily rows are identified by their date. Intraday rows need timezone-aware
timestamps of the bar **start** (``2024-01-02T14:30:00Z``); naive timestamps are
rejected rather than guessed. The files' adjustment basis is declared in config
(``data.file_import.adjustment``); only ``raw`` files allow local derivation of
adjusted series from corporate actions.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from adaptive_quant.core.errors import CorporateActionsUnavailable, DataProviderError
from adaptive_quant.quant.data.bars import Adjustment, Frequency, empty_bars, make_bars
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.quant.data.corporate_actions import ActionType, CorporateAction
from adaptive_quant.quant.data.normalize import (
    daily_bar_ends,
    intraday_bar_ends,
    regular_session_only,
    restrict_dates,
)
from adaptive_quant.quant.data.providers.base import (
    BarRequest,
    MarketDataProvider,
    ProviderCapabilities,
)

_ALIASES: dict[str, tuple[str, ...]] = {
    "time": ("timestamp", "datetime", "date", "time", "t"),
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "volume": ("volume", "vol", "v"),
    "vwap": ("vwap", "vw"),
}


class FileDataProvider(MarketDataProvider):
    def __init__(self, directory: Path, adjustment: Adjustment, calendar: TradingCalendar) -> None:
        self.directory = directory
        self.adjustment = adjustment
        self._calendar = calendar

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="file",
            frequencies=frozenset(Frequency),
            adjustments=frozenset({self.adjustment}),
            corporate_actions=True,
            notes=(f"directory={self.directory}", f"files are {self.adjustment}"),
        )

    def fetch_bars(self, request: BarRequest) -> pd.DataFrame:
        if request.adjustment is not self.adjustment:
            raise DataProviderError(
                f"file data is {self.adjustment}, request asked for {request.adjustment}"
            )
        path = self._find(request.symbol, request.frequency)
        raw = _read_table(path)
        cols = _resolve_columns(raw, path)
        try:
            if request.frequency is Frequency.DAILY:
                dates = pd.to_datetime(raw[cols["time"]]).dt.date
                index = daily_bar_ends(dates, self._calendar)
            else:
                starts = pd.to_datetime(raw[cols["time"]])
                if starts.dt.tz is None:
                    raise DataProviderError(
                        f"{path.name}: intraday timestamps are timezone-naive",
                        hint="write timestamps with an explicit offset, e.g. 2024-01-02T14:30:00Z",
                    )
                index = intraday_bar_ends(starts.dt.tz_convert("UTC"), request.frequency)
            frame = make_bars(
                index,
                open=raw[cols["open"]].tolist(),
                high=raw[cols["high"]].tolist(),
                low=raw[cols["low"]].tolist(),
                close=raw[cols["close"]].tolist(),
                volume=raw[cols["volume"]].tolist(),
                vwap=raw[cols["vwap"]].tolist() if "vwap" in cols else None,
            )
        except (TypeError, ValueError) as exc:
            raise DataProviderError(f"{path.name}: cannot parse bars ({exc})") from exc
        if frame.empty:
            return empty_bars()
        if request.frequency.is_intraday:
            frame = regular_session_only(frame, self._calendar)
        return restrict_dates(frame, request.start, request.end)

    def fetch_corporate_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        path = self.directory / f"{symbol}_actions.csv"
        if not path.exists():
            raise CorporateActionsUnavailable(
                f"no corporate-actions file for {symbol} ({path.name})",
                hint="add it if the raw prices span splits or dividends",
            )
        df = pd.read_csv(path)
        missing = {"ex_date", "type"} - set(df.columns)
        if missing:
            raise DataProviderError(f"{path.name}: missing columns {sorted(missing)}")
        actions = []
        try:
            for row in df.to_dict("records"):
                kind = ActionType(str(row["type"]).strip().lower())
                ratio = float(row["ratio"]) if kind is ActionType.SPLIT else None
                amount = float(row["amount"]) if kind is ActionType.CASH_DIVIDEND else None
                actions.append(
                    CorporateAction(
                        symbol=symbol,
                        ex_date=date.fromisoformat(str(row["ex_date"])),
                        type=kind,
                        ratio=ratio,
                        amount=amount,
                        source="file",
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataProviderError(f"{path.name}: invalid corporate action ({exc})") from exc
        return [a for a in actions if start <= a.ex_date <= end]

    def _find(self, symbol: str, frequency: Frequency) -> Path:
        stem = symbol if frequency is Frequency.DAILY else f"{symbol}_{frequency.value}"
        for suffix in (".parquet", ".csv"):
            path = self.directory / f"{stem}{suffix}"
            if path.exists():
                return path
        raise DataProviderError(
            f"no file for {symbol} {frequency} in {self.directory}",
            hint=f"expected {stem}.parquet or {stem}.csv",
        )


def _read_table(path: Path) -> pd.DataFrame:
    try:
        if path.suffix == ".parquet":
            return pd.read_parquet(path)
        return pd.read_csv(path)
    except (OSError, ValueError) as exc:
        raise DataProviderError(f"cannot read {path}: {exc}") from exc


def _resolve_columns(df: pd.DataFrame, path: Path) -> dict[str, str]:
    lower = {str(c).strip().lower(): str(c) for c in df.columns}
    resolved: dict[str, str] = {}
    for canonical, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in lower:
                resolved[canonical] = lower[alias]
                break
    missing = [c for c in ("time", "open", "high", "low", "close", "volume") if c not in resolved]
    if missing:
        raise DataProviderError(
            f"{path.name}: cannot find columns {missing}",
            hint="expected date/timestamp, open, high, low, close, volume",
        )
    if "adj close" in lower or "adj_close" in lower:
        raise DataProviderError(
            f"{path.name}: has an 'adj close' column; export raw OHLCV only",
            hint="mixed raw/adjusted columns are ambiguous; adjustments are derived locally",
        )
    return resolved
