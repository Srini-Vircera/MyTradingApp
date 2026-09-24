"""Behaviour of individual strategies in constructed market scenarios.

These check that each strategy reacts as *designed* - not that any of them is
profitable (that is for Milestones 5-6).
"""

import math

import numpy as np
import pytest

from adaptive_quant.core.enums import SignalDirection
from adaptive_quant.quant.indicators.trend import distance_from_ma
from adaptive_quant.quant.strategies.catalogue.breakout import channel_position
from adaptive_quant.quant.strategies.catalogue.regime import classify
from adaptive_quant.quant.strategies.registry import create
from tests.unit.strategies.helpers import bars_from_returns, view_at

N = 600
_NOISE = np.random.default_rng(42).normal(0, 0.003, N)  # markets are never perfectly smooth
UP = bars_from_returns(0.0015 + _NOISE)  # steady rally
DOWN = bars_from_returns(-0.0015 + _NOISE)  # steady decline
_LEVEL = 100 * (1 + 0.001 * np.sin(np.arange(N + 1) / 3))  # oscillates +/-0.1% around 100
FLAT = bars_from_returns(_LEVEL[1:] / _LEVEL[:-1] - 1)


def sig(impl: str, frame, i: int = N - 1, params=None, extra=None):  # type: ignore[no-untyped-def]
    frames = {"QQQ": frame, **(extra or {})}
    return create(impl, params=params).generate_signal(view_at(frames, i))


# ------------------------------------------------------------------ trend
@pytest.mark.parametrize(
    "impl",
    [
        "ltt_sma_distance",
        "ltt_trend_slope",
        "it_ma_stack",
        "st_ema_cross",
        "mom_multi_horizon",
        "mom_time_series",
        "bo_donchian",
        "trend_vol_filter",
        "dd_aware_trend",
    ],
)
def test_trend_and_momentum_followers_agree_with_the_trend(impl: str) -> None:
    assert sig(impl, UP).direction is SignalDirection.BULLISH
    assert sig(impl, DOWN).direction is SignalDirection.BEARISH


def test_ltt_score_is_tanh_of_distance_over_scale() -> None:
    s = sig("ltt_sma_distance", UP)
    d = distance_from_ma(UP["close"], 200).iloc[-1]
    assert s.raw_score == pytest.approx(d)
    assert s.normalized_score == pytest.approx(math.tanh(d / 0.05))
    assert s.indicator_values["distance"] == pytest.approx(d)


def test_ltt_ndx_confirmation() -> None:
    params = {"confirm_symbol": "NDX"}
    alone = sig("ltt_sma_distance", UP, params=params)
    agree = sig("ltt_sma_distance", UP, params=params, extra={"NDX": UP})
    disagree = sig("ltt_sma_distance", UP, params=params, extra={"NDX": DOWN})
    assert "NDX unavailable" in alone.reason
    assert agree.normalized_score == pytest.approx(alone.normalized_score)
    assert "confirms" in agree.reason
    assert disagree.normalized_score == pytest.approx(alone.normalized_score / 2)
    assert "disagrees" in disagree.reason
    # NDX without enough history is treated as unavailable, not as an error
    short = sig("ltt_sma_distance", UP, params=params, extra={"NDX": UP.iloc[:50]})
    assert "NDX unavailable" in short.reason


def test_ma_stack_votes() -> None:
    assert sig("it_ma_stack", UP).normalized_score == pytest.approx(1.0)
    assert sig("it_ma_stack", DOWN).normalized_score == pytest.approx(-1.0)


def test_trend_following_is_flat_in_a_flat_market() -> None:
    for impl in ("ltt_sma_distance", "it_ma_stack", "st_ema_cross"):
        assert sig(impl, FLAT).direction is SignalDirection.NEUTRAL


# ------------------------------------------------------------------ momentum
def test_momentum_acceleration_detects_strengthening() -> None:
    ramp_up = np.concatenate([np.full(N - 30, 0.0005), np.linspace(0.0005, 0.006, 30)])
    s = sig("mom_acceleration", bars_from_returns(ramp_up))
    assert s.direction is SignalDirection.BULLISH
    assert "strengthening" in s.reason
    ramp_down = np.concatenate([np.full(N - 30, 0.006), np.linspace(0.006, 0.0005, 30)])
    weakening = sig("mom_acceleration", bars_from_returns(ramp_down))
    assert weakening.normalized_score < 0
    assert "against the prevailing move" in weakening.reason  # ROC still up, accel down


