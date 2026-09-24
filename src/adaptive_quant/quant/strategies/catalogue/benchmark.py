"""Baselines. Every candidate must beat these out of sample to be worth anything."""

from __future__ import annotations

from adaptive_quant.core.enums import StrategyFamily
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.strategies.base import Evaluation, Strategy, StrategyContext
from adaptive_quant.quant.strategies.params import ParamSpec
from adaptive_quant.quant.strategies.registry import register


@register
class BuyAndHold(Strategy):
    implementation = "baseline_buy_hold"
    family = StrategyFamily.BENCHMARK
    description = "always fully long the signal symbol (benchmark)"
    param_specs = (ParamSpec("max_short_exposure", float, 0.0, 0.0, 0.0),)
    can_short = False

    def indicators(self) -> list[IndicatorSpec]:
        return []

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        return Evaluation(1.0, f"benchmark: always long {self.signal_symbol}", confidence=1.0)


@register
class AlwaysCash(Strategy):
    implementation = "baseline_cash"
    family = StrategyFamily.BENCHMARK
    description = "always in cash (benchmark)"
    param_specs = (ParamSpec("max_short_exposure", float, 0.0, 0.0, 0.0),)
    can_short = False

    def indicators(self) -> list[IndicatorSpec]:
        return []

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        return Evaluation(0.0, "benchmark: always in cash", confidence=1.0)
