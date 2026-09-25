"""Path/performance indicators: drawdown and rolling Sharpe."""

from __future__ import annotations

import numpy as np
import pandas as pd

from adaptive_quant.quant.indicators._common import (
    TRADING_DAYS_PER_YEAR,
    annualization,
    check_positive,
    check_series,
    check_window,
    rolling_mean,
    rolling_std,
    safe_divide,
)


def drawdown(values: pd.Series[float], window: int | None = None) -> pd.Series[float]:
    """Decline from the running peak: ``x / peak - 1`` (<= 0).

    ``window=None`` uses the peak since the start of the series (expanding);
    otherwise the peak of the last ``window`` bars. Note that the expanding
    version depends on where the history starts. Warm-up: 0 / window - 1.
    """
    x = check_series(values)
    check_positive(x, "drawdown input")
    if window is None:
        peak = x.cummax()
    else:
        check_window(window)
        peak = x.rolling(window, min_periods=window).max()
    return (x / peak - 1.0).astype("float64")


def drawdown_duration(values: pd.Series[float]) -> pd.Series[float]:
    """Bars since the running (expanding) peak; 0 on a new high."""
    x = check_series(values).to_numpy(dtype=float)
    out = np.full(x.shape, np.nan)
    peak = float("-inf")
    since = 0
    for i, v in enumerate(x.tolist()):
        if np.isnan(v):
            continue
        if v >= peak:
            peak, since = v, 0
        else:
            since += 1
        out[i] = since
    return pd.Series(out, index=values.index, dtype="float64")


def rolling_sharpe(
    values: pd.Series[float],
    window: int = 63,
    risk_free_annual: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> pd.Series[float]:
    """Annualized Sharpe ratio of simple returns over the last ``window`` returns.

    ``mean(r - rf/periods) / std(r, ddof=1) x sqrt(periods)``; NaN when the
    window has zero volatility. Input is a *price/equity* series. Warm-up: window.
    """
    check_window(window, minimum=2)
    if not np.isfinite(risk_free_annual):
        raise ValueError("risk_free_annual must be finite")
    x = check_series(values)
    check_positive(x, "rolling_sharpe input")
    excess = (x / x.shift(1) - 1.0) - risk_free_annual / periods_per_year
    mean = rolling_mean(excess, window)
    std = rolling_std(excess, window, ddof=1)
    return safe_divide(mean, std) * annualization(periods_per_year)
