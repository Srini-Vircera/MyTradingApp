from datetime import UTC, datetime

import pytest

from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.enums import StrategyFamily
from adaptive_quant.core.errors import StrategyError
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.strategies.base import Evaluation, Strategy, StrategyContext
from adaptive_quant.quant.strategies.params import ParamSpec
from adaptive_quant.quant.strategies.registry import create
from adaptive_quant.quant.strategies.runner import run_strategies
from adaptive_quant.trading.safety.preflight import PreflightGate, RefusalReason
from adaptive_quant.trading.safety.signal_checks import SignalGenerationCheck
from tests.unit.strategies.helpers import random_walk, view_at

FRAMES = {"QQQ": random_walk(400)}
CLOCK = FrozenClock(datetime(2024, 1, 2, tzinfo=UTC))


class Exploding(Strategy):
    implementation = "test_exploding"
    family = StrategyFamily.BENCHMARK
    description = "raises"

    def indicators(self) -> list[IndicatorSpec]:
        return []

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        raise RuntimeError("bug in strategy")


class OutOfRange(Exploding):
    implementation = "test_out_of_range"

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        return Evaluation(1.7, "too strong")


class EmptyReason(Exploding):
    implementation = "test_empty_reason"

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        return Evaluation(0.1, "   ")


class TooMuchExposure(Exploding):
    implementation = "test_too_much"

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        return Evaluation(0.5, "suggests 2x", exposure=2.0)


def test_all_good_batch() -> None:
    batch = run_strategies([create("baseline_cash"), create("st_ema_cross")], view_at(FRAMES, 399))
    assert batch.ok
    assert [s.strategy_name for s in batch.signals] == ["baseline_cash", "st_ema_cross"]


def test_failures_are_captured_not_raised_and_block_trading() -> None:
    strategies = [
        create("baseline_cash"),
        Exploding(),
        OutOfRange(),
        EmptyReason(),
        TooMuchExposure(),
    ]
    batch = run_strategies(strategies, view_at(FRAMES, 399))
    assert not batch.ok
    assert len(batch.signals) == 1
    assert "RuntimeError: bug in strategy" in batch.failures["test_exploding"]
    assert "outside [-1, 1]" in batch.failures["test_out_of_range"]
    assert "empty reason" in batch.failures["test_empty_reason"]
    assert "exposure 2.0 outside" in batch.failures["test_too_much"]
    result = SignalGenerationCheck(lambda: batch).run()
    assert result.reason is RefusalReason.SIGNAL_FAILURE


def test_not_ready_blocks_trading() -> None:
    batch = run_strategies([create("ltt_sma_distance")], view_at(FRAMES, 50))
    assert not batch.ok
    assert "needs 200" in batch.not_ready["ltt_sma_distance"]
    assert not SignalGenerationCheck(lambda: batch).run().passed


def test_empty_batch_blocks_trading() -> None:
    batch = run_strategies([], view_at(FRAMES, 399))
    assert not batch.ok
    result = SignalGenerationCheck(lambda: batch).run()
    assert "no eligible strategies" in result.detail
    report = PreflightGate([SignalGenerationCheck(lambda: batch)], CLOCK).evaluate()
    assert not report.may_reduce_risk  # signal failure blocks all orders


def test_ok_batch_passes_check() -> None:
    batch = run_strategies([create("baseline_cash")], view_at(FRAMES, 399))
    assert SignalGenerationCheck(lambda: batch).run().passed


class NotFinite(Exploding):
    implementation = "test_not_finite"

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        return Evaluation(float("nan"), "nan score")


class BadConfidence(Exploding):
    implementation = "test_bad_confidence"

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        return Evaluation(0.2, "overconfident", confidence=1.5)


class BearishGoneBullish(Exploding):
    implementation = "test_bearish_bullish"
    can_long = False
    param_specs = (ParamSpec("max_long_exposure", float, 0.0, 0.0, 0.0),)

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        return Evaluation(0.5, "bullish from a bearish-only strategy")


class KeyErrorStrategy(Exploding):
    implementation = "test_key_error"

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        return Evaluation(ctx.values["missing"], "x")


def test_every_invalid_evaluation_fails_closed() -> None:
    strategies = [NotFinite(), BadConfidence(), BearishGoneBullish(), KeyErrorStrategy()]
    batch = run_strategies(strategies, view_at(FRAMES, 399))
    assert not batch.signals
    assert "non-finite" in batch.failures["test_not_finite"]
    assert "confidence 1.5" in batch.failures["test_bad_confidence"]
    assert "bearish-only" in batch.failures["test_bearish_bullish"]
    assert "evaluation failed" in batch.failures["test_key_error"]


def test_bearish_only_and_never_short_are_enforced_at_construction() -> None:
    class LongOnlyShorting(Exploding):
        implementation = "test_long_only"
        can_short = False

    with pytest.raises(StrategyError, match="never shorts"):
        LongOnlyShorting(params={"max_short_exposure": 0.5})

    class BearOnlyLong(Exploding):
        implementation = "test_bear_only"
        can_long = False

    with pytest.raises(StrategyError, match="bearish-only"):
        BearOnlyLong()
