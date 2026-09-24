"""Rolling price ranges (breakout inputs)."""

from __future__ import annotations

import pandas as pd

from adaptive_quant.quant.indicators._common import check_series, check_window


def rolling_high(
    values: pd.Series[float], window: int, include_current: bool = True
) -> pd.Series[float]:
    """Highest value of the last ``window`` bars.

    ``include_current=False`` gives the high of the ``window`` bars *before*
    the current one - the level a breakout must exceed ("close above the prior
    20-day high"). Warm-up: window - 1 (window if excluding current).
    """
    check_window(window)
    x = check_series(values)
    out = x.rolling(window, min_periods=window).max()
    return out if include_current else out.shift(1)


def rolling_low(
    values: pd.Series[float], window: int, include_current: bool = True
) -> pd.Series[float]:
    """Lowest value of the last ``window`` bars (see :func:`rolling_high`)."""
    check_window(window)
    x = check_series(values)
    out = x.rolling(window, min_periods=window).min()
    return out if include_current else out.shift(1)