def test_bearish_strategy_only_fires_in_confirmed_bear() -> None:
    bear = sig("bear_confirmed_trend", DOWN)
    assert bear.direction is SignalDirection.BEARISH
    assert bear.suggested_exposure == pytest.approx(bear.normalized_score * 0.45)
    assert bear.suggested_exposure >= -0.45
    assert sig("bear_confirmed_trend", UP).normalized_score == 0.0
    # below the long SMA but momentum has turned up: not confirmed -> no bearish signal
    rebound = bars_from_returns(np.concatenate([np.full(N - 40, -0.0015), np.full(40, 0.0015)]))
    r = sig("bear_confirmed_trend", rebound)
    assert r.indicator_values["distance"] < 0 < r.indicator_values["mom"]
    assert r.normalized_score == 0.0


# ------------------------------------------------------------------ breakouts
def test_channel_position_math() -> None:
    assert channel_position(110, 110, 90) == 1.0
    assert channel_position(90, 110, 90) == -1.0
    assert channel_position(100, 110, 90) == 0.0
    assert channel_position(120, 110, 90) == 1.0  # clipped
    assert channel_position(100, 100, 100) == 0.0  # degenerate channel


def test_donchian_new_high_is_full_bullish() -> None:
    frame = bars_from_returns(np.concatenate([0.0015 + _NOISE[:-1], [0.05]]))  # new high today
    assert sig("bo_donchian", frame).normalized_score == pytest.approx(1.0)


def test_trend_volume_breakout_confirmation() -> None:
    frame = bars_from_returns(np.concatenate([0.0015 + _NOISE[:-1], [0.05]]))
    frame.loc[frame.index[-1], "volume"] = frame["volume"].iloc[-60:-1].mean() * 3
    assert sig("bo_trend_volume", frame).normalized_score == pytest.approx(1.0)
    frame.loc[frame.index[-1], "volume"] = frame["volume"].iloc[-60:-1].mean() * 0.5
    assert sig("bo_trend_volume", frame).normalized_score == pytest.approx(0.6)
    frame["volume"] = 0.0  # synthetic history has no volume
    unavailable = sig("bo_trend_volume", frame)
    assert unavailable.normalized_score == pytest.approx(0.6)
    assert "volume unavailable" in unavailable.reason
    crash = bars_from_returns(np.concatenate([-0.0015 + _NOISE[:-1], [-0.05]]))
    assert sig("bo_trend_volume", crash).normalized_score in (-0.6, -1.0)


def test_momentum_confirmed_breakout_cuts_unconfirmed() -> None:
    rets = np.concatenate(
        [np.full(N - 25, -0.002), np.full(25, 0.006)]
    )  # sharp rebound in downtrend
    s = sig("bo_momentum_confirmed", bars_from_returns(rets), params={"mom_window": 200})
    assert "does not confirm" in s.reason
    assert s.normalized_score == pytest.approx(0.25 * s.raw_score)


# ------------------------------------------------------------------ mean reversion
def _dip_in_uptrend(days: int = 3, size: float = -0.03):  # type: ignore[no-untyped-def]
    return bars_from_returns(np.concatenate([np.full(N - days, 0.0015), np.full(days, size)]))


def test_rsi_dip_buys_only_in_uptrend() -> None:
    s = sig("mr_rsi_dip", _dip_in_uptrend())
    assert s.direction is SignalDirection.BULLISH
    assert "buy the dip" in s.reason
    assert sig("mr_rsi_dip", DOWN).normalized_score == 0.0  # oversold but downtrend
    assert "below the long SMA" in sig("mr_rsi_dip", DOWN).reason


def test_drawdown_dip() -> None:
    s = sig("mr_drawdown_dip", _dip_in_uptrend(days=3, size=-0.04))
    assert s.direction is SignalDirection.BULLISH
    assert s.indicator_values["depth_atr"] >= 2.0
    assert sig("mr_drawdown_dip", UP).normalized_score == 0.0


