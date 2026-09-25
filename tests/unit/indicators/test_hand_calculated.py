"""Indicator values checked against hand calculations (derivations in comments)."""

import math

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.indicators.momentum import (
    momentum,
    momentum_acceleration,
    rate_of_change,
    rsi,
)
from adaptive_quant.quant.indicators.performance import (
    drawdown,
    drawdown_duration,
    rolling_sharpe,
)
from adaptive_quant.quant.indicators.ranges import rolling_high, rolling_low
from adaptive_quant.quant.indicators.trend import (
    distance_from_ma,
    ema,
    rolling_zscore,
    sma,
    trend_r2,
    trend_slope,
)
from adaptive_quant.quant.indicators.volatility import (
    atr,
    bollinger_bands,
    historical_volatility,
    rolling_stdev,
    true_range,
    volatility_percentile,
)

NAN = float("nan")


def s(*values: float) -> pd.Series:
    return pd.Series(values, dtype="float64")


def same(actual: pd.Series, expected: list[float]) -> None:
    np.testing.assert_allclose(actual.to_numpy(), np.array(expected), rtol=1e-9, atol=1e-12)


# ------------------------------------------------------------------ trend
def test_sma() -> None:
    same(sma(s(1, 2, 3, 4, 5), 3), [NAN, NAN, 2, 3, 4])


def test_ema_sma_seed_then_recursion() -> None:
    # alpha = 2/(3+1) = 0.5; seed = mean(2,4,6) = 4
    # t3 = 0.5*8 + 0.5*4 = 6 ; t4 = 0.5*4 + 0.5*6 = 5
    same(ema(s(2, 4, 6, 8, 4), 3), [NAN, NAN, 4, 6, 5])


def test_ema_starts_after_leading_nan() -> None:
    same(ema(s(NAN, 2, 4, 6, 8), 3), [NAN, NAN, NAN, 4, 6])


def test_distance_from_ma() -> None:
    # t3: SMA(10,10,12) = 32/3 ; 12 / (32/3) - 1 = 0.125
    same(distance_from_ma(s(10, 10, 10, 12), 3), [NAN, NAN, 0.0, 0.125])


def test_rolling_zscore() -> None:
    # window [3,4,10]: mean 17/3, sample var = ((-8/3)²+(-5/3)²+(13/3)²)/2 = 43/3
    z = (10 - 17 / 3) / math.sqrt(43 / 3)
    out = rolling_zscore(s(1, 2, 3, 4, 10), 3)
    assert out.iloc[4] == pytest.approx(z)  # 1.14457...
    assert out.iloc[2] == pytest.approx(1.0)  # [1,2,3]: (3-2)/1


def test_trend_slope_of_exact_exponential_growth() -> None:
    prices = pd.Series(100 * 1.01 ** np.arange(30))
    per_bar = trend_slope(prices, 10, annualize=False)
    assert per_bar.iloc[9:].to_numpy() == pytest.approx(math.log(1.01))
    assert trend_slope(prices, 10).iloc[-1] == pytest.approx(252 * math.log(1.01))
    assert trend_r2(prices, 10).iloc[-1] == pytest.approx(1.0)


def test_trend_slope_three_points() -> None:
    # log prices y = [0, ln2, ln2]; x centred [-1,0,1]; slope = (0*-1 + ln2*1)/2
    out = trend_slope(s(1, 2, 2), 3, annualize=False)
    assert out.iloc[2] == pytest.approx(math.log(2) / 2)
    # R² = slope² * Sxx / Syy; Syy = (ln2)² * (4/9 + 1/9 + 1/9) = (ln2)² * 2/3
    assert trend_r2(s(1, 2, 2), 3).iloc[2] == pytest.approx(
        (math.log(2) / 2) ** 2 * 2 / ((math.log(2)) ** 2 * 2 / 3)
    )


def test_trend_r2_flat_window_is_nan() -> None:
    assert math.isnan(trend_r2(s(5, 5, 5, 5), 3).iloc[3])


