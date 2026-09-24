from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from adaptive_quant.core.errors import CorporateActionsUnavailable, DataProviderError
from adaptive_quant.quant.data.bars import Adjustment, Frequency
from adaptive_quant.quant.data.corporate_actions import ActionType
from adaptive_quant.quant.data.providers.base import BarRequest
from adaptive_quant.quant.data.providers.files import FileDataProvider
from tests.data_helpers import calendar, daily_bars

REQ = BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 31))


def provider(tmp_path: Path, adjustment: Adjustment = Adjustment.RAW) -> FileDataProvider:
    return FileDataProvider(tmp_path, adjustment, calendar())


def write_daily_csv(path: Path) -> None:
    df = daily_bars(date(2023, 12, 1), date(2024, 2, 29))
    out = df.reset_index()
    out["Date"] = out["timestamp"].dt.tz_convert("America/New_York").dt.date
    out = out.drop(columns=["timestamp"]).rename(columns=str.title)
    out.to_csv(path, index=False)


def test_daily_csv_with_aliases_and_date_filter(tmp_path: Path) -> None:
    write_daily_csv(tmp_path / "QQQ.csv")
    bars = provider(tmp_path).fetch_bars(REQ)
    assert bars.index[0] == datetime(2024, 1, 2, 21, tzinfo=UTC)
    assert bars.index[-1] == datetime(2024, 1, 31, 21, tzinfo=UTC)


def test_parquet_preferred(tmp_path: Path) -> None:
    df = daily_bars(date(2024, 1, 2), date(2024, 1, 31)).reset_index()
    df["date"] = df.pop("timestamp").dt.tz_convert("America/New_York").dt.date.astype(str)
    df.to_parquet(tmp_path / "QQQ.parquet")
    (tmp_path / "QQQ.csv").write_text("garbage")
    assert len(provider(tmp_path).fetch_bars(REQ)) == 21


def test_intraday_requires_timezone(tmp_path: Path) -> None:
    (tmp_path / "QQQ_5min.csv").write_text(
        "timestamp,open,high,low,close,volume\n2024-01-02 14:30:00,1,1,1,1,1\n"
    )
    req = BarRequest("QQQ", Frequency.MINUTE_5, date(2024, 1, 2), date(2024, 1, 2))
    with pytest.raises(DataProviderError, match="timezone-naive"):
        provider(tmp_path).fetch_bars(req)
    (tmp_path / "QQQ_5min.csv").write_text(
        "timestamp,open,high,low,close,volume\n"
        "2024-01-02T14:30:00Z,1,1,1,1,1\n2024-01-02T12:00:00Z,1,1,1,1,1\n"
    )
    bars = provider(tmp_path).fetch_bars(req)
    assert list(bars.index) == [datetime(2024, 1, 2, 14, 35, tzinfo=UTC)]  # pre-market dropped


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("date,open,high,low\n2024-01-02,1,1,1\n", "cannot find columns"),
        ("date,open,high,low,close,adj close,volume\n2024-01-02,1,1,1,1,1,1\n", "adj close"),
        ("date,open,high,low,close,volume\nnot-a-date,1,1,1,1,1\n", "cannot parse"),
    ],
)
def test_bad_files(tmp_path: Path, content: str, match: str) -> None:
    (tmp_path / "QQQ.csv").write_text(content)
    with pytest.raises(DataProviderError, match=match):
        provider(tmp_path).fetch_bars(REQ)


def test_missing_file_and_wrong_adjustment(tmp_path: Path) -> None:
    with pytest.raises(DataProviderError, match="no file"):
        provider(tmp_path).fetch_bars(REQ)
    with pytest.raises(DataProviderError, match="request asked for"):
        provider(tmp_path, Adjustment.ALL).fetch_bars(REQ)


def test_corporate_actions_file(tmp_path: Path) -> None:
    p = provider(tmp_path)
    with pytest.raises(CorporateActionsUnavailable):
        p.fetch_corporate_actions("QQQ", date(2000, 1, 1), date(2025, 1, 1))
    (tmp_path / "QQQ_actions.csv").write_text(
        "ex_date,type,ratio,amount\n2000-03-20,split,2,\n2024-03-18,cash_dividend,,0.57\n"
        "1999-01-01,split,2,\n"
    )
    actions = p.fetch_corporate_actions("QQQ", date(2000, 1, 1), date(2025, 1, 1))
    assert [(a.type, a.ex_date) for a in actions] == [
        (ActionType.SPLIT, date(2000, 3, 20)),
        (ActionType.CASH_DIVIDEND, date(2024, 3, 18)),
    ]
    (tmp_path / "QQQ_actions.csv").write_text("ex_date,type,ratio,amount\n2000-03-20,merger,,\n")
    with pytest.raises(DataProviderError, match="invalid corporate action"):
        p.fetch_corporate_actions("QQQ", date(2000, 1, 1), date(2025, 1, 1))
