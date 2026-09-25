"""Canonical bar format.

A *bar frame* is a ``pandas.DataFrame`` with:

* index ``timestamp``: tz-aware UTC ``DatetimeIndex`` holding each bar's **end**
  time (daily bars: the session close);
* required ``float64`` columns ``open, high, low, close, volume``;
* optional columns ``vwap`` (float64), ``trade_count`` (float64) and
  ``is_synthetic`` (bool, only on spliced synthetic/real histories).

:func:`to_canonical` coerces types but deliberately does **not** sort or
de-duplicate: ordering and duplicate problems must stay visible to validation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import numpy as np
import pandas as pd

from adaptive_quant.core.errors import DataQualityError

INDEX_NAME = "timestamp"
PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
REQUIRED_COLUMNS: tuple[str, ...] = (*PRICE_COLUMNS, "volume")
FLOAT_OPTIONAL_COLUMNS: tuple[str, ...] = ("vwap", "trade_count")
BOOL_OPTIONAL_COLUMNS: tuple[str, ...] = ("is_synthetic",)
ALL_COLUMNS: tuple[str, ...] = (*REQUIRED_COLUMNS, *FLOAT_OPTIONAL_COLUMNS, *BOOL_OPTIONAL_COLUMNS)


class Frequency(StrEnum):
    DAILY = "1d"
    MINUTE_1 = "1min"
    MINUTE_5 = "5min"
    MINUTE_15 = "15min"
    MINUTE_30 = "30min"

    @property
    def is_intraday(self) -> bool:
        return self is not Frequency.DAILY

    @property
    def duration(self) -> timedelta:
        """Bar length. Daily bars have no fixed length (early closes) - use the calendar."""
        if self is Frequency.DAILY:
            raise ValueError("daily bars have no fixed duration; use the trading calendar")
        return _DURATIONS[self]


_DURATIONS = {
    Frequency.MINUTE_1: timedelta(minutes=1),
    Frequency.MINUTE_5: timedelta(minutes=5),
    Frequency.MINUTE_15: timedelta(minutes=15),
    Frequency.MINUTE_30: timedelta(minutes=30),
}


class Adjustment(StrEnum):
    RAW = "raw"  # as traded; what the broker sees - use for sizing and fills
    SPLIT = "split"  # split-adjusted price return
    ALL = "all"  # split + dividend adjusted (total return) - use for signals/research


@dataclass(frozen=True, order=True)
class SeriesKey:
    """Identifies one stored bar series."""

    source: str
    symbol: str
    frequency: Frequency
    adjustment: Adjustment

    def __str__(self) -> str:
        return f"{self.source}:{self.symbol}:{self.frequency}:{self.adjustment}"


def empty_bars() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name=INDEX_NAME)
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in REQUIRED_COLUMNS}, index=index)


def make_bars(
    timestamps: Sequence[datetime] | pd.DatetimeIndex,
    *,
    open: Sequence[float],
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    volume: Sequence[float],
    vwap: Sequence[float] | None = None,
    trade_count: Sequence[float] | None = None,
) -> pd.DataFrame:
    """Build a canonical frame from column sequences (provider adapters use this)."""
    data: dict[str, Sequence[float]] = {
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }
    if vwap is not None:
        data["vwap"] = vwap
    if trade_count is not None:
        data["trade_count"] = trade_count
    return to_canonical(pd.DataFrame(data, index=pd.DatetimeIndex(list(timestamps))))


def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce ``df`` to the canonical dtypes. Raises :class:`DataQualityError` if impossible."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise DataQualityError("bar index must be a DatetimeIndex of bar end times")
    if df.index.tz is None:
        raise DataQualityError(
            "bar timestamps are timezone-naive",
            hint="attach the correct timezone at the provider boundary; naive times are ambiguous",
        )
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataQualityError(f"bars are missing required columns: {missing}")
    unknown = [c for c in df.columns if c not in ALL_COLUMNS]
    if unknown:
        raise DataQualityError(f"bars contain unknown columns: {unknown}")
    # nanosecond resolution everywhere: pandas 3 infers "us" for Python datetimes,
    # and the unit must not change a series' content hash
    out = pd.DataFrame(index=df.index.tz_convert("UTC").as_unit("ns").rename(INDEX_NAME))
    for col in (*REQUIRED_COLUMNS, *FLOAT_OPTIONAL_COLUMNS):
        if col in df.columns:
            try:
                out[col] = pd.to_numeric(df[col], errors="raise").astype("float64").to_numpy()
            except (TypeError, ValueError) as exc:
                raise DataQualityError(f"column {col!r} is not numeric: {exc}") from exc
    for col in BOOL_OPTIONAL_COLUMNS:
        if col in df.columns:
            values = df[col]
            if values.isna().any():
                raise DataQualityError(f"column {col!r} contains missing values")
            out[col] = values.astype(bool).to_numpy()
    return out


def is_sorted_unique(df: pd.DataFrame) -> bool:
    idx = df.index
    return bool(idx.is_monotonic_increasing and idx.is_unique)


def require_canonical_sorted(df: pd.DataFrame, what: str = "bars") -> None:
    """Guard for consumers (store, point-in-time view) that need clean, ordered input."""
    to_canonical(df)  # raises on structural problems
    if not is_sorted_unique(df):
        raise DataQualityError(f"{what} must be sorted by timestamp without duplicates")


def index_ns(index: pd.Index) -> np.ndarray:
    """UTC nanoseconds since the epoch as ``int64`` (unit-independent)."""
    utc = pd.DatetimeIndex(index).tz_convert("UTC").tz_localize(None).as_unit("ns")
    return np.asarray(utc.to_numpy(dtype="datetime64[ns]")).view(np.int64)


def content_hash(df: pd.DataFrame) -> str:
    """Deterministic SHA-256 of a canonical frame's index and values.

    Independent of the Parquet writer version, so identical data always yields
    the identical snapshot ID (idempotent storage) and corruption is detectable.
    """
    import hashlib

    h = hashlib.sha256()
    h.update(np.ascontiguousarray(index_ns(df.index)).tobytes())
    for col in sorted(df.columns):
        h.update(col.encode())
        values = df[col].to_numpy()
        if values.dtype == np.bool_:
            h.update(np.ascontiguousarray(values.astype(np.uint8)).tobytes())
        else:
            h.update(np.ascontiguousarray(values.astype(np.float64)).tobytes())
    return h.hexdigest()
