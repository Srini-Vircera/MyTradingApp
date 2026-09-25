"""Trend indicators: moving averages, distance from trend, trend slope."""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from adaptive_quant.quant.indicators._common import (
    TRADING_DAYS_PER_YEAR,
    check_positive,
    check_series,
    check_window,
    rolling_mean,
    rolling_std,
    safe_divide,
)


def sma(values: pd.Series[float], window: int) -> pd.Series[float]:
    """Simple moving average of the last ``window`` values (warm-up: window - 1)."""
    check_window(window)
    return rolling_mean(check_series(values), window)


def ema(values: pd.Series[float], window: int) -> pd.Series[float]:
    """Exponential moving average, ``alpha = 2 / (window + 1)``.

    Seeded with the SMA of the first ``window`` valid values (the common
    "SMA seed" convention), then ``e_t = alpha * x_t + (1 - alpha) * e_{t-1}``.
    NaN before the seed, so no partially-warmed values are ever produced.
    """
    check_window(window)
    x = check_series(values).to_numpy(dtype=float)
    out = np.full(x.shape, np.nan)
    valid = np.flatnonzero(~np.isnan(x))
    if valid.size >= window:
        alpha = 2.0 / (window + 1.0)
        start = int(valid[0])
        seed = start + window - 1
        out[seed] = x[start : seed + 1].mean()
        for i in range(seed + 1, len(x)):
            out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return pd.Series(out, index=values.index, dtype="float64")


def distance_from_ma(values: pd.Series[float], window: int, kind: str = "sma") -> pd.Series[float]:
    """Relative distance of price from its moving average: ``x / MA - 1``."""
    if kind not in ("sma", "ema"):
        raise ValueError("kind must be 'sma' or 'ema'")
    x = check_series(values)
    check_positive(x, "distance_from_ma input")
    ma = sma(x, window) if kind == "sma" else ema(x, window)
    return safe_divide(x, ma) - 1.0


def rolling_zscore(values: pd.Series[float], window: int) -> pd.Series[float]:
    """``(x - mean) / std`` over the last ``window`` values (sample std, ddof=1)."""
    check_window(window, minimum=2)
    x = check_series(values)
    return safe_divide(x - rolling_mean(x, window), rolling_std(x, window, ddof=1))


def _log_windows(values: pd.Series[float], window: int) -> tuple[np.ndarray, np.ndarray]:
    check_window(window, minimum=2)
    x = check_series(values)
    check_positive(x, "trend input")
    logs = np.log(x.to_numpy(dtype=float))
    if len(logs) < window:
        return logs, np.empty((0, window))
    return logs, sliding_window_view(logs, window)


def trend_slope(values: pd.Series[float], window: int, annualize: bool = True) -> pd.Series[float]:
    """OLS slope of ``log(price)`` against bar number over the last ``window`` bars.

    Per-bar log growth rate; multiplied by 252 when ``annualize`` (so 0.20 means
    the fitted trend rises ~20 % per year in log terms). Warm-up: window - 1.
    """
    logs, windows = _log_windows(values, window)
    out = np.full(logs.shape, np.nan)
    if windows.size:
        xc = np.arange(window, dtype=float) - (window - 1) / 2.0
        slopes = windows @ xc / float(xc @ xc)
        out[window - 1 :] = slopes
    if annualize:
        out = out * TRADING_DAYS_PER_YEAR
    return pd.Series(out, index=values.index, dtype="float64")


def trend_r2(values: pd.Series[float], window: int) -> pd.Series[float]:
    """R² of the log-price regression in :func:`trend_slope` (trend quality, 0..1).

    NaN when the window is perfectly flat (R² undefined).
    """
    logs, windows = _log_windows(values, window)
    out = np.full(logs.shape, np.nan)
    if windows.size:
        xc = np.arange(window, dtype=float) - (window - 1) / 2.0
        sxx = float(xc @ xc)
        slopes = windows @ xc / sxx
        yc = windows - windows.mean(axis=1, keepdims=True)
        syy = (yc**2).sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            r2 = np.where(syy > 1e-24, slopes**2 * sxx / syy, np.nan)
        out[window - 1 :] = np.clip(r2, 0.0, 1.0)
    return pd.Series(out, index=values.index, dtype="float64")
