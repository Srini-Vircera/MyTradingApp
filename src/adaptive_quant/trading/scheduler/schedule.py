"""Session schedule: steps at *minutes before the session close*.

Weekends and holidays have no steps (no cycle at all); early-close days shift
every step automatically because offsets are relative to the actual close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from adaptive_quant.config.schema import ScheduleConfig
from adaptive_quant.core.clock import MARKET_TZ, ensure_utc
from adaptive_quant.quant.data.calendar import Session, TradingCalendar

#: Steps the trading cycle implements, in order.
CYCLE_STEPS = (
    "health_check",
    "market_data_update",
    "indicators",
    "strategies",
    "risk",
    "portfolio_target",
    "order_submission",
    "fill_monitoring",
    "reconciliation",
)


@dataclass(frozen=True)
class ScheduledStep:
    name: str
    at: datetime  # UTC


@dataclass(frozen=True)
class SessionPlan:
    session: Session
    steps: tuple[ScheduledStep, ...]
    order_cutoff: datetime  # no new orders at or after this time

    @property
    def date(self) -> date:
        return self.session.date


def validate_schedule(cfg: ScheduleConfig) -> None:
    names = [s.name for s in cfg.steps]
    if tuple(names) != CYCLE_STEPS:
        raise ValueError(
            f"schedule steps must be exactly {list(CYCLE_STEPS)} in that order, got {names}"
        )
    submit = next(s for s in cfg.steps if s.name == "order_submission")
    if submit.minutes_before_close <= cfg.order_cutoff:
        raise ValueError("order_submission must be scheduled before the no-new-orders cutoff")


def plan_for(d: date, calendar: TradingCalendar, cfg: ScheduleConfig) -> SessionPlan | None:
    """The session's steps, or ``None`` when ``d`` is not a trading day."""
    session = calendar.session(d)
    if session is None:
        return None
    steps = tuple(
        ScheduledStep(s.name, session.close - timedelta(minutes=s.minutes_before_close))
        for s in cfg.steps
    )
    return SessionPlan(session, steps, session.close - timedelta(minutes=cfg.order_cutoff))


def plan_at(now: datetime, calendar: TradingCalendar, cfg: ScheduleConfig) -> SessionPlan | None:
    return plan_for(ensure_utc(now).astimezone(MARKET_TZ).date(), calendar, cfg)


def next_step_time(
    now: datetime, calendar: TradingCalendar, cfg: ScheduleConfig, done: set[str]
) -> datetime:
    """When the scheduler should wake next: the first undone step today, else the
    first step of the next session."""
    now = ensure_utc(now)
    today = plan_at(now, calendar, cfg)
    if today is not None:
        pending = [s.at for s in today.steps if s.name not in done]
        if pending:
            return max(min(pending), now)
    d = now.astimezone(MARKET_TZ).date()
    nxt = plan_for(calendar.next_session(d).date, calendar, cfg)
    if nxt is None:  # pragma: no cover - next_session always returns a session
        raise RuntimeError("no next session")
    return nxt.steps[0].at
