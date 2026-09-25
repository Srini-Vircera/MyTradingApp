"""Contract every registered strategy must satisfy (default parameters)."""

import re
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.core.enums import SignalDirection, StrategyFamily
from adaptive_quant.core.errors import InsufficientHistoryError, MissingDataError
from adaptive_quant.core.models import direction_for_score
from adaptive_quant.quant.data.view import MarketDataView
from adaptive_quant.quant.strategies.base import Strategy
from adaptive_quant.quant.strategies.registry import create, registry
from tests.unit.strategies.helpers import default_strategies, ids, random_walk, view_at

QQQ = random_walk(800, seed=7)
NDX = random_walk(800, seed=8) * 1.0
FRAMES = {"QQQ": QQQ, "NDX": NDX}
STRATEGIES = default_strategies()
CHECK_ROWS = [300, 450, 620, 799]
SCENARIOS = {
    "up": random_walk(800, drift=0.002, vol=0.008, seed=11),
    "down": random_walk(800, drift=-0.002, vol=0.012, seed=12),
    "chop": random_walk(800, drift=0.0, vol=0.025, seed=13),
}


def test_registry_covers_every_family() -> None:
    families = {cls.family for cls in registry().values()}
    assert families == set(StrategyFamily)
    assert len(registry()) >= 20


@pytest.mark.parametrize("strategy", STRATEGIES, ids=ids)
class TestContract:
    def test_signal_fields_are_valid(self, strategy: Strategy) -> None:
        for i in CHECK_ROWS:
            view = view_at(FRAMES, i)
            sig = strategy.generate_signal(view)
            assert -1.0 <= sig.normalized_score <= 1.0
            assert 0.0 <= sig.confidence <= 1.0
            assert sig.direction is direction_for_score(sig.normalized_score)
            long_max = strategy.p_float("max_long_exposure")
            short_max = strategy.p_float("max_short_exposure")
            assert -short_max - 1e-9 <= sig.suggested_exposure <= long_max + 1e-9
            assert sig.strategy_name == strategy.strategy_id
            assert sig.strategy_version == strategy.version_id
            assert sig.timestamp == view.as_of
            assert sig.data_timestamp <= sig.timestamp
            assert "close" in sig.indicator_values

    def test_reason_is_readable(self, strategy: Strategy) -> None:
        for frame in SCENARIOS.values():
            sig = strategy.generate_signal(view_at({"QQQ": frame}, 700))
            assert 10 <= len(sig.reason) <= 300
            assert "nan" not in sig.reason.lower()
            assert not re.search(r"\binf\b", sig.reason.lower())

    def test_deterministic(self, strategy: Strategy) -> None:
        view = view_at(FRAMES, 500)
        assert strategy.generate_signal(view) == strategy.generate_signal(view)
        clone = create(strategy.implementation, strategy.strategy_id, dict(strategy.params))
        assert clone.generate_signal(view) == strategy.generate_signal(view)

    def test_future_data_cannot_change_the_signal(self, strategy: Strategy) -> None:
        for i in (300, 500, 700):
            base = strategy.generate_signal(view_at(FRAMES, i))
            poisoned = {}
            for sym, df in FRAMES.items():
                p = df.copy()
                after = p.index > df.index[i]
                p.loc[after, ["open", "high", "low", "close"]] *= 3.0
                p.loc[after, "volume"] *= 50.0
                poisoned[sym] = p
            assert strategy.generate_signal(view_at(poisoned, i)) == base
            truncated = {sym: df.iloc[: i + 1] for sym, df in FRAMES.items()}
            assert strategy.generate_signal(view_at(truncated, i)) == base

    def test_intraday_as_of_never_uses_todays_bar(self, strategy: Strategy) -> None:
        close_i = QQQ.index[600].to_pydatetime()
        intraday = close_i - timedelta(minutes=15)  # 15 minutes before today's close
        sig = strategy.generate_signal(MarketDataView(FRAMES, intraday))
        assert sig.data_timestamp == QQQ.index[599].to_pydatetime()
        expected = strategy.generate_signal(MarketDataView(FRAMES, QQQ.index[599].to_pydatetime()))
        assert sig.normalized_score == expected.normalized_score

    def test_warmup_is_exact(self, strategy: Strategy) -> None:
        n = strategy.warmup_bars
        just_enough = {"QQQ": QQQ.iloc[:n]}
        strategy.generate_signal(view_at(just_enough, n - 1))
        if n > 1:
            too_few = {"QQQ": QQQ.iloc[: n - 1]}
            with pytest.raises(InsufficientHistoryError, match="needs"):
                strategy.generate_signal(view_at(too_few, n - 2))

    def test_optional_ndx_missing_or_present(self, strategy: Strategy) -> None:
        without = strategy.generate_signal(view_at({"QQQ": QQQ}, 700))
        with_ndx = strategy.generate_signal(view_at(FRAMES, 700))
        short_ndx = strategy.generate_signal(view_at({"QQQ": QQQ, "NDX": NDX.iloc[:50]}, 700))
        for sig in (without, with_ndx, short_ndx):
            assert -1 <= sig.normalized_score <= 1

    def test_missing_signal_symbol_raises(self, strategy: Strategy) -> None:
        with pytest.raises(MissingDataError):
            strategy.generate_signal(view_at({"NDX": NDX}, 700, symbol="NDX"))

    def test_long_short_restrictions_hold_in_all_scenarios(self, strategy: Strategy) -> None:
        for frame in SCENARIOS.values():
            for i in range(400, 800, 40):
                sig = strategy.generate_signal(view_at({"QQQ": frame}, i))
                if not strategy.can_short:
                    assert sig.suggested_exposure >= 0.0
                if not strategy.can_long:
                    assert sig.normalized_score <= 0.0
                    assert sig.direction is not SignalDirection.BULLISH


def test_signal_indicator_values_are_finite() -> None:
    for strategy in STRATEGIES:
        sig = strategy.generate_signal(view_at(FRAMES, 700))
        vals = [v for v in sig.indicator_values.values() if v is not None]
        assert np.isfinite(vals).all(), strategy.strategy_id


def test_frames_are_not_mutated_by_strategies() -> None:
    before = {k: v.copy() for k, v in FRAMES.items()}
    for strategy in STRATEGIES:
        strategy.generate_signal(view_at(FRAMES, 700))
    for k, v in FRAMES.items():
        pd.testing.assert_frame_equal(v, before[k])
