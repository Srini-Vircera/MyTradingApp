"""Volatility indicators."""

from __future__ import annotations

import numpy as np
import pandas as pd

from adaptive_quant.quant.indicators._common import (
    TRADING_DAYS_PER_YEAR,
    annualization,
    check_ohlc,
    check_positive,
    check_series,
    check_window,
    rolling_mean,
    rolling_std,
    safe_divide,
    wilder_smooth,
)


def rolling_stdev(values: pd.Series[float], window: int, ddof: int = 1) -> pd.Series[float]:
    """Rolling standard deviation of any series (sample, ddof=1, by default)."""
    if ddof not in (0, 1):
        raise ValueError("ddof must be 0 (population) or 1 (sample)")
    check_window(window, minimum=ddof + 1)
    return rolling_std(check_series(values), window, ddof)


def log_returns(values: pd.Series[float]) -> pd.Series[float]:
    x = check_series(values)
    check_positive(x, "price")
    ratio = (x / x.shift(1)).to_numpy(dtype=float)
    return pd.Series(np.log(ratio), index=x.index, dtype="float64")


def historical_volatility(
    values: pd.Series[float],
    window: int = 20,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> pd.Series[float]:
    """Annualized close-to-close volatility: sample std of the last ``window``
    log returns x sqrt(periods_per_year). Warm-up: window (window + 1 prices)."""
    check_window(window, minimum=2)
    return rolling_std(log_returns(values), window, ddof=1) * annualization(periods_per_year)


def true_range(bars: pd.DataFrame) -> pd.Series[float]:
    """``max(high - low, |high - prev_close|, |low - prev_close|)``; NaN on the first
    bar (no previous close - using high-low there would mix definitions)."""
    check_ohlc(bars, ("high", "low", "close"))
    prev = bars["close"].shift(1)
    ranges = pd.concat(
        [bars["high"] - bars["low"], (bars["high"] - prev).abs(), (bars["low"] - prev).abs()],
        axis=1,
    )
    tr = ranges.max(axis=1, skipna=False)
    return tr.astype("float64")


def atr(bars: pd.DataFrame, window: int = 14) -> pd.Series[float]:
    """Average True Range with Wilder smoothing (seeded by the mean of the first
    ``window`` true ranges). Warm-up: window."""
    check_window(window)
    tr = true_range(bars).to_numpy(dtype=float)
    return pd.Series(wilder_smooth(tr, window), index=bars.index, dtype="float64")


def bollinger_bands(
    values: pd.Series[float], window: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    """Bollinger Bands: middle = SMA, bands = middle ± num_std x population std
    (ddof=0, Bollinger's convention). Also returns ``width`` = (upper - lower) /
    middle and ``percent_b`` = (x - lower) / (upper - lower). Warm-up: window - 1."""
    check_window(window, minimum=2)
    if not np.isfinite(num_std) or num_std <= 0:
        raise ValueError("num_std must be a positive number")
    x = check_series(values)
    middle = rolling_mean(x, window)
    dev = rolling_std(x, window, ddof=0) * num_std
    upper, lower = middle + dev, middle - dev
    return pd.DataFrame(
        {
            "middle": middle,
            "upper": upper,
            "lower": lower,
            "width": safe_divide(upper - lower, middle),
            "percent_b": safe_divide(x - lower, upper - lower),
        },
        index=values.index,
    )


def volatility_percentile(
    values: pd.Series[float],
    vol_window: int = 20,
    lookback: int = 252,
) -> pd.Series[float]:
    """Where current realized volatility sits within its own recent history.

    Fraction of the last ``lookback`` historical-volatility readings (including
    the current one) that are ``<=`` the current reading, so the result is in
    ``(0, 1]``; 1.0 means "highest in the lookback". Warm-up:
    vol_window + lookback - 1.
    """
    check_window(lookback, "lookback", minimum=2)
    vol = historical_volatility(values, vol_window).to_numpy(dtype=float)
    out = np.full(vol.shape, np.nan)
    for i in range(len(vol)):
        lo = i - lookback + 1
        if lo < 0:
            continue
        win = vol[lo : i + 1]
        if np.isnan(win).any():
            continue
        out[i] = float((win <= vol[i]).mean())
    return pd.Series(out, index=values.index, dtype="float64")