def test_extension_trim_reduces_but_never_shorts() -> None:
    rets = np.concatenate([np.full(N - 15, 0.0005), np.full(15, 0.012)])  # parabolic spike
    s = sig("dmr_extension_trim", bars_from_returns(rets))
    assert s.direction is SignalDirection.BEARISH
    assert s.suggested_exposure == 0.0  # trims to cash, never short
    assert "trim toward cash" in s.reason
    assert sig("dmr_extension_trim", FLAT).normalized_score == 0.0


def test_bollinger_extension_is_contrarian() -> None:
    assert sig("ext_bollinger_percent_b", UP).normalized_score < 0
    assert sig("ext_bollinger_percent_b", DOWN).normalized_score > 0
    assert sig("ext_bollinger_percent_b", UP).suggested_exposure == 0.0


# ------------------------------------------------------------------ regimes
def test_regime_classification_boundaries() -> None:
    assert classify(0.10, 0.25, 0.75, 0.95) == 0
    assert classify(0.25, 0.25, 0.75, 0.95) == 1
    assert classify(0.80, 0.25, 0.75, 0.95) == 2
    assert classify(0.99, 0.25, 0.75, 0.95) == 3


def test_vol_regime_after_volatility_spike() -> None:
    rng = np.random.default_rng(3)
    rets = np.concatenate([rng.normal(0, 0.005, N - 20), rng.normal(0, 0.05, 20)])
    s = sig("vol_regime", bars_from_returns(rets))
    assert s.indicator_values["regime_code"] == 3.0
    assert s.normalized_score == pytest.approx(-0.75)
    assert "extreme" in s.reason
    calm = np.concatenate([rng.normal(0, 0.03, N - 40), rng.normal(0, 0.002, 40)])
    assert sig("vol_regime", bars_from_returns(calm)).indicator_values["regime_code"] == 0.0


def test_trend_vol_filter_damps_bullish_in_extreme_vol() -> None:
    rng = np.random.default_rng(4)
    rets = np.concatenate([np.full(N - 20, 0.002), 0.002 + rng.normal(0, 0.04, 20)])
    frame = bars_from_returns(rets)
    filtered = sig("trend_vol_filter", frame)
    if filtered.indicator_values["regime_code"] == 3.0:
        assert filtered.normalized_score == 0.0
    plain = sig("ltt_sma_distance", frame)
    assert filtered.normalized_score <= plain.normalized_score + 1e-12


def test_drawdown_aware_damping() -> None:
    rets = np.concatenate([np.full(N - 60, 0.003), np.full(60, -0.0025)])  # 1y high, then -14%
    s = sig("dd_aware_trend", bars_from_returns(rets))
    assert 0 < s.indicator_values["damping"] < 1
    assert s.normalized_score == pytest.approx(s.raw_score * s.indicator_values["damping"])


def test_regime_transition_detects_fresh_breaks() -> None:
    down_break = np.concatenate([np.full(N - 8, 0.001), np.full(8, -0.02)])
    s = sig("regime_transition", bars_from_returns(down_break))
    assert s.direction is SignalDirection.BEARISH
    assert "fresh break below" in s.reason
    up_break = np.concatenate([np.full(N - 8, -0.001), np.full(8, 0.02)])
    assert (
        sig("regime_transition", bars_from_returns(up_break)).direction is SignalDirection.BULLISH
    )
    assert sig("regime_transition", UP).normalized_score == 0.0  # long-established trend


# ------------------------------------------------------------------ benchmarks
def test_baselines() -> None:
    hold = sig("baseline_buy_hold", DOWN)
    cash = sig("baseline_cash", UP)
    assert (hold.normalized_score, hold.suggested_exposure) == (1.0, 1.0)
    assert (cash.normalized_score, cash.suggested_exposure) == (0.0, 0.0)


def test_undefined_inputs_fail_closed() -> None:
    """Zero volatility makes z-scores undefined: the strategy refuses instead of guessing."""
    from adaptive_quant.core.errors import StrategyError

    frozen_price = bars_from_returns(np.zeros(N))
    with pytest.raises(StrategyError, match="undefined"):
        sig("dmr_extension_trim", frozen_price)
