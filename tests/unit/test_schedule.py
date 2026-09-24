"""Schedule: minutes-before-close, early closes, weekends/holidays, validation."""

from datetime import date, datetime, time

import pytest

from adaptive_quant.config.schema import ScheduleConfig
from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.trading.scheduler.schedule import (
    CYCLE_STEPS,
    next_step_time,
    plan_for,
    validate_schedule,
)
from tests.data_helpers import calendar
from tests.unit.backtest.helpers import SETTINGS

CFG = SETTINGS.schedule


def local(dt: datetime) -> str:
    return dt.astimezone(MARKET_TZ).strftime("%Y-%m-%d %H:%M")


def test_shipped_schedule_is_valid_and_complete() -> None:
    validate_schedule(CFG)
    assert tuple(s.name for s in CFG.steps) == CYCLE_STEPS


def test_regular_and_early_close_days() -> None:
    reg = plan_for(date(2024, 7, 2), calendar(), CFG)
    early = plan_for(date(2024, 11, 29), calendar(), CFG)  # day after Thanksgiving: 13:00 close
    assert reg is not None and early is not None
    assert (
        local(reg.steps[0].at) == "2024-07-02 15:40"
        and local(reg.steps[-1].at) == "2024-07-02 16:15"
    )
    assert (
        local(early.steps[0].at) == "2024-11-29 12:40"
        and local(early.order_cutoff) == "2024-11-29 12:55"
    )
    assert early.session.early_close and not reg.session.early_close


@pytest.mark.parametrize(
    "d", [date(2024, 7, 4), date(2024, 12, 25), date(2024, 7, 6), date(2024, 7, 7)]
)
def test_no_plan_on_holidays_and_weekends(d: date) -> None:
    assert plan_for(d, calendar(), CFG) is None


def test_next_step_time() -> None:
    cal = calendar()
    fri_night = datetime.combine(date(2024, 7, 5), time(20, 0), tzinfo=MARKET_TZ)
    # Friday's cycle finished: next wake is Monday's first step (weekend skipped)
    assert local(next_step_time(fri_night, cal, CFG, set(CYCLE_STEPS))) == "2024-07-08 15:40"
    # nothing ran on Friday (process was down): catch up immediately
    assert next_step_time(fri_night, cal, CFG, set()) == fri_night
    midday = datetime.combine(date(2024, 7, 2), time(12, 0), tzinfo=MARKET_TZ)
    assert local(next_step_time(midday, cal, CFG, set())) == "2024-07-02 15:40"
    assert local(next_step_time(midday, cal, CFG, set(CYCLE_STEPS[:3]))) == "2024-07-02 15:47"
    late = datetime.combine(date(2024, 7, 2), time(15, 50), tzinfo=MARKET_TZ)
    assert next_step_time(late, cal, CFG, set()) == late  # overdue steps: now


def test_invalid_schedules_are_rejected() -> None:
    steps = [s.model_dump() for s in CFG.steps]
    with pytest.raises(ValueError, match="exactly"):
        validate_schedule(ScheduleConfig(steps=steps[:-1]))
    moved = [dict(s) for s in steps]
    moved[6]["minutes_before_close"] = 4  # order submission after the 5-minute cutoff
    moved[7]["minutes_before_close"] = 3
    with pytest.raises(ValueError, match="cutoff"):
        validate_schedule(ScheduleConfig(steps=moved))
