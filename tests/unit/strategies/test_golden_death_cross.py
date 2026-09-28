"""Golden Cross / Death Cross: regime vs exactly-once cross events, SMA/EMA, warm-up,
validation, point-in-time behaviour. Generated data only (never real prices)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.core.errors import DataQualityError, InsufficientHistoryError, StrategyError
from adaptive_quant.quant.data.bars import make_bars
from adaptive_quant.quant.data.view import MarketDataView
from adaptive_quant.quant.indicators.trend import ema, sma
from adaptive_quant.quant.strategies.catalogue.crossover import (
    DEATH,
    GOLDEN,
    GoldenDeathCross,
    cross_history,
)
from tests.data_helpers import calendar
from tests.unit.strategies.helpers import random_walk


def closes_to_bars(
    closes: Sequence[float] | np.ndarray, start: date = date(2018, 1, 2)
) -> pd.DataFrame:
    c = [float(x) for x in closes]
    sessions = calendar().sessions_in_range(start, start + timedelta(days=int(len(c) * 1.6) + 30))
    ts = [s.close for s in sessions[: len(c)]]
    return make_bars(ts, open=c, high=c, low=c, close=c, volume=[1e6] * len(c))


def signal(strategy: GoldenDeathCross, bars: pd.DataFrame, i: int = -1):  # type: ignore[no-untyped-def]
    as_of = bars.index[i].to_pydatetime()
    return strategy.generate_signal(MarketDataView({"QQQ": bars}, as_of))


def small(**kw: object) -> GoldenDeathCross:
    return GoldenDeathCross(params={"fast_period": 2, "slow_period": 3, **kw})


# ------------------------------------------------------------------ pure cross logic
def ser(values: Sequence[float]) -> pd.Series:
    return pd.Series(values, dtype=float)


@pytest.mark.parametrize(
    ("fast", "slow", "events", "regime"),
    [
        # below -> above: one Golden Cross on the "above" bar
        ([1, 1, 3, 3], [2, 2, 2, 2], [0, 0, GOLDEN, 0], [-1, -1, 1, 1]),
        # above -> below: one Death Cross
        ([3, 3, 1, 1], [2, 2, 2, 2], [0, 0, DEATH, 0], [1, 1, -1, -1]),
        # below -> equal -> above: exactly one Golden Cross, on the "above" bar
        ([1, 2, 3], [2, 2, 2], [0, 0, GOLDEN], [-1, -1, 1]),
        # above -> equal -> below: exactly one Death Cross, on the "below" bar
        ([3, 2, 1], [2, 2, 2], [0, 0, DEATH], [1, 1, -1]),
        # above -> equal -> above: no transition, no event, regime stays bullish
        ([3, 2, 3], [2, 2, 2], [0, 0, 0], [1, 1, 1]),
        # equal from the start: neutral until the averages differ; first regime is not a cross
        ([2, 2, 3, 1], [2, 2, 2, 2], [0, 0, 0, DEATH], [0, 0, 1, -1]),
        # warm-up (NaN) rows: no regime, no event
        ([np.nan, np.nan, 1, 3], [np.nan, np.nan, 2, 2], [0, 0, 0, GOLDEN], [0, 0, -1, 1]),
    ],
)
def test_cross_events_happen_exactly_once_per_transition(
    fast: list[float], slow: list[float], events: list[int], regime: list[int]
) -> None:
    h = cross_history(ser(fast), ser(slow))
    assert h.events.tolist() == events
    assert h.regime.tolist() == regime


def test_repeated_days_in_the_same_regime_report_no_new_cross() -> None:
    fast = ser([1] * 5 + [3] * 50 + [1] * 5)
    slow = ser([2] * 60)
    h = cross_history(fast, slow)
    assert int((h.events == GOLDEN).sum()) == 1 and int((h.events == DEATH).sum()) == 1
    assert (h.regime[5:55] == 1).all() and (h.regime[55:] == -1).all()


# ------------------------------------------------------------------ the strategy
def test_golden_and_death_cross_through_the_strategy() -> None:
    # SMA(2)/SMA(3): rising prices -> fast above slow; falling -> fast below
    closes = [10, 10, 10, 9, 8, 7, 8, 10, 12, 12, 11, 9, 7]
    bars = closes_to_bars(closes)
    st = small()
    fast, slow = sma(bars["close"], 2), sma(bars["close"], 3)
    expected = cross_history(fast, slow)
    seen_events = []
    for i in range(st.warmup_bars - 1, len(bars)):
        sig = signal(st, bars, i)
        ev = int(sig.indicator_values["cross_event"])
        seen_events.append(ev)
        assert ev == expected.events[i]
        assert int(sig.indicator_values["regime"]) == expected.regime[i]
        assert sig.indicator_values["fast_ma"] == pytest.approx(fast.iloc[i])
        assert sig.indicator_values["slow_ma"] == pytest.approx(slow.iloc[i])
        if expected.regime[i] > 0:
            assert sig.normalized_score == 1.0 and sig.suggested_exposure == 1.0
        elif expected.regime[i] < 0:
            assert sig.normalized_score == -1.0 and sig.suggested_exposure == 0.0  # cash
    assert GOLDEN in seen_events and DEATH in seen_events
    assert seen_events.count(GOLDEN) == int((expected.events == GOLDEN).sum())


def test_reason_names_regime_and_most_recent_cross() -> None:
    bars = closes_to_bars([10, 10, 10, 9, 8, 7, 8, 10, 12, 12, 12])
    sig = signal(small(), bars)
    assert "bullish regime" in sig.reason and "most recent: Golden Cross on" in sig.reason
    assert "(variant)" in sig.reason  # 2/3 is not the classic 50/200
    assert sig.indicator_values["last_cross"] == GOLDEN


def test_default_is_classic_50_200_sma_long_qqq_or_cash() -> None:
    st = GoldenDeathCross()
    assert (st.p_int("fast_period"), st.p_int("slow_period"), st.p_str("ma_type")) == (
        50, 200, "SMA"
    )  # fmt: skip
    assert st.is_classic and st.label == "Golden/Death Cross 50/200 SMA"
    assert st.p_str("bearish_action") == "cash" and st.p_float("max_short_exposure") == 0.0
    assert st.p_float("max_long_exposure") == 1.0  # QQQ, never leveraged by default
    assert st.warmup_bars == 201  # 200 for the slow SMA + 1 to know the previous day


def test_50_200_sma_values_match_a_plain_rolling_mean() -> None:
    bars = random_walk(600, seed=3)
    sig = signal(GoldenDeathCross(), bars)
    c = bars["close"]
    assert sig.indicator_values["fast_ma"] == pytest.approx(c.iloc[-50:].mean())
    assert sig.indicator_values["slow_ma"] == pytest.approx(c.iloc[-200:].mean())


def test_ema_option_uses_the_sma_seeded_ema() -> None:
    bars = random_walk(600, seed=4)
    st = GoldenDeathCross(params={"ma_type": "EMA"})
    sig = signal(st, bars)
    assert sig.indicator_values["fast_ma"] == pytest.approx(ema(bars["close"], 50).iloc[-1])
    assert sig.indicator_values["slow_ma"] == pytest.approx(ema(bars["close"], 200).iloc[-1])
    assert st.warmup_bars == 201


def test_configurable_periods() -> None:
    bars = random_walk(400, seed=5)
    st = GoldenDeathCross(params={"fast_period": 20, "slow_period": 100})
    sig = signal(st, bars)
    assert sig.indicator_values["slow_ma"] == pytest.approx(bars["close"].iloc[-100:].mean())
    assert st.warmup_bars == 101 and not st.is_classic


def test_insufficient_history_produces_no_signal() -> None:
    bars = random_walk(400, seed=6)
    st = GoldenDeathCross()
    with pytest.raises(InsufficientHistoryError):
        signal(st, bars, 199)  # 200 bars: the slow SMA exists but not yesterday's
    sig = signal(st, bars, 200)  # 201 bars: the first valid signal
    assert sig.data_timestamp == bars.index[200].to_pydatetime()


@pytest.mark.parametrize(
    "params",
    [
        {"fast_period": 1},
        {"fast_period": 200, "slow_period": 200},
        {"fast_period": 200, "slow_period": 50},
        {"slow_period": 401},
        {"ma_type": "WMA"},
        {"ma_type": "sma"},
        {"bearish_action": "tqqq"},
        {"bearish_action": "sqqq"},  # needs an explicit max_short_exposure
        {"max_short_exposure": 0.5},  # only with bearish_action=sqqq
        {"bearish_action": "qqq_reduced", "reduced_exposure": 1.5},
        {"fast_period": 50.5},
        {"unknown": 1},
    ],
)
def test_invalid_parameters_fail_closed(params: dict[str, object]) -> None:
    with pytest.raises(StrategyError, match="invalid parameters"):
        GoldenDeathCross(params=params)


def test_bearish_behaviours_are_explicit_and_bounded() -> None:
    bars = closes_to_bars([12, 12, 12, 11, 10, 9, 8, 7])  # falling: bearish regime
    reduced = signal(small(bearish_action="qqq_reduced", reduced_exposure=0.3), bars)
    assert reduced.normalized_score == -1.0 and reduced.suggested_exposure == pytest.approx(0.3)
    inverse = signal(small(bearish_action="sqqq", max_short_exposure=0.2), bars)
    assert inverse.suggested_exposure == pytest.approx(-0.2)
    cash = signal(small(), bars)
    assert cash.suggested_exposure == 0.0 and cash.direction.value == "bearish"


# ------------------------------------------------------------------ point in time
def test_changing_tomorrow_does_not_change_today() -> None:
    bars = random_walk(420, seed=8)
    st = GoldenDeathCross()
    i = 350
    before = signal(st, bars, i)
    shocked = bars.copy()
    shocked.iloc[i + 1 :, :4] *= 3.0  # a huge jump after the decision point
    after = signal(st, shocked, i)
    assert before.model_dump() == after.model_dump()
    assert before.data_timestamp <= before.timestamp


def test_backtest_path_equals_live_path() -> None:
    bars = random_walk(500, seed=9)
    st = GoldenDeathCross()
    pre = st.precompute({"QQQ": bars})
    for i in (200, 260, 499):
        as_of = bars.index[i].to_pydatetime()
        assert st.signal_at(pre, as_of).model_dump() == signal(st, bars, i).model_dump()


def test_deterministic() -> None:
    bars = random_walk(450, seed=10)
    a, b = signal(GoldenDeathCross(), bars), signal(GoldenDeathCross(), bars)
    assert a.model_dump() == b.model_dump()
    assert GoldenDeathCross().version_id == GoldenDeathCross().version_id


# ------------------------------------------------------------------ data edge cases
def test_gap_in_prices_fails_closed() -> None:
    bars = random_walk(260, seed=11)
    broken = bars.copy()
    broken.loc[broken.index[230], "close"] = np.nan  # a missing close
    with pytest.raises((StrategyError, ValueError)):
        signal(GoldenDeathCross(), broken)


def test_duplicate_bars_are_rejected() -> None:
    bars = random_walk(260, seed=12)
    dup = pd.concat([bars.iloc[:100], bars.iloc[99:]])
    with pytest.raises(DataQualityError, match="without duplicates"):
        signal(GoldenDeathCross(), dup)


def test_non_trading_days_do_not_count_as_bars() -> None:
    """Bars exist only on sessions: 'bars since cross' counts sessions, not calendar days."""
    bars = closes_to_bars([10, 10, 10, 9, 8, 7, 8, 10, 12, 12, 12, 12])
    sig = signal(small(), bars)
    last_cross = int(
        np.flatnonzero(cross_history(sma(bars["close"], 2), sma(bars["close"], 3)).events)[-1]
    )
    assert sig.indicator_values["bars_since_cross"] == len(bars) - 1 - last_cross
