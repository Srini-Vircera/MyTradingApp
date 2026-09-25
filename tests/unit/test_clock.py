from datetime import UTC, datetime, timedelta, timezone

import pytest

from adaptive_quant.core.clock import FrozenClock, SystemClock, ensure_utc, to_market_time


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2024, 1, 2, 12, 0))  # noqa: DTZ001 - deliberately naive


def test_aware_datetime_normalised_to_utc() -> None:
    est = timezone(timedelta(hours=-5))
    assert ensure_utc(datetime(2024, 1, 2, 10, 0, tzinfo=est)) == datetime(
        2024, 1, 2, 15, 0, tzinfo=UTC
    )


def test_market_time_handles_daylight_saving() -> None:
    winter = to_market_time(datetime(2024, 1, 2, 21, 0, tzinfo=UTC))
    summer = to_market_time(datetime(2024, 7, 2, 20, 0, tzinfo=UTC))
    assert (winter.hour, summer.hour) == (16, 16)


def test_system_clock_is_utc() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_frozen_clock_advances_but_never_rewinds(clock: FrozenClock) -> None:
    start = clock.now()
    clock.advance(timedelta(minutes=5))
    assert clock.now() - start == timedelta(minutes=5)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
