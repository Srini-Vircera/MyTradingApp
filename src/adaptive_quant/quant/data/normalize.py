"""Helpers that turn provider rows into canonical, session-aware bar frames."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

import numpy as np
import pandas as pd

from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.quant.data.bars import INDEX_NAME, Frequency
from adaptive_quant.quant.data.calendar import TradingCalendar


def daily_bar_ends(dates: Iterable[date], calendar: TradingCalendar) -> pd.DatetimeIndex:
    """Stamp daily rows at their session close (nominal 16:00 ET for non-sessions,
    which validation then reports as ``non_session``)."""
    return pd.DatetimeIndex([calendar.nominal_close(d) for d in dates], name=INDEX_NAME)


def intraday_bar_ends(starts: Iterable[datetime], frequency: Frequency) -> pd.DatetimeIndex:
    """Providers stamp intraday bars at their *start*; the canonical stamp is the end."""
    step = frequency.duration
    return pd.DatetimeIndex([s + step for s in starts], name=INDEX_NAME)


def market_date(ts: datetime) -> date:
    return ts.astimezone(MARKET_TZ).date()


def regular_session_only(bars: pd.DataFrame, calendar: TradingCalendar) -> pd.DataFrame:
    """Drop intraday bars outside regular trading hours (pre/post-market)."""
    keep = []
    for ts in bars.index:
        session = calendar.session(market_date(ts))
        keep.append(session is not None and session.open < ts <= session.close)
    return bars.loc[np.asarray(keep, dtype=bool)]


def restrict_dates(bars: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Keep rows whose exchange-local date lies in ``[start, end]``."""
    mask = [start <= market_date(ts) <= end for ts in bars.index]
    return bars.loc[np.asarray(mask, dtype=bool)]
