"""Staleness: is the newest stored bar recent enough to trade on *now*?

Daily data
    The expected newest bar is the last session whose close is at or before
    ``as_of``. Data is stale when more than ``max_age_sessions`` sessions have
    closed after the newest bar. Weekends, holidays and early closes are handled
    by the calendar, so Monday morning is not "stale" because of Saturday.

Intraday data
    While the market is open, the newest bar must end within
    ``max_age_seconds`` of ``as_of``. When the market is closed, the newest bar
    must reach the last session close (within the same tolerance).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from adaptive_quant.core.clock import ensure_utc
from adaptive_quant.quant.data.calendar import TradingCalendar


@dataclass(frozen=True)
class FreshnessResult:
    subject: str
    fresh: bool
    newest: datetime | None
    detail: str


def check_daily_freshness(
    bars: pd.DataFrame,
    *,
    subject: str,
    as_of: datetime,
    calendar: TradingCalendar,
    max_age_sessions: int,
) -> FreshnessResult:
    as_of = ensure_utc(as_of)
    if bars.empty:
        return FreshnessResult(subject, False, None, "no bars")
    newest: datetime = bars.index.max().to_pydatetime()
    if newest > as_of:
        return FreshnessResult(
            subject, False, newest, "newest bar is in the future (clock or data error)"
        )
    expected = calendar.last_completed_session(as_of)
    if expected is None:
        return FreshnessResult(subject, True, newest, "no completed session yet")
    age = calendar.sessions_between(newest, as_of)
    if age > max_age_sessions:
        return FreshnessResult(
            subject,
            False,
            newest,
            f"newest bar {newest:%Y-%m-%d} is {age} session(s) old; last completed session "
            f"is {expected.date} (limit {max_age_sessions})",
        )
    return FreshnessResult(
        subject, True, newest, f"newest bar {newest:%Y-%m-%d}, age {age} session(s)"
    )


def check_intraday_freshness(
    bars: pd.DataFrame,
    *,
    subject: str,
    as_of: datetime,
    calendar: TradingCalendar,
    max_age_seconds: int,
) -> FreshnessResult:
    as_of = ensure_utc(as_of)
    if bars.empty:
        return FreshnessResult(subject, False, None, "no bars")
    newest: datetime = bars.index.max().to_pydatetime()
    if newest > as_of:
        return FreshnessResult(
            subject, False, newest, "newest bar is in the future (clock or data error)"
        )
    tolerance = timedelta(seconds=max_age_seconds)
    session = calendar.current_session(as_of)
    if session is not None:
        reference = as_of
        if as_of - session.open < tolerance:
            return FreshnessResult(subject, True, newest, "session just opened")
    else:
        last = calendar.last_completed_session(as_of)
        if last is None:
            return FreshnessResult(subject, True, newest, "no completed session yet")
        reference = last.close
    lag = reference - newest
    if lag > tolerance:
        return FreshnessResult(
            subject,
            False,
            newest,
            f"newest bar ends {newest:%Y-%m-%d %H:%M}Z, {int(lag.total_seconds())}s behind "
            f"(limit {max_age_seconds}s)",
        )
    return FreshnessResult(
        subject, True, newest, f"lag {int(max(lag, timedelta(0)).total_seconds())}s"
    )
