"""Time handling.

Rules enforced here:

* Internally every timestamp is timezone-aware UTC.
* US/Eastern (exchange time) is used only at boundaries, via :func:`to_market_time`.
* Code never calls ``datetime.now()`` directly; it receives a :class:`Clock`
  so tests (and backtests) control time deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")


class Clock(Protocol):
    """Source of the current time (always UTC)."""

    def now(self) -> datetime: ...


class SystemClock:
    """Wall-clock time in UTC."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FrozenClock:
    """A controllable clock for tests and simulations."""

    def __init__(self, at: datetime) -> None:
        self._now = ensure_utc(at)

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        self._now = ensure_utc(at)

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("FrozenClock cannot move backwards")
        self._now = self._now + delta


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` converted to UTC. Naive datetimes are rejected, not guessed."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(
            f"naive datetime {dt!r} is not allowed; attach a timezone "
            "(internally the platform uses UTC everywhere)"
        )
    return dt.astimezone(UTC)


def to_market_time(dt: datetime) -> datetime:
    """Convert an aware datetime to US/Eastern for display or exchange boundaries."""
    return ensure_utc(dt).astimezone(MARKET_TZ)
