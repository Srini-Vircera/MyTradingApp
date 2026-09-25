"""Point-in-time access to market data.

:class:`MarketDataView` is the *only* way strategies see prices. It exposes
rows whose timestamp (bar **end** time) is ``<= as_of``, which makes
look-ahead structurally impossible: at 15:45 ET today's daily bar (stamped at
16:00) is invisible, and so is every later bar.

Frames are validated once at construction (canonical, sorted, unique). With
pandas copy-on-write, a strategy that modifies a returned frame cannot corrupt
the shared history.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import pandas as pd

from adaptive_quant.core.clock import ensure_utc
from adaptive_quant.core.errors import MissingDataError
from adaptive_quant.quant.data.bars import require_canonical_sorted


class MarketDataView:
    def __init__(self, frames: Mapping[str, pd.DataFrame], as_of: datetime) -> None:
        for symbol, df in frames.items():
            require_canonical_sorted(df, f"{symbol} bars")
        self._frames = dict(frames)
        self._as_of = ensure_utc(as_of)
        self._cut = pd.Timestamp(self._as_of)

    @property
    def as_of(self) -> datetime:
        return self._as_of

    @property
    def symbols(self) -> list[str]:
        return sorted(self._frames)

    def has(self, symbol: str) -> bool:
        return symbol in self._frames and self._visible_count(symbol) > 0

    def at(self, as_of: datetime) -> MarketDataView:
        """A view of the same data at another point in time (cheap; no copies)."""
        view = MarketDataView.__new__(MarketDataView)
        view._frames = self._frames
        view._as_of = ensure_utc(as_of)
        view._cut = pd.Timestamp(view._as_of)
        return view

    def bars(self, symbol: str, lookback: int | None = None) -> pd.DataFrame:
        """Bars known at ``as_of``; the last ``lookback`` rows if given."""
        df = self._frame(symbol)
        n = self._visible_count(symbol)
        start = 0 if lookback is None else max(0, n - lookback)
        return df.iloc[start:n]

    def close(self, symbol: str, lookback: int | None = None) -> pd.Series[float]:
        return self.bars(symbol, lookback)["close"]

    def latest(self, symbol: str) -> pd.Series[float]:
        visible = self.bars(symbol, lookback=1)
        if visible.empty:
            raise MissingDataError(f"no {symbol} bar is known as of {self._as_of:%Y-%m-%d %H:%M}Z")
        return visible.iloc[-1]

    def data_timestamp(self, symbol: str) -> datetime:
        """End time of the most recent visible bar (goes into ``StrategySignal.data_timestamp``)."""
        visible = self.bars(symbol, lookback=1)
        if visible.empty:
            raise MissingDataError(f"no {symbol} bar is known as of {self._as_of:%Y-%m-%d %H:%M}Z")
        ts: pd.Timestamp = visible.index[-1]
        return ts.to_pydatetime()

    def _frame(self, symbol: str) -> pd.DataFrame:
        try:
            return self._frames[symbol]
        except KeyError:
            raise MissingDataError(
                f"no market data loaded for {symbol}", hint="add it to the data request"
            ) from None

    def _visible_count(self, symbol: str) -> int:
        return int(self._frame(symbol).index.searchsorted(self._cut, side="right"))