# ------------------------------------------------------------------ momentum
def test_rate_of_change() -> None:
    same(rate_of_change(s(100, 110, 99, 121), 2), [NAN, NAN, -0.01, 0.1])


def test_momentum_log_and_skip() -> None:
    x = s(100, 110, 121, 133.1)
    same(momentum(x, 2), [NAN, NAN, math.log(1.21), math.log(1.21)])
    # window 3, skip 1 at t3: log(x_2 / x_0) = log(121/100)
    same(momentum(x, 3, skip=1), [NAN, NAN, NAN, math.log(1.21)])


def test_momentum_acceleration() -> None:
    # ROC1 = [nan, .1, .1, 110/121-1, 121/110-1]; accel = ROC1 - ROC1.shift(1)
    roc_3 = 110 / 121 - 1
    same(
        momentum_acceleration(s(100, 110, 121, 110, 121), 1, 1),
        [NAN, NAN, 0.0, roc_3 - 0.1, 0.1 - roc_3],
    )


WILDER = [
    44.34,
    44.09,
    44.15,
    43.61,
    44.33,
    44.83,
    45.10,
    45.42,
    45.84,
    46.08,
    45.89,
    46.03,
    45.61,
    46.28,
    46.28,
    46.00,
]


def test_rsi_wilder_example() -> None:
    # First 14 changes: gains sum 3.34, losses sum 1.40 (hand-summed)
    ag, al = 3.34 / 14, 1.40 / 14
    first = 100 - 100 / (1 + ag / al)  # 70.4641
    # next change 46.28 -> 46.00 = -0.28: Wilder update
    ag2, al2 = ag * 13 / 14, (al * 13 + 0.28) / 14
    second = 100 - 100 / (1 + ag2 / al2)  # 66.2496
    out = rsi(pd.Series(WILDER), 14)
    assert out.iloc[:14].isna().all()
    assert out.iloc[14] == pytest.approx(first, abs=1e-9)
    assert out.iloc[15] == pytest.approx(second, abs=1e-9)
    assert round(out.iloc[14], 2) == 70.46


def test_rsi_edge_cases() -> None:
    assert rsi(s(1, 2, 3, 4), 2).iloc[-1] == 100.0  # only gains
    assert rsi(s(4, 3, 2, 1), 2).iloc[-1] == pytest.approx(0.0)  # only losses
    assert rsi(s(5, 5, 5, 5), 2).iloc[-1] == 50.0  # no movement


# ------------------------------------------------------------------ volatility
OHLC = pd.DataFrame(
    {
        "open": [9, 10, 11, 11, 13],
        "high": [10, 11, 12, 11.5, 14],
        "low": [8, 9, 10.5, 9, 12],
        "close": [9, 10, 11, 9.5, 13],
    },
    dtype="float64",
)


def test_true_range_including_gap() -> None:
    # t1 max(2, |11-9|, |9-9|)=2 ; t2 max(1.5, 2, .5)=2 ; t3 max(2.5, .5, 2)=2.5
    # t4 gap up: max(2, |14-9.5|=4.5, |12-9.5|=2.5)=4.5
    same(true_range(OHLC), [NAN, 2, 2, 2.5, 4.5])


def test_atr_wilder() -> None:
    # seed at t2 = mean(2, 2) = 2 ; t3 = (2*1 + 2.5)/2 = 2.25 ; t4 = (2.25 + 4.5)/2 = 3.375
    same(atr(OHLC, 2), [NAN, NAN, 2, 2.25, 3.375])


def test_rolling_std_sample_and_population() -> None:
    same(rolling_stdev(s(1, 2, 3, 4), 3), [NAN, NAN, 1, 1])
    same(rolling_stdev(s(1, 2, 3, 4), 3, ddof=0), [NAN, NAN, math.sqrt(2 / 3), math.sqrt(2 / 3)])


