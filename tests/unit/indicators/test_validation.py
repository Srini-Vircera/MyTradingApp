"""Bad inputs and bad parameters fail loudly."""

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.indicators.momentum import momentum, rsi
from adaptive_quant.quant.indicators.performance import drawdown, rolling_sharpe
from adaptive_quant.quant.indicators.trend import distance_from_ma, sma, trend_slope
from adaptive_quant.quant.indicators.volatility import atr, bollinger_bands, rolling_stdev


def test_gap_after_first_value_rejected() -> None:
    with pytest.raises(ValueError, match="gaps"):
        sma(pd.Series([1.0, np.nan, 3.0]), 2)


def test_infinite_rejected() -> None:
    with pytest.raises(ValueError, match="infinite"):
        sma(pd.Series([1.0, np.inf]), 2)


def test_non_series_rejected() -> None:
    with pytest.raises(TypeError):
        sma([1.0, 2.0], 2)  # type: ignore[arg-type]


@pytest.mark.parametrize("window", [0, -1, 2.5, True, "5"])
def test_bad_windows(window: object) -> None:
    with pytest.raises(ValueError, match="window"):
        sma(pd.Series([1.0, 2.0]), window)  # type: ignore[arg-type]


def test_minimum_windows() -> None:
    x = pd.Series([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match=">= 2"):
        rsi(x, 1)
    with pytest.raises(ValueError, match=">= 2"):
        trend_slope(x, 1)
    with pytest.raises(ValueError, match=">= 2"):
        rolling_stdev(x, 1, ddof=1)
    with pytest.raises(ValueError, match="ddof"):
        rolling_stdev(x, 2, ddof=2)


@pytest.mark.parametrize(
    "call",
    [
        lambda x: momentum(x, 5),
        lambda x: distance_from_ma(x, 2),
        lambda x: trend_slope(x, 2),
        lambda x: drawdown(x),
        lambda x: rolling_sharpe(x, 2),
    ],
)
def test_price_based_indicators_require_positive_prices(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="strictly positive"):
        call(pd.Series([1.0, 0.0, 2.0, 3.0, 4.0, 5.0]))


def test_parameter_value_errors() -> None:
    x = pd.Series(np.arange(1.0, 30.0))
    with pytest.raises(ValueError, match="skip"):
        momentum(x, 5, skip=5)
    with pytest.raises(ValueError, match="kind"):
        distance_from_ma(x, 5, kind="wma")
    with pytest.raises(ValueError, match="num_std"):
        bollinger_bands(x, 5, 0.0)
    with pytest.raises(ValueError, match="risk_free"):
        rolling_sharpe(x, 5, risk_free_annual=float("nan"))


def test_ohlc_indicators_need_columns() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        atr(pd.DataFrame({"close": [1.0, 2.0]}), 1)
