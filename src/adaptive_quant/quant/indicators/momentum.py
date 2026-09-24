"""Momentum indicators."""

from __future__ import annotations

import numpy as np
import pandas as pd

from adaptive_quant.quant.indicators._common import (
    check_positive,
    check_series,
    check_window,
    wilder_smooth,
)


def rate_of_change(values: pd.Series[float], window: int) -> pd.Series[float]:
    """Simple return over ``window`` bars: ``x_t / x_{t-window} - 1`` (warm-up: window)."""
    check_window(window)
    x = check_series(values)
    check_positive(x, "rate_of_change input")
    return (x / x.shift(window) - 1.0).astype("float64")


def momentum(values: pd.Series[float], window: int, skip: int = 0) -> pd.Series[float]:
    """Log-return momentum from ``t - window`` to ``t - skip``.

    ``log(x_{t-skip} / x_{t-window})``. Log returns add across time and do not
    depend on the price level. ``skip > 0`` excludes the most recent bars
    (e.g. the classic 12-1 month momentum: window=252, skip=21). Warm-up: window.
    """
    check_window(window)
    if isinstance(skip, bool) or not isinstance(skip, int) or not 0 <= skip < window:
        raise ValueError(f"skip must be an integer in [0, window), got {skip!r}")
    x = check_series(values)
    check_positive(x, "momentum input")
    logs = pd.Series(np.log(x.to_numpy(dtype=float)), index=x.index, dtype="float64")
    return (logs.shift(skip) - logs.shift(window)).astype("float64")


def momentum_acceleration(values: pd.Series[float], window: int, lag: int) -> pd.Series[float]:
    """Change in momentum: ``ROC_window(t) - ROC_window(t - lag)``.

    Positive when momentum is strengthening. Warm-up: window + lag.
    """
    check_window(lag, "lag")
    roc = rate_of_change(values, window)
    return (roc - roc.shift(lag)).astype("float64")


def rsi(values: pd.Series[float], window: int = 14) -> pd.Series[float]:
    """Wilder's Relative Strength Index (0..100).

    Gains/losses of consecutive closes are smoothed with Wilder's RMA seeded by
    the simple mean of the first ``window`` changes. ``RSI = 100 - 100/(1+RS)``;
    100 when there are no losses, 50 when the window has neither gains nor
    losses. Warm-up: window (needs window + 1 prices).
    """
    check_window(window, minimum=2)
    x = check_series(values).to_numpy(dtype=float)
    delta = np.diff(x, prepend=np.nan)
    gains = np.where(np.isnan(delta), np.nan, np.maximum(delta, 0.0))
    losses = np.where(np.isnan(delta), np.nan, np.maximum(-delta, 0.0))
    avg_gain = wilder_smooth(gains, window)
    avg_loss = wilder_smooth(losses, window)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - 100.0 / (1.0 + rs)
    out = np.where((avg_loss == 0) & (avg_gain > 0), 100.0, out)
    out = np.where((avg_loss == 0) & (avg_gain == 0), 50.0, out)
    return pd.Series(out, index=values.index, dtype="float64")
