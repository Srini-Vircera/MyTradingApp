from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from adaptive_quant.core.errors import DataQualityError
from adaptive_quant.quant.data.bars import (
    Frequency,
    content_hash,
    empty_bars,
    make_bars,
    require_canonical_sorted,
    to_canonical,
)

T = [datetime(2024, 1, 2, 21, tzinfo=UTC), datetime(2024, 1, 3, 21, tzinfo=UTC)]


def two_bars() -> pd.DataFrame:
    return make_bars(T, open=[1, 2], high=[2, 3], low=[0.5, 1], close=[1.5, 2.5], volume=[10, 20])


def test_canonical_dtypes_and_index() -> None:
    df = two_bars()
    assert df.index.name == "timestamp"
    assert str(pd.DatetimeIndex(df.index).tz) == "UTC"
    assert all(str(t) == "float64" for t in df.dtypes)


def test_naive_index_rejected() -> None:
    naive = pd.DataFrame(
        {c: [1.0] for c in ("open", "high", "low", "close", "volume")},
        index=pd.DatetimeIndex([datetime(2024, 1, 2)]),  # noqa: DTZ001
    )
    with pytest.raises(DataQualityError, match="naive"):
        to_canonical(naive)


def test_missing_and_unknown_columns_rejected() -> None:
    df = two_bars()
    with pytest.raises(DataQualityError, match="missing required"):
        to_canonical(df.drop(columns=["volume"]))
    with pytest.raises(DataQualityError, match="unknown columns"):
        to_canonical(df.assign(adj_close=1.0))


def test_non_numeric_rejected() -> None:
    df = two_bars().astype({"close": object})
    df.loc[df.index[0], "close"] = "abc"
    with pytest.raises(DataQualityError, match="not numeric"):
        to_canonical(df)


def test_to_canonical_does_not_sort() -> None:
    df = two_bars().iloc[::-1]
    assert list(to_canonical(df).index) == list(df.index)
    with pytest.raises(DataQualityError, match="sorted"):
        require_canonical_sorted(df)


def test_content_hash_is_stable_and_sensitive() -> None:
    a, b = two_bars(), two_bars()
    assert content_hash(a) == content_hash(b)
    b.loc[b.index[1], "close"] = 2.5000001
    assert content_hash(a) != content_hash(b)


def test_content_hash_independent_of_time_unit() -> None:
    a = two_bars()
    b = a.copy()
    b.index = pd.DatetimeIndex(b.index).as_unit("us")
    assert content_hash(a) == content_hash(b)


def test_frequency_duration() -> None:
    assert Frequency.MINUTE_5.duration == timedelta(minutes=5)
    assert Frequency.MINUTE_5.is_intraday
    with pytest.raises(ValueError, match="calendar"):
        _ = Frequency.DAILY.duration


def test_empty_bars_is_canonical() -> None:
    assert to_canonical(empty_bars()).empty


def test_bool_column_rejects_missing() -> None:
    df = two_bars().assign(is_synthetic=pd.Series([True, None], dtype=object).to_numpy())
    with pytest.raises(DataQualityError, match="missing values"):
        to_canonical(df)
