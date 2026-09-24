from datetime import UTC, date, datetime

from adaptive_quant.quant.data.bars import Frequency
from adaptive_quant.quant.data.freshness import check_daily_freshness, check_intraday_freshness
from tests.data_helpers import calendar, daily_bars, intraday_bars

CAL = calendar()


def daily(end: date, as_of: datetime, max_age: int = 1):  # type: ignore[no-untyped-def]
    bars = daily_bars(date(2023, 12, 1), end)
    return check_daily_freshness(
        bars, subject="QQQ", as_of=as_of, calendar=CAL, max_age_sessions=max_age
    )


def test_yesterdays_bar_is_fresh_before_todays_close() -> None:
    assert daily(date(2024, 1, 9), datetime(2024, 1, 10, 20, 40, tzinfo=UTC)).fresh


def test_monday_morning_after_weekend_is_fresh() -> None:
    assert daily(date(2024, 1, 5), datetime(2024, 1, 8, 14, 0, tzinfo=UTC)).fresh


def test_holiday_does_not_make_data_stale() -> None:
    # Friday 2024-01-12 bar, checked Tuesday after MLK day before the close
    assert daily(date(2024, 1, 12), datetime(2024, 1, 16, 20, 0, tzinfo=UTC)).fresh


def test_missing_one_session_with_zero_tolerance_is_stale() -> None:
    result = daily(date(2024, 1, 8), datetime(2024, 1, 10, 20, 40, tzinfo=UTC), max_age=0)
    assert not result.fresh
    assert "session(s) old" in result.detail


def test_two_sessions_behind_is_stale() -> None:
    assert not daily(date(2024, 1, 5), datetime(2024, 1, 10, 21, 30, tzinfo=UTC)).fresh


def test_empty_and_future() -> None:
    bars = daily_bars(date(2024, 1, 2), date(2024, 1, 31))
    as_of = datetime(2024, 1, 10, tzinfo=UTC)
    assert not check_daily_freshness(
        bars.iloc[0:0], subject="x", as_of=as_of, calendar=CAL, max_age_sessions=1
    ).fresh
    future = check_daily_freshness(bars, subject="x", as_of=as_of, calendar=CAL, max_age_sessions=1)
    assert not future.fresh
    assert "future" in future.detail


class TestIntraday:
    BARS = intraday_bars(date(2024, 1, 2), Frequency.MINUTE_5)

    def check(self, as_of: datetime, bars=None):  # type: ignore[no-untyped-def]
        b = self.BARS if bars is None else bars
        return check_intraday_freshness(
            b, subject="QQQ", as_of=as_of, calendar=CAL, max_age_seconds=180
        )

    def test_fresh_during_session(self) -> None:
        cut = self.BARS[self.BARS.index <= datetime(2024, 1, 2, 18, 0, tzinfo=UTC)]
        assert self.check(datetime(2024, 1, 2, 18, 2, tzinfo=UTC), cut).fresh

    def test_stale_during_session(self) -> None:
        cut = self.BARS[self.BARS.index <= datetime(2024, 1, 2, 17, 0, tzinfo=UTC)]
        assert not self.check(datetime(2024, 1, 2, 18, 0, tzinfo=UTC), cut).fresh

    def test_after_close_needs_the_closing_bar(self) -> None:
        assert self.check(datetime(2024, 1, 2, 23, 0, tzinfo=UTC)).fresh
        cut = self.BARS.iloc[:-10]
        assert not self.check(datetime(2024, 1, 2, 23, 0, tzinfo=UTC), cut).fresh

    def test_just_opened(self) -> None:
        assert self.check(datetime(2024, 1, 3, 14, 31, tzinfo=UTC)).fresh
