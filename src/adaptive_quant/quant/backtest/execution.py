"""Execution timing: *when* a decision is made and *when/where* it fills.

======================  ===============================  ==========================
model                   decision time (data visible)     fill
======================  ===============================  ==========================
``near_close``          close(t) - N min  (with daily     close(t + delay)
                        data: bars through close(t-1))
``next_open``           close(t)                          open(t + 1 + delay)
``next_close``          close(t)                          close(t + 1 + delay)
``closing_auction``     close(t)                          close(t + delay)
======================  ===============================  ==========================

Only ``closing_auction`` with zero delay fills at the very close whose data
produced the signal. It must be chosen explicitly and is flagged in every
result, because it assumes participation in the closing auction at a price
not yet known when the order is sent. The engine additionally asserts, for
every fill, that ``fill_time > data_timestamp`` unless this model is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from adaptive_quant.quant.data.calendar import Session


class ExecutionTiming(StrEnum):
    NEAR_CLOSE = "near_close"
    NEXT_OPEN = "next_open"
    NEXT_CLOSE = "next_close"
    CLOSING_AUCTION = "closing_auction"


class FillPoint(StrEnum):
    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True)
class Schedule:
    decision_time: datetime
    fill_offset: int  # sessions after the decision session
    fill_point: FillPoint


def schedule(
    timing: ExecutionTiming, session: Session, delay: int, near_close_minutes: int
) -> Schedule:
    if delay < 0:
        raise ValueError("execution delay cannot be negative")
    if timing is ExecutionTiming.NEAR_CLOSE:
        decision = session.close - timedelta(minutes=near_close_minutes)
        if decision <= session.open:
            raise ValueError("near_close_minutes is longer than the session")
        return Schedule(decision, delay, FillPoint.CLOSE)
    if timing is ExecutionTiming.NEXT_OPEN:
        return Schedule(session.close, 1 + delay, FillPoint.OPEN)
    if timing is ExecutionTiming.NEXT_CLOSE:
        return Schedule(session.close, 1 + delay, FillPoint.CLOSE)
    return Schedule(session.close, delay, FillPoint.CLOSE)


def fill_time(session: Session, point: FillPoint) -> datetime:
    return session.open if point is FillPoint.OPEN else session.close


def allows_same_bar_close(timing: ExecutionTiming, delay: int) -> bool:
    """True only for the explicit closing-auction model without delay."""
    return timing is ExecutionTiming.CLOSING_AUCTION and delay == 0
