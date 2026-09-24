"""Shared input validation and rolling helpers."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def check_series(values: pd.Series[float], what: str = "input") -> pd.Series[float]:
    """Validate a numeric input series.

    Leading NaN is allowed (e.g. the warm-up of an upstream indicator). NaN or
    infinity *after* the first valid value is an error: silently skipping gaps
    would make windows span different amounts of time.
    """
    if not isinstance(values, pd.Series):
        raise TypeError(f"{what} must be a pandas Series")
    arr = values.to_numpy(dtype=float)
    if np.isinf(arr).any():
        raise ValueError(f"{what} contains infinite values")
    valid = ~np.isnan(arr)
    if valid.any():
        first = int(np.argmax(valid))
        if not valid[first:].all():
            raise ValueError(f"{what} has gaps (NaN after its first valid value)")
    return values.astype("float64")


def check_positive(values: pd.Series[float], what: str) -> None:
    arr = values.to_numpy(dtype=float)
    finite = arr[~np.isnan(arr)]
    if (finite <= 0).any():
        raise ValueError(f"{what} must be strictly positive (prices)")


def check_window(window: int, name: str = "window", minimum: int = 1) -> None:
    if isinstance(window, bool) or not isinstance(window, int) or window < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {window!r}")


def check_ohlc(bars: pd.DataFrame, columns: tuple[str, ...]) -> None:
    missing = [c for c in columns if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing columns {missing}")
    for c in columns:
        check_series(bars[c], c)


def rolling_mean(values: pd.Series[float], window: int) -> pd.Series[float]:
    return values.rolling(window, min_periods=window).mean()


def rolling_std(values: pd.Series[float], window: int, ddof: int) -> pd.Series[float]:
    out = values.rolling(window, min_periods=window).std(ddof=ddof)
    # rolling variance can come out as tiny negatives -> clip to exact zero
    return out.where(~(out.abs() < 1e-15), 0.0)


def safe_divide(num: pd.Series[float], den: pd.Series[float]) -> pd.Series[float]:
    """num / den with NaN (not +/-inf) where den == 0."""
    out = num / den.where(den != 0)
    return out.astype("float64")


def annualization(periods_per_year: int) -> float:
    check_window(periods_per_year, "periods_per_year")
    return math.sqrt(periods_per_year)


def wilder_smooth(values: np.ndarray, window: int) -> np.ndarray:
    """Wilder's smoothing (RMA): seed with the mean of the first ``window`` valid
    values, then ``s_t = (s_{t-1} * (window - 1) + x_t) / window``.

    Output is NaN until the seed is complete. Input may have leading NaN only.
    """
    out = np.full(values.shape, np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if valid.size < window:
        return out
    start = int(valid[0])
    seed_end = start + window - 1
    out[seed_end] = values[start : seed_end + 1].mean()
    for i in range(seed_end + 1, len(values)):
        out[i] = (out[i - 1] * (window - 1) + values[i]) / window
    return out
