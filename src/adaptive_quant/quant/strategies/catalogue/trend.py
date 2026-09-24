"""Trend-following candidates (long-term, intermediate, short-term)."""

from __future__ import annotations

from adaptive_quant.core.enums import StrategyFamily
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.strategies.base import Evaluation, Strategy, StrategyContext
from adaptive_quant.quant.strategies.params import ParamSpec
from adaptive_quant.quant.strategies.registry import register
from adaptive_quant.quant.strategies.scoring import pct, sign, squash


@register
class LongTermTrendSmaDistance(Strategy):
    """Price relative to a long SMA; optionally confirmed by a second index (NDX)."""

    implementation = "ltt_sma_distance"
    family = StrategyFamily.LONG_TERM_TREND
    description = "tanh((price / SMA(n) - 1) / scale); halved if the confirm symbol disagrees"
    param_specs = (
        ParamSpec("window", int, 200, 20, 400, description="SMA length (test 200-260)"),
        ParamSpec("scale", float, 0.05, 0.005, 0.5, description="distance giving score ~0.76"),
        ParamSpec("confirm_symbol", str, "", description="optional confirming symbol, e.g. NDX"),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec("distance_from_ma", {"window": self.p_int("window")}, name="distance")
        ]

    @property
    def optional_symbols(self) -> tuple[str, ...]:
        s = self.p_str("confirm_symbol")
        return (s,) if s else ()

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        d = ctx.values["distance"]
        score = squash(d, self.p_float("scale"))
        w = self.p_int("window")
        reason = f"{self.signal_symbol} {pct(d)} vs SMA({w})"
        for sym, data in ctx.optional.items():
            if data.values is None:
                reason += f"; {sym} unavailable, not used"
            elif sign(data.values["distance"]) != sign(d):
                score *= 0.5
                reason += f"; {sym} disagrees ({pct(data.values['distance'])}), score halved"
            else:
                reason += f"; {sym} confirms ({pct(data.values['distance'])})"
        return Evaluation(score, reason, raw_score=d)


@register
class LongTermTrendSlope(Strategy):
    implementation = "ltt_trend_slope"
    family = StrategyFamily.LONG_TERM_TREND
    description = "annualized log-price regression slope; confidence = fit R²"
    param_specs = (
        ParamSpec("window", int, 200, 20, 400),
        ParamSpec("scale", float, 0.25, 0.01, 2.0, description="annual log slope giving ~0.76"),
    )

    def indicators(self) -> list[IndicatorSpec]:
        w = self.p_int("window")
        return [
            IndicatorSpec("trend_slope", {"window": w}, name="slope"),
            IndicatorSpec("trend_r2", {"window": w}, name="r2"),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        slope, r2 = ctx.values["slope"], ctx.values["r2"]
        score = squash(slope, self.p_float("scale"))
        return Evaluation(
            score,
            f"{self.p_int('window')}-bar trend {pct(slope)}/yr (log), fit R² {r2:.2f}",
            confidence=r2,
            raw_score=slope,
        )


@register
class IntermediateMaStack(Strategy):
    implementation = "it_ma_stack"
    family = StrategyFamily.INTERMEDIATE_TREND
    description = "votes: price vs fast, fast vs mid, mid vs slow (dead band); score = mean vote"
    param_specs = (
        ParamSpec("fast", int, 20, 5, 100),
        ParamSpec("mid", int, 50, 10, 200),
        ParamSpec("slow", int, 100, 20, 300),
        ParamSpec(
            "band",
            float,
            0.002,
            0.0,
            0.05,
            description="relative gap below which a comparison votes 0 (avoids noise flips)",
        ),
    )

    def validate_params(self) -> None:
        if not self.p_int("fast") < self.p_int("mid") < self.p_int("slow"):
            raise ValueError("require fast < mid < slow")

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec("sma", {"window": self.p_int(k)}, name=k) for k in ("fast", "mid", "slow")
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        v, band = ctx.values, self.p_float("band")

        def vote(a: float, b: float) -> float:
            gap = a / b - 1.0
            return 0.0 if abs(gap) <= band else sign(gap)

        votes = [vote(ctx.close, v["fast"]), vote(v["fast"], v["mid"]), vote(v["mid"], v["slow"])]
        score = sum(votes) / 3.0
        labels = ["price>fast", "fast>mid", "mid>slow"]
        up = [lab for lab, x in zip(labels, votes, strict=True) if x > 0]
        down = [lab.replace(">", "<") for lab, x in zip(labels, votes, strict=True) if x < 0]
        return Evaluation(
            score,
            f"MA stack {self.p_int('fast')}/{self.p_int('mid')}/{self.p_int('slow')}: "
            f"bullish [{', '.join(up) or '-'}], bearish [{', '.join(down) or '-'}]",
        )


@register
class ShortTermEmaCross(Strategy):
    implementation = "st_ema_cross"
    family = StrategyFamily.SHORT_TERM_TREND
    description = "tanh((EMA fast / EMA slow - 1) / scale)"
    param_specs = (
        ParamSpec("fast", int, 10, 2, 50),
        ParamSpec("slow", int, 30, 5, 150),
        ParamSpec("scale", float, 0.02, 0.001, 0.2),
    )

    def validate_params(self) -> None:
        if not self.p_int("fast") < self.p_int("slow"):
            raise ValueError("require fast < slow")

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec("ema", {"window": self.p_int("fast")}, name="fast"),
            IndicatorSpec("ema", {"window": self.p_int("slow")}, name="slow"),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        spread = ctx.values["fast"] / ctx.values["slow"] - 1.0
        return Evaluation(
            squash(spread, self.p_float("scale")),
            f"EMA({self.p_int('fast')}) {pct(spread)} vs EMA({self.p_int('slow')})",
            raw_score=spread,
            extra={"ema_spread": spread},
        )
