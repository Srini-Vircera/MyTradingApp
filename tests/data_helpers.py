"""Deterministic synthetic market data for tests (never real vendor data)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from adaptive_quant.quant.data.bars import Frequency, make_bars
from adaptive_quant.quant.data.calendar import TradingCalendar, nyse_calendar


def market_dates(df: pd.DataFrame) -> np.ndarray:
    """Exchange-local session dates of each row (as an array of ``date``)."""
    return np.asarray(pd.DatetimeIndex(df.index).tz_convert("America/New_York").date)


def calendar() -> TradingCalendar:
    return nyse_calendar()


def daily_bars(
    start: date,
    end: date,
    *,
    start_price: float = 100.0,
    returns: list[float] | np.ndarray | None = None,
    seed: int = 7,
    vol: float = 0.01,
) -> pd.DataFrame:
    """Canonical daily bars on real NYSE sessions with internally consistent OHLC."""
    sessions = calendar().sessions_in_range(start, end)
    n = len(sessions)
    rng = np.random.default_rng(seed)
    r = np.asarray(returns, dtype=float) if returns is not None else rng.normal(0.0003, vol, n)
    if len(r) != n:
        raise ValueError(f"need {n} returns, got {len(r)}")
    close = start_price * np.cumprod(1.0 + r)
    prev = np.concatenate([[start_price], close[:-1]])
    opens = prev * (1.0 + rng.normal(0, vol / 4, n))
    wiggle = np.abs(rng.normal(0, vol / 2, n))
    high = np.maximum(opens, close) * (1.0 + wiggle)
    low = np.minimum(opens, close) * (1.0 - wiggle)
    return make_bars(
        [s.close for s in sessions],
        open=opens.tolist(),
        high=high.tolist(),
        low=low.tolist(),
        close=close.tolist(),
        volume=(1_000_000 + rng.integers(0, 1000, n)).astype(float).tolist(),
    )


def intraday_bars(day: date, frequency: Frequency, price: float = 100.0) -> pd.DataFrame:
    """A full regular session of intraday bars (bar end stamps)."""
    session = calendar().session(day)
    if session is None:
        raise ValueError(f"{day} is not a session")
    step = frequency.duration
    ends = list(pd.date_range(session.open + step, session.close, freq=step))
    n = len(ends)
    closes = price * (1.0 + 0.0001 * np.arange(n))
    return make_bars(
        ends,
        open=closes.tolist(),
        high=(closes * 1.001).tolist(),
        low=(closes * 0.999).tolist(),
        close=closes.tolist(),
        volume=[1000.0] * n,
    )
