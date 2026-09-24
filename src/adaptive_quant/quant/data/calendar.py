"""Trading calendar.

Every date/time question the platform asks about the market ("is today a
session?", "when does it close?", "what was the last completed session at
15:45?") goes through :class:`TradingCalendar`, so weekends, holidays, special
closures and early closes are handled in one place.

The NYSE calendar is built from the ``exchange_calendars`` project (XNYS),
which tracks holidays, early closes and one-off closures (e.g. 2001-09-11,
2012 Hurricane Sandy, national days of mourning). Queries outside the
calendar's coverage raise :class:`CalendarError` instead of guessing.
"""

from __future__ import annotations

import bisect
import functools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from itertools import pairwise

from adaptive_quant.core.clock import MARKET_TZ, ensure_utc
from adaptive_quant.core.errors import CalendarError

REGULAR_CLOSE_LOCAL = time(16, 0)
REGULAR_OPEN_LOCAL = time(9, 30)
NYSE_COVERAGE_START = date(1990, 1, 2)


@dataclass(frozen=True, slots=True)
class Session:
    """One trading session. ``open``/``close`` are UTC."""

    date: date
    open: datetime
    close: datetime

    @property
    def early_close(self) -> bool:
        return self.close.astimezone(MARKET_TZ).time() < REGULAR_CLOSE_LOCAL


class TradingCalendar:
    """A calendar backed by an explicit, sorted list of sessions."""

    def __init__(self, name: str, sessions: Sequence[Session]) -> None:
        if not sessions:
            raise CalendarError(f"calendar {name!r} has no sessions")
        dates = [s.date for s in sessions]
        if any(b <= a for a, b in pairwise(dates)):
            raise CalendarError(f"calendar {name!r} sessions must be strictly increasing")
        self.name = name
        self._sessions = tuple(sessions)
        self._dates = tuple(dates)
        self._closes = tuple(s.close for s in sessions)
        self._by_date = {s.date: s for s in sessions}

    # ------------------------------------------------------------ coverage
    @property
    def first_date(self) -> date:
        return self._dates[0]

    @property
    def last_date(self) -> date:
        return self._dates[-1]

    def _check(self, d: date) -> None:
        if not self.first_date <= d <= self.last_date:
            raise CalendarError(
                f"{d} is outside the {self.name} calendar coverage "
                f"({self.first_date} .. {self.last_date})",
                hint="extend the calendar range or update the exchange_calendars package",
            )

    # ------------------------------------------------------------ queries
    def is_session(self, d: date) -> bool:
        self._check(d)
        return d in self._by_date

    def session(self, d: date) -> Session | None:
        self._check(d)
        return self._by_date.get(d)

    def sessions_in_range(self, start: date, end: date) -> list[Session]:
        """Sessions with ``start <= date <= end`` (both inclusive)."""
        if end < start:
            return []
        self._check(start)
        self._check(end)
        lo = bisect.bisect_left(self._dates, start)
        hi = bisect.bisect_right(self._dates, end)
        return list(self._sessions[lo:hi])

    def previous_session(self, d: date) -> Session:
        """The last session strictly before ``d``."""
        self._check(d)
        i = bisect.bisect_left(self._dates, d)
        if i == 0:
            raise CalendarError(f"no session before {d} in the {self.name} calendar")
        return self._sessions[i - 1]

    def next_session(self, d: date) -> Session:
        """The first session strictly after ``d``."""
        self._check(d)
        i = bisect.bisect_right(self._dates, d)
        if i >= len(self._sessions):
            raise CalendarError(f"no session after {d} in the {self.name} calendar")
        return self._sessions[i]

    def last_completed_session(self, as_of: datetime) -> Session | None:
        """The most recent session whose close is at or before ``as_of``."""
        at = ensure_utc(as_of)
        self._check(at.astimezone(MARKET_TZ).date())
        i = bisect.bisect_right(self._closes, at)
        return self._sessions[i - 1] if i else None

    def current_session(self, at: datetime) -> Session | None:
        """The session in progress at ``at`` (open <= at < close), if any."""
        at = ensure_utc(at)
        s = self.session(at.astimezone(MARKET_TZ).date())
        if s is not None and s.open <= at < s.close:
            return s
        return None

    def is_open(self, at: datetime) -> bool:
        return self.current_session(at) is not None

    def sessions_between(self, after: datetime, until: datetime) -> int:
        """Number of sessions whose close is in ``(after, until]``."""
        a, u = ensure_utc(after), ensure_utc(until)
        return max(0, bisect.bisect_right(self._closes, u) - bisect.bisect_right(self._closes, a))

    def nominal_close(self, d: date) -> datetime:
        """Session close for ``d``, or the *regular* 16:00 ET close if ``d`` is not a session.

        Used to stamp provider rows; non-session rows are then flagged by validation.
        """
        s = self._by_date.get(d)
        if s is not None:
            return s.close
        return datetime.combine(d, REGULAR_CLOSE_LOCAL, tzinfo=MARKET_TZ).astimezone(UTC)


@functools.lru_cache(maxsize=4)
def nyse_calendar(start: date = NYSE_COVERAGE_START, end: date | None = None) -> TradingCalendar:
    """NYSE sessions from ``exchange_calendars`` (default: 1990 to ~1 year ahead)."""
    import exchange_calendars as xcals  # heavy import, kept local

    kwargs: dict[str, str] = {"start": start.isoformat()}
    if end is not None:
        kwargs["end"] = end.isoformat()
    cal = xcals.get_calendar("XNYS", **kwargs)
    schedule = cal.schedule
    sessions = [
        Session(
            date=label.date(),
            open=opened.to_pydatetime().astimezone(UTC),
            close=closed.to_pydatetime().astimezone(UTC),
        )
        for label, opened, closed in zip(
            schedule.index, schedule["open"], schedule["close"], strict=True
        )
    ]
    return TradingCalendar("XNYS", sessions)


def session_date(ts: datetime) -> date:
    """The exchange-local calendar date of a UTC timestamp."""
    return ensure_utc(ts).astimezone(MARKET_TZ).date()
