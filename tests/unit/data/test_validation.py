"""One test per defect class, plus clean-data and corporate-action cases."""

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from adaptive_quant.core.errors import DataQualityError
from adaptive_quant.quant.data.bars import Frequency, make_bars
from adaptive_quant.quant.data.validation import (
    BarValidator,
    IssueKind,
    IssueSeverity,
    ValidationPolicy,
)
from tests.data_helpers import calendar, daily_bars, intraday_bars

V = BarValidator(calendar(), ValidationPolicy(max_abs_return=0.5))
START, END = date(2024, 1, 2), date(2024, 3, 28)


def validate(df: pd.DataFrame, **kw: object):  # type: ignore[no-untyped-def]
    return V.validate(df, frequency=Frequency.DAILY, subject="TEST", **kw)  # type: ignore[arg-type]


def clean() -> pd.DataFrame:
    return daily_bars(START, END)


def test_clean_data_passes() -> None:
    report = validate(clean(), expected_start=START, expected_end=END)
    assert report.ok
    assert report.issues == ()
    assert report.rows == len(clean())


def test_empty() -> None:
    report = validate(clean().iloc[0:0])
    assert report.kinds() == {IssueKind.EMPTY}
    assert not report.ok


def test_missing_value() -> None:
    df = clean()
    df.loc[df.index[5], "close"] = float("nan")
    assert IssueKind.MISSING_VALUE in validate(df).kinds()


@pytest.mark.parametrize("price", [0.0, -1.0])
def test_zero_and_negative_prices(price: float) -> None:
    df = clean()
    df.loc[df.index[3], ["open", "low"]] = price
    report = validate(df)
    assert IssueKind.NON_POSITIVE_PRICE in report.kinds()
    assert not report.ok


@pytest.mark.parametrize(
    "mutation",
    [
        {"high": 1.0, "low": 2.0},  # high < low
        {"close": 1e6},  # close above high
        {"open": 0.01},  # open below low
    ],
)
def test_impossible_prices(mutation: dict[str, float]) -> None:
    df = clean()
    for col, val in mutation.items():
        df.loc[df.index[10], col] = val
    assert IssueKind.IMPOSSIBLE_PRICE in validate(df).kinds()


def test_negative_volume() -> None:
    df = clean()
    df.loc[df.index[2], "volume"] = -5
    assert IssueKind.NEGATIVE_VOLUME in validate(df).kinds()


def test_duplicate_bars() -> None:
    df = clean()
    dup = pd.concat([df.iloc[:10], df.iloc[9:10], df.iloc[10:]])
    report = validate(dup)
    assert IssueKind.DUPLICATE in report.kinds()
    assert not report.ok


def test_out_of_order_bars() -> None:
    df = clean()
    swapped = pd.concat([df.iloc[:5], df.iloc[6:7], df.iloc[5:6], df.iloc[7:]])
    report = validate(swapped)
    assert IssueKind.OUT_OF_ORDER in report.kinds()
    assert IssueKind.DUPLICATE not in report.kinds()


def test_missing_bar() -> None:
    df = clean().drop(index=clean().index[20])
    report = validate(df)
    issue = next(i for i in report.issues if i.kind is IssueKind.MISSING_BAR)
    assert issue.count == 1
    assert issue.severity is IssueSeverity.ERROR


def test_missing_bars_at_expected_edges() -> None:
    df = clean().iloc[3:]
    assert IssueKind.MISSING_BAR in validate(df, expected_start=START).kinds()


def test_non_session_bar() -> None:
    df = clean()
    holiday = pd.Timestamp(datetime(2024, 1, 15, 21, tzinfo=UTC))  # MLK day
    extra = df.iloc[[8]].copy()
    extra.index = pd.DatetimeIndex([holiday], name="timestamp")
    report = validate(pd.concat([df.iloc[:9], extra, df.iloc[9:]]).sort_index())
    assert IssueKind.NON_SESSION in report.kinds()


def test_bad_daily_timestamp_not_at_close() -> None:
    df = clean()
    df.index = pd.DatetimeIndex(df.index) - pd.Timedelta(
        hours=5
    )  # midnight-ish stamps instead of close
    assert IssueKind.BAD_TIMESTAMP in validate(df).kinds()