def test_historical_volatility() -> None:
    # returns r1=ln(1.01), r2=ln(100/101); sample std of two values = |r1-r2|/sqrt(2)
    r1, r2 = math.log(1.01), math.log(100 / 101)
    expected = abs(r1 - r2) / math.sqrt(2) * math.sqrt(252)  # 0.2234
    out = historical_volatility(s(100, 101, 100, 102), 2)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(expected)


def test_bollinger_bands() -> None:
    # window [1,2,3]: mid 2, population std sqrt(2/3); k = 2
    sd = math.sqrt(2 / 3)
    b = bollinger_bands(s(1, 2, 3), 3, 2.0).iloc[2]
    assert b["middle"] == pytest.approx(2)
    assert b["upper"] == pytest.approx(2 + 2 * sd)  # 3.63299
    assert b["lower"] == pytest.approx(2 - 2 * sd)
    assert b["width"] == pytest.approx(4 * sd / 2)  # 1.63299
    assert b["percent_b"] == pytest.approx((3 - (2 - 2 * sd)) / (4 * sd))  # 0.80619


def test_bollinger_flat_window_percent_b_is_nan_not_inf() -> None:
    b = bollinger_bands(s(5, 5, 5), 3).iloc[2]
    assert b["width"] == 0.0
    assert math.isnan(b["percent_b"])


def test_volatility_percentile() -> None:
    # log returns alternate sign with growing size, then two equal returns:
    # 2-return vols ~ |r_{t-1} - r_t|: .03, .05, .07, .09, 0  (scaled)
    rets = [0.01, -0.02, 0.03, -0.04, 0.05, 0.05]
    prices = pd.Series(100 * np.exp(np.cumsum([0.0, *rets])))
    out = volatility_percentile(prices, vol_window=2, lookback=3)
    assert out.iloc[:4].isna().all()  # warm-up = 2 + 3 - 1
    assert out.iloc[4] == pytest.approx(1.0)  # highest of [.03,.05,.07]
    assert out.iloc[5] == pytest.approx(1.0)  # highest of [.05,.07,.09]
    assert out.iloc[6] == pytest.approx(1 / 3)  # lowest of [.07,.09,0]


# ------------------------------------------------------------------ ranges / performance
def test_rolling_high_low() -> None:
    x = s(1, 3, 2, 5, 4)
    same(rolling_high(x, 3), [NAN, NAN, 3, 5, 5])
    same(rolling_high(x, 3, include_current=False), [NAN, NAN, NAN, 3, 5])
    same(rolling_low(x, 3), [NAN, NAN, 1, 2, 2])
    same(rolling_low(x, 3, include_current=False), [NAN, NAN, NAN, 1, 2])


def test_drawdown_and_duration() -> None:
    x = s(100, 120, 90, 130, 117)
    same(drawdown(x), [0, 0, -0.25, 0, -0.1])
    same(drawdown(x, window=2), [NAN, 0, -0.25, 0, -0.1])
    same(drawdown_duration(s(100, 120, 90, 95, 130, 117)), [0, 0, 1, 2, 0, 1])


def test_rolling_sharpe() -> None:
    # returns: .01, -1/101, .01 ; window 2 at t2: (.01, -1/101)
    a, b = 0.01, -1 / 101
    mean, sd = (a + b) / 2, abs(a - b) / math.sqrt(2)
    out = rolling_sharpe(s(100, 101, 100, 101), 2)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(mean / sd * math.sqrt(252))


def test_rolling_sharpe_with_risk_free_and_zero_vol() -> None:
    const_growth = pd.Series(100 * 1.001 ** np.arange(10))
    assert np.isnan(rolling_sharpe(const_growth, 3).iloc[-1])  # zero volatility -> NaN, not inf
    x = s(100, 101, 100, 101)
    base = rolling_sharpe(x, 2).iloc[2]
    shifted = rolling_sharpe(x, 2, risk_free_annual=0.0252).iloc[2]
    sd = abs(0.01 + 1 / 101) / math.sqrt(2)
    assert base - shifted == pytest.approx(0.0001 / sd * math.sqrt(252))
