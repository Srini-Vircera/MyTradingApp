"""Volatility-regime, trend+volatility, drawdown-aware and regime-transition candidates."""

from __future__ import annotations

from adaptive_quant.core.enums import StrategyFamily
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.strategies.base import Evaluation, Strategy, StrategyContext
from adaptive_quant.quant.strategies.params import ParamSpec
from adaptive_quant.quant.strategies.registry import register
from adaptive_quant.quant.strategies.scoring import clip, pct, squash

REGIMES = ("low", "normal", "elevated", "extreme")

_REGIME_PARAMS = (
    ParamSpec("vol_window", int, 20, 5, 126),
    ParamSpec("vol_lookback", int, 252, 60, 1260),
    ParamSpec("low_below", float, 0.25, 0.01, 0.99, description="percentile boundary low/normal"),
    ParamSpec("elevated_above", float, 0.75, 0.01, 0.99),
    ParamSpec("extreme_above", float, 0.95, 0.01, 0.999),
)


def classify(percentile: float, low: float, elevated: float, extreme: float) -> int:
    """0 low, 1 normal, 2 elevated, 3 extreme (boundaries mirror risk.yaml)."""
    if percentile > extreme:
        return 3
    if percentile > elevated:
        return 2
    if percentile < low:
        return 0
    return 1


class _RegimeMixin(Strategy):
    def validate_params(self) -> None:
        if (
            not self.p_float("low_below")
            < self.p_float("elevated_above")
            < self.p_float("extreme_above")
        ):
            raise ValueError("require low_below < elevated_above < extreme_above")

    def _vol_spec(self) -> IndicatorSpec:
        return IndicatorSpec(
            "volatility_percentile",
            {"vol_window": self.p_int("vol_window"), "lookback": self.p_int("vol_lookback")},
            name="vol_pct",
        )

    def _regime(self, pct_value: float) -> int:
        return classify(
            pct_value,
            self.p_float("low_below"),
            self.p_float("elevated_above"),
            self.p_float("extreme_above"),
        )


@register
class VolatilityRegime(_RegimeMixin):
    """Risk-appetite signal for sizing: calm markets allow more exposure."""

    implementation = "vol_regime"
    family = StrategyFamily.VOLATILITY_REGIME
    description = "maps the realized-volatility regime to a risk-appetite score"
    param_specs = (
        *_REGIME_PARAMS,
        ParamSpec("low_score", float, 0.5, -1.0, 1.0),
        ParamSpec("normal_score", float, 0.25, -1.0, 1.0),
        ParamSpec("elevated_score", float, -0.25, -1.0, 1.0),
        ParamSpec("extreme_score", float, -0.75, -1.0, 1.0),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [self._vol_spec()]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        p = ctx.values["vol_pct"]
        code = self._regime(p)
        score = self.p_float(f"{REGIMES[code]}_score")
        return Evaluation(
            score,
            f"volatility regime {REGIMES[code]} "
            f"({p:.0%} percentile of {self.p_int('vol_lookback')}d)",
            confidence=1.0,
            raw_score=p,
            extra={"regime_code": float(code)},
        )


@register
class TrendWithVolatilityFilter(_RegimeMixin):
    implementation = "trend_vol_filter"
    family = StrategyFamily.TREND_PLUS_VOLATILITY
    description = "long-term trend score, bullish side damped in elevated/extreme volatility"
    param_specs = (
        *_REGIME_PARAMS,
        ParamSpec("window", int, 200, 20, 400),
        ParamSpec("scale", float, 0.05, 0.005, 0.5),
        ParamSpec("elevated_multiplier", float, 0.5, 0.0, 1.0),
        ParamSpec("extreme_multiplier", float, 0.0, 0.0, 1.0),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec("distance_from_ma", {"window": self.p_int("window")}, name="distance"),
            self._vol_spec(),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        d, p = ctx.values["distance"], ctx.values["vol_pct"]
        trend = squash(d, self.p_float("scale"))
        code = self._regime(p)
        mult = {2: self.p_float("elevated_multiplier"), 3: self.p_float("extreme_multiplier")}.get(
            code, 1.0
        )
        score = trend * mult if trend > 0 else trend
        return Evaluation(
            score,
            f"trend {pct(d)} vs SMA({self.p_int('window')}); "
            f"volatility {REGIMES[code]} -> bullish x{mult:g}",
            raw_score=trend,
            extra={"regime_code": float(code)},
        )


@register
class DrawdownAwareTrend(Strategy):
    implementation = "dd_aware_trend"
    family = StrategyFamily.DRAWDOWN_AWARE
    description = "bullish trend score damped linearly as the 1-year drawdown deepens"
    param_specs = (
        ParamSpec("window", int, 200, 20, 400),
        ParamSpec("scale", float, 0.05, 0.005, 0.5),
        ParamSpec(
            "dd_window",
            int,
            252,
            20,
            1260,
            description="peak window (avoids history-start dependence)",
        ),
        ParamSpec(
            "dd_limit", float, 0.30, 0.05, 0.8, description="drawdown at which bullishness is zero"
        ),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec("distance_from_ma", {"window": self.p_int("window")}, name="distance"),
            IndicatorSpec("drawdown", {"window": self.p_int("dd_window")}, name="dd"),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        d, dd = ctx.values["distance"], ctx.values["dd"]
        trend = squash(d, self.p_float("scale"))
        damp = clip(1.0 - abs(dd) / self.p_float("dd_limit"), 0.0, 1.0)
        score = trend * damp if trend > 0 else trend
        return Evaluation(
            score,
            f"trend {pct(d)} vs SMA; drawdown {pct(dd)} from "
            f"{self.p_int('dd_window')}d peak -> bullish x{damp:.2f}",
            raw_score=trend,
            extra={"damping": damp},
        )


@register
class RegimeTransition(_RegimeMixin):
    """Reacts to *fresh* trend flips, stronger when volatility confirms."""

    implementation = "regime_transition"
    family = StrategyFamily.REGIME_TRANSITION
    description = "recent cross of the long SMA, amplified by the change in volatility"
    param_specs = (
        *_REGIME_PARAMS,
        ParamSpec("window", int, 200, 20, 400),
        ParamSpec("lookback", int, 10, 2, 63, description="bars within which a cross is 'fresh'"),
    )

    def extra_history_bars(self) -> int:
        return self.p_int("lookback")

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec("distance_from_ma", {"window": self.p_int("window")}, name="distance"),
            self._vol_spec(),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        k = self.p_int("lookback")
        recent = ctx.frame.iloc[-(k + 1) :]  # point-in-time: rows <= as_of only
        dist, vol = recent["distance"].to_numpy(), recent["vol_pct"].to_numpy()
        now, before = float(dist[-1]), dist[:-1]
        vol_change = float(vol[-1] - vol[0])
        if now < 0 and (before > 0).any():
            score = -clip(0.5 + max(0.0, vol_change), 0.5, 1.0)
            text = f"fresh break below SMA within {k}d; volatility {vol_change:+.0%} pts"
        elif now > 0 and (before < 0).any():
            score = clip(0.5 + max(0.0, -vol_change), 0.5, 1.0)
            text = f"fresh break above SMA within {k}d; volatility {vol_change:+.0%} pts"
        else:
            return Evaluation(
                0.0, f"no trend transition in the last {k}d ({pct(now)} vs SMA)", confidence=0.0
            )
        return Evaluation(score, text, extra={"vol_change": vol_change})
