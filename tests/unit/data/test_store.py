import json
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.errors import DataQualityError, MissingDataError
from adaptive_quant.quant.data.bars import Adjustment, Frequency, SeriesKey
from adaptive_quant.quant.data.corporate_actions import ActionType, CorporateAction
from adaptive_quant.quant.data.store import ParquetBarStore, parquet_provenance
from adaptive_quant.quant.data.validation import BarValidator, ValidationReport
from tests.data_helpers import calendar, daily_bars

KEY = SeriesKey("file", "QQQ", Frequency.DAILY, Adjustment.RAW)
V = BarValidator(calendar())


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> ParquetBarStore:
    return ParquetBarStore(tmp_path / "data", clock)


def report(df: pd.DataFrame) -> ValidationReport:
    return V.validate(df, frequency=Frequency.DAILY, subject="t")


def test_roundtrip_preserves_data(store: ParquetBarStore) -> None:
    df = daily_bars(date(2024, 1, 2), date(2024, 2, 29))
    info = store.write(KEY, df, provider="file", validation=report(df))
    out = store.read(KEY)
    assert out.equals(df)
    assert info.rows == len(df)
    assert info.validation_passed


def test_writes_are_idempotent(store: ParquetBarStore) -> None:
    df = daily_bars(date(2024, 1, 2), date(2024, 1, 31))
    first = store.write(KEY, df, provider="file", validation=report(df))
    second = store.write(KEY, df.copy(), provider="file", validation=report(df))
    assert first == second
    assert len(store.history(KEY)) == 1
    assert len(list(store._series_dir(KEY).glob("*.parquet"))) == 1


def test_new_data_creates_new_version_and_keeps_history(store: ParquetBarStore) -> None:
    a = daily_bars(date(2024, 1, 2), date(2024, 1, 31))
    b = daily_bars(date(2024, 1, 2), date(2024, 2, 29))
    store.write(KEY, a, provider="file", validation=report(a))
    store.write(KEY, b, provider="file", validation=report(b))
    hist = store.history(KEY)
    assert [h.rows for h in hist] == [len(a), len(b)]
    assert store.read_snapshot(hist[0]).equals(a)
    assert store.read(KEY).equals(b)


def test_invalid_snapshots_are_not_served_by_default(store: ParquetBarStore) -> None:
    good = daily_bars(date(2024, 1, 2), date(2024, 1, 31))
    bad = good.copy()
    bad.loc[bad.index[3], "close"] = -1.0
    store.write(KEY, good, provider="file", validation=report(good))
    info = store.write(KEY, bad, provider="file", validation=report(bad))
    assert not info.validation_passed
    assert store.read(KEY).equals(good)  # falls back to the last valid version
    assert store.read(KEY, require_valid=False).equals(bad)


def test_nothing_valid_raises(store: ParquetBarStore) -> None:
    with pytest.raises(MissingDataError, match="no validated data"):
        store.read(KEY)


def test_corruption_is_detected(store: ParquetBarStore) -> None:
    df = daily_bars(date(2024, 1, 2), date(2024, 1, 31))
    info = store.write(KEY, df, provider="file", validation=report(df))
    path = store._series_dir(KEY) / info.file
    tampered = df.copy()
    tampered.loc[tampered.index[0], "close"] = 1.0
    tampered.to_parquet(path)
    with pytest.raises(DataQualityError, match="integrity"):
        store.read(KEY)


def test_unsorted_frames_are_refused(store: ParquetBarStore) -> None:
    df = daily_bars(date(2024, 1, 2), date(2024, 1, 31)).iloc[::-1]
    with pytest.raises(DataQualityError, match="sorted"):
        store.write(KEY, df, provider="file", validation=report(df))


def test_synthetic_label_travels_with_the_file(store: ParquetBarStore) -> None:
    key = SeriesKey("synthetic", "TQQQ", Frequency.DAILY, Adjustment.ALL)
    df = daily_bars(date(2024, 1, 2), date(2024, 1, 31)).assign(is_synthetic=True)
    info = store.write(key, df, provider="synthetic", validation=report(df), is_synthetic=True)
    embedded = parquet_provenance(store._series_dir(key) / info.file)
    assert embedded.is_synthetic
    manifest = json.loads((store._series_dir(key) / "manifest.json").read_text())
    assert manifest["snapshots"][0]["is_synthetic"] is True
    assert pq.read_schema(store._series_dir(key) / info.file).metadata is not None


def test_list_series(store: ParquetBarStore) -> None:
    df = daily_bars(date(2024, 1, 2), date(2024, 1, 31))
    other = SeriesKey("file", "SPY", Frequency.DAILY, Adjustment.ALL)
    store.write(KEY, df, provider="file", validation=report(df))
    store.write(other, df, provider="file", validation=report(df))
    assert set(store.list_series()) == {KEY, other}


def test_corporate_actions_roundtrip(store: ParquetBarStore) -> None:
    assert store.read_corporate_actions("file", "QQQ") is None  # unknown, not "none"
    action = CorporateAction(
        symbol="QQQ", ex_date=date(2024, 3, 18), type=ActionType.CASH_DIVIDEND, amount=0.57
    )
    store.write_corporate_actions("file", "QQQ", [action, action])
    assert store.read_corporate_actions("file", "QQQ") == [action]
    store.write_corporate_actions("file", "SPY", [])
    assert store.read_corporate_actions("file", "SPY") == []


def test_unsafe_path_components_rejected(store: ParquetBarStore) -> None:
    df = daily_bars(date(2024, 1, 2), date(2024, 1, 31))
    with pytest.raises(ValueError, match="unsafe"):
        store.write(
            SeriesKey("..", "QQQ", Frequency.DAILY, Adjustment.RAW),
            df,
            provider="x",
            validation=report(df),
        )
