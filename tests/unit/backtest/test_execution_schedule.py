from datetime import UTC, date, datetime, timedelta

import pytest

from adaptive_quant.quant.backtest.execution import (
    ExecutionTiming,
    FillPoint,
    allows_same_bar_close,
    fill_time,
    schedule,
)
from tests.data_helpers import calendar

S = calendar().session(date(2024, 11, 29))  # early close 13:00 ET


def test_near_close_respects_early_close() -> None:
    assert S is not None
    sch = schedule(ExecutionTiming.NEAR_CLOSE, S, 0, 15)
    assert sch.decision_time == datetime(2024, 11, 29, 17, 45, tzinfo=UTC)  # 12:45 ET
    assert (sch.fill_offset, sch.fill_point) == (0, FillPoint.CLOSE)


@pytest.mark.parametrize(
    ("timing", "offset", "point"),
    [
        (ExecutionTiming.NEXT_OPEN, 1, FillPoint.OPEN),
        (ExecutionTiming.NEXT_CLOSE, 1, FillPoint.CLOSE),
        (ExecutionTiming.CLOSING_AUCTION, 0, FillPoint.CLOSE),
    ],
)
def test_close_time_models(timing: ExecutionTiming, offset: int, point: FillPoint) -> None:
    assert S is not None
    sch = schedule(timing, S, 2, 15)
    assert sch.decision_time == S.close
    assert (sch.fill_offset, sch.fill_point) == (offset + 2, point)


def test_fill_time_and_same_bar_flag() -> None:
    assert S is not None
    assert fill_time(S, FillPoint.OPEN) == S.open
    assert fill_time(S, FillPoint.CLOSE) == S.close
    assert allows_same_bar_close(ExecutionTiming.CLOSING_AUCTION, 0)
    assert not allows_same_bar_close(ExecutionTiming.CLOSING_AUCTION, 1)
    assert not any(
        allows_same_bar_close(t, 0)
        for t in ExecutionTiming
        if t is not ExecutionTiming.CLOSING_AUCTION
    )


def test_invalid_schedules() -> None:
    assert S is not None
    with pytest.raises(ValueError, match="delay"):
        schedule(ExecutionTiming.NEXT_OPEN, S, -1, 15)
    with pytest.raises(ValueError, match="longer than the session"):
        schedule(ExecutionTiming.NEAR_CLOSE, S, 0, int((S.close - S.open) / timedelta(minutes=1)))
