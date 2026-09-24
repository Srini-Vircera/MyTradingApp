"""Momentum, momentum-acceleration and bearish-momentum candidates."""

from __future__ import annotations

import math

from adaptive_quant.core.enums import StrategyFamily
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.strategies.base import Evaluation, Strategy, StrategyContext
from adaptive_quant.quant.strategies.params import ParamSpec, parse_int_list
from adaptive_quant.quant.strategies.registry import register
from adaptive_quant.quant.strategies.scoring import pct, sign, squash


@register
class MultiHorizonMomentum(Strategy):
    _horizons: tuple[int, ...]
    implementation = "mom_multi_horizon"
    family = StrategyFamily.MOMENTUM
    description = "mean volatility-scaled log momentum over several horizons"
    param_specs = (
        ParamSpec("horizons", str, "5,10,20,60,120", description="comma-separated bar counts"),
        ParamSpec("vol_window", int, 60, 10, 252),
        ParamSpec("scale", float, 1.0, 0.1, 5.0, description="mean z giving score ~0.76"),
    )

    def validate_params(self) -> None:
        self._horizons = parse_int_list(self.p_str("horizons"), "horizons", 1, 504)

    def indicators(self) -> list[IndicatorSpec]:
        specs = [IndicatorSpec("momentum", {"window": h}, name=f"mom_{h}") for h in self._horizons]
        specs.append(
            IndicatorSpec("historical_volatility", {"window": self.p_int("vol_window")}, name="vol")
        )
        return specs

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        vol = ctx.values["vol"]
        if vol <= 0:
            return Evaluation(0.0, "zero volatility: momentum not scalable", confidence=0.0)
        zs = {h: ctx.values[f"mom_{h}"] / (vol * math.sqrt(h / 252)) for h in self._horizons}
        mean_z = sum(zs.values()) / len(zs)
        agreement = abs(sum(sign(z) for z in zs.values())) / len(zs)
        parts = ", ".join(f"{h}d {z:+.2f}" for h, z in zs.items())
        return Evaluation(
            squash(mean_z, self.p_float("scale")),
            f"vol-scaled momentum z: {parts} (mean {mean_z:+.2f})",
            confidence=agreement,
            raw_score=mean_z,
            extra={f"z_{h}": z for h, z in zs.items()},
        )


@register
class TimeSeriesMomentum(Strategy):
    implementation = "mom_time_series"
    family = StrategyFamily.MOMENTUM
    description = "classic 12-1 time-series momentum, volatility scaled"
    param_specs = (
        ParamSpec("window", int, 252, 20, 504),
        ParamSpec("skip", int, 21, 0, 63),
        ParamSpec("vol_window", int, 60, 10, 252),
        ParamSpec("scale", float, 1.0, 0.1, 5.0),
    )

    def validate_params(self) -> None:
        if not self.p_int("skip") < self.p_int("window"):
            raise ValueError("require skip < window")

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec(
                "momentum", {"window": self.p_int("window"), "skip": self.p_int("skip")}, name="mom"
            ),
            IndicatorSpec(
                "historical_volatility", {"window": self.p_int("vol_window")}, name="vol"
            ),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        m, vol = ctx.values["mom"], ctx.values["vol"]
        horizon = (self.p_int("window") - self.p_int("skip")) / 252
        z = m / (vol * math.sqrt(horizon)) if vol > 0 else 0.0
        return Evaluation(
            squash(z, self.p_float("scale")),
            f"{self.p_int('window')}-{self.p_int('skip')} momentum {pct(m)} (log), z {z:+.2f}",
            raw_score=z,
        )


@register
class MomentumAcceleration(Strategy):
    implementation = "mom_acceleration"
    family = StrategyFamily.MOMENTUM_ACCELERATION
    description = "change in ROC; halved when it opposes the current momentum direction"
    param_specs = (
        ParamSpec("window", int, 20, 5, 126),
        ParamSpec("lag", int, 10, 1, 63),
        ParamSpec("scale", float, 0.05, 0.005, 0.5),
    )

    def indicators(self) -> list[IndicatorSpec]:
        w, lag = self.p_int("window"), self.p_int("lag")
        return [
            IndicatorSpec("rate_of_change", {"window": w}, name="roc"),
            IndicatorSpec("momentum_acceleration", {"window": w, "lag": lag}, name="accel"),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        roc, accel = ctx.values["roc"], ctx.values["accel"]
        score = squash(accel, self.p_float("scale"))
        note = "strengthening" if accel > 0 else "weakening"
        if sign(roc) != sign(accel):
            score *= 0.5
            note += " against the prevailing move (early turn, halved)"
        return Evaluation(
            score,
            f"{self.p_int('window')}d ROC {pct(roc)}, {note} by {pct(accel)} "
            f"over {self.p_int('lag')}d",
            raw_score=accel,
        )


@register
class BearishConfirmedTrend(Strategy):
    """Only ever bearish; small, capped inverse exposure (SQQQ decays - be conservative)."""

    implementation = "bear_confirmed_trend"
    family = StrategyFamily.BEARISH_MOMENTUM
    description = "bearish only when price<SMA, momentum<0 and slope<0 all agree"
    can_long = False
    param_specs = (
        ParamSpec("max_long_exposure", float, 0.0, 0.0, 0.0),
        ParamSpec(
            "max_short_exposure",
            float,
            0.45,
            0.0,
            1.0,
            description="net inverse exposure at score -1 (0.45 ~ 15% SQQQ)",
        ),
        ParamSpec("ma_window", int, 200, 50, 400),
        ParamSpec("mom_window", int, 60, 10, 252),
        ParamSpec("slope_window", int, 100, 20, 300),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec("distance_from_ma", {"window": self.p_int("ma_window")}, name="distance"),
            IndicatorSpec("momentum", {"window": self.p_int("mom_window")}, name="mom"),
            IndicatorSpec("trend_slope", {"window": self.p_int("slope_window")}, name="slope"),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        d, m, s = ctx.values["distance"], ctx.values["mom"], ctx.values["slope"]
        facts = f"distance {pct(d)}, momentum {pct(m)}, slope {pct(s)}/yr"
        if d < 0 and m < 0 and s < 0:
            strength = (squash(-d, 0.05) + squash(-m, 0.10) + squash(-s, 0.25)) / 3.0
            return Evaluation(-strength, f"confirmed bear regime: {facts}", raw_score=-strength)
        return Evaluation(0.0, f"no confirmed bear regime: {facts}", confidence=0.0)