def test_future_timestamp_is_lookahead() -> None:
    df = clean()
    as_of = df.index[-3].to_pydatetime() - timedelta(minutes=1)
    report = validate(df, as_of=as_of)
    issue = next(i for i in report.issues if i.kind is IssueKind.FUTURE_TIMESTAMP)
    assert issue.count == 3


def test_unexplained_jump_is_warning() -> None:
    df = clean()
    i = 30
    df.iloc[i:, :4] = df.iloc[i:, :4] * 0.3  # -70% overnight, e.g. an unrecorded 1:3 split
    report = validate(df)
    assert IssueKind.UNEXPLAINED_JUMP in report.kinds()
    assert report.ok  # warning only


def test_jump_explained_by_corporate_action() -> None:
    df = clean()
    i = 30
    df.iloc[i:, :4] = df.iloc[i:, :4] / 3
    ex_date = df.index[i].tz_convert("America/New_York").date()
    report = validate(df, corporate_action_dates={ex_date})
    assert IssueKind.UNEXPLAINED_JUMP not in report.kinds()


def test_vwap_outside_range_warning() -> None:
    df = clean().assign(vwap=1e6)
    report = validate(df)
    assert IssueKind.VWAP_OUTSIDE_RANGE in report.kinds()
    assert report.ok


def test_structural_problem_reported_not_raised() -> None:
    df = clean().drop(columns=["volume"])
    report = validate(df)
    assert not report.ok


def test_raise_if_invalid_and_summary() -> None:
    df = clean()
    df.loc[df.index[1], "close"] = -1
    report = validate(df)
    assert "non_positive_price" in report.summary()
    with pytest.raises(DataQualityError, match="non_positive_price"):
        report.raise_if_invalid()


def test_examples_are_capped() -> None:
    df = clean()
    df["volume"] = -1.0
    issue = next(i for i in validate(df).issues if i.kind is IssueKind.NEGATIVE_VOLUME)
    assert issue.count == len(df)
    assert len(issue.examples) == 5


class TestIntraday:
    DAY = date(2024, 1, 2)

    def val(self, df: pd.DataFrame):  # type: ignore[no-untyped-def]
        return V.validate(df, frequency=Frequency.MINUTE_5, subject="QQQ 5min")

    def test_full_session_is_clean(self) -> None:
        df = intraday_bars(self.DAY, Frequency.MINUTE_5)
        assert len(df) == 78
        assert self.val(df).issues == ()

    def test_early_close_session(self) -> None:
        df = intraday_bars(date(2024, 11, 29), Frequency.MINUTE_5)
        assert len(df) == 42  # 09:30-13:00
        assert self.val(df).ok

    def test_missing_intraday_bar_is_warning(self) -> None:
        df = intraday_bars(self.DAY, Frequency.MINUTE_5).drop(
            index=[pd.Timestamp("2024-01-02 15:00", tz="UTC")]
        )
        report = self.val(df)
        assert IssueKind.MISSING_INTRADAY_BAR in report.kinds()
        assert report.ok

    def test_misaligned_and_after_hours_bars(self) -> None:
        df = intraday_bars(self.DAY, Frequency.MINUTE_5)
        bad = make_bars(
            [datetime(2024, 1, 2, 15, 2, tzinfo=UTC), datetime(2024, 1, 2, 22, 0, tzinfo=UTC)],
            open=[100, 100],
            high=[101, 101],
            low=[99, 99],
            close=[100, 100],
            volume=[1, 1],
        )
        report = self.val(pd.concat([df, bad]).sort_index())
        issue = next(i for i in report.issues if i.kind is IssueKind.BAD_TIMESTAMP)
        assert issue.count == 2

    def test_intraday_on_holiday(self) -> None:
        df = make_bars(
            [datetime(2024, 7, 4, 15, 0, tzinfo=UTC)],
            open=[1],
            high=[1],
            low=[1],
            close=[1],
            volume=[1],
        )
        assert IssueKind.BAD_TIMESTAMP in self.val(df).kinds()
