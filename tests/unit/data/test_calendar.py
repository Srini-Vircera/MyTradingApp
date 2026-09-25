from datetime import UTC, date, datetime

import pytest

from adaptive_quant.core.errors import CalendarError
from adaptive_quant.quant.data.calendar import Session, TradingCalendar, nyse_calendar

CAL = nyse_calendar()


@pytest.mark.parametrize(
    ("day", "why"),
    [
        (date(2024, 1, 1), "New Year's Day"),
        (date(2024, 1, 15), "MLK Day"),
        (date(2024, 3, 29), "Good Friday"),
        (date(2023, 6, 19), "Juneteenth (observed since 2022)"),
        (date(2024, 7, 4), "Independence Day"),
        (date(2024, 11, 28), "Thanksgiving"),
        (date(2024, 12, 25), "Christmas"),
        (date(2001, 9, 11), "9/11 closure"),
        (date(2001, 9, 14), "9/11 closure"),
        (date(2012, 10, 29), "Hurricane Sandy"),
        (date(2018, 12, 5), "President G.H.W. Bush mourning"),
        (date(2025, 1, 9), "President Carter mourning"),
        (date(2024, 1, 6), "Saturday"),
    ],
)
def test_market_holidays_and_closures(day: date, why: str) -> None:
    assert not CAL.is_session(day), why


@pytest.mark.parametrize(
    ("day", "close_utc"),
    [
        (date(2024, 7, 3), datetime(2024, 7, 3, 17, 0, tzinfo=UTC)),  # 13:00 EDT
        (date(2024, 11, 29), datetime(2024, 11, 29, 18, 0, tzinfo=UTC)),  # 13:00 EST
        (date(2024, 12, 24), datetime(2024, 12, 24, 18, 0, tzinfo=UTC)),
    ],
)
def test_early_closes(day: date, close_utc: datetime) -> None:
    session = CAL.session(day)
    assert session is not None
    assert session.close == close_utc
    assert session.early_close


def test_regular_close_respects_dst() -> None:
    winter, summer = CAL.session(date(2024, 1, 2)), CAL.session(date(2024, 7, 2))
    assert winter is not None and summer is not None
    assert winter.close == datetime(2024, 1, 2, 21, 0, tzinfo=UTC)
    assert summer.close == datetime(2024, 7, 2, 20, 0, tzinfo=UTC)
    assert not winter.early_close


def test_previous_and_next_skip_weekends_and_holidays() -> None:
    assert CAL.previous_session(date(2024, 1, 2)).date == date(2023, 12, 29)
    assert CAL.next_session(date(2024, 3, 28)).date == date(2024, 4, 1)  # over Good Friday


def test_sessions_in_range_inclusive() -> None:
    days = [s.date for s in CAL.sessions_in_range(date(2024, 1, 2), date(2024, 1, 8))]
    assert days == [date(2024, 1, d) for d in (2, 3, 4, 5, 8)]
    assert CAL.sessions_in_range(date(2024, 1, 8), date(2024, 1, 2)) == []


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (datetime(2024, 1, 2, 20, 45, tzinfo=UTC), date(2023, 12, 29)),  # 15:45 ET, before close
        (datetime(2024, 1, 2, 21, 0, tzinfo=UTC), date(2024, 1, 2)),  # exactly at the close
        (datetime(2024, 1, 6, 12, 0, tzinfo=UTC), date(2024, 1, 5)),  # Saturday
        (datetime(2024, 11, 29, 18, 30, tzinfo=UTC), date(2024, 11, 29)),  # after early close
    ],
)
def test_last_completed_session(as_of: datetime, expected: date) -> None:
    session = CAL.last_completed_session(as_of)
    assert session is not None
    assert session.date == expected


def test_is_open() -> None:
    assert CAL.is_open(datetime(2024, 1, 2, 15, 0, tzinfo=UTC))
    assert not CAL.is_open(datetime(2024, 1, 2, 21, 0, tzinfo=UTC))  # close is exclusive
    assert not CAL.is_open(datetime(2024, 11, 29, 18, 30, tzinfo=UTC))  # early close
    assert not CAL.is_open(datetime(2024, 1, 6, 15, 0, tzinfo=UTC))


def test_sessions_between() -> None:
    fri_close = datetime(2024, 1, 5, 21, 0, tzinfo=UTC)
    tue_noon = datetime(2024, 1, 9, 17, 0, tzinfo=UTC)
    assert CAL.sessions_between(fri_close, tue_noon) == 1  # only Monday closed in between


def test_nominal_close_for_non_session() -> None:
    assert CAL.nominal_close(date(2024, 7, 4)) == datetime(2024, 7, 4, 20, 0, tzinfo=UTC)


def test_outside_coverage_raises_instead_of_guessing() -> None:
    with pytest.raises(CalendarError, match="outside"):
        CAL.is_session(date(1980, 1, 2))
    with pytest.raises(CalendarError, match="outside"):
        CAL.session(date(2100, 1, 4))


def test_custom_calendar_validation() -> None:
    s = Session(
        date(2024, 1, 2),
        datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
    )
    with pytest.raises(CalendarError, match="no sessions"):
        TradingCalendar("x", [])
    with pytest.raises(CalendarError, match="strictly increasing"):
        TradingCalendar("x", [s, s])
    single = TradingCalendar("x", [s])
    with pytest.raises(CalendarError, match="no session before"):
        single.previous_session(date(2024, 1, 2))
    with pytest.raises(CalendarError, match="no session after"):
        single.next_session(date(2024, 1, 2))
