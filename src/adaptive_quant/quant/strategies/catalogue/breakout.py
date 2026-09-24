"""Breakout candidates (channel, momentum-confirmed, squeeze, trend + volume)."""

from __future__ import annotations

from adaptive_quant.core.enums import StrategyFamily
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.strategies.base import Evaluation, Strategy, StrategyContext
from adaptive_quant.quant.strategies.params import ParamSpec
from adaptive_quant.quant.strategies.registry import register
from adaptive_quant.quant.strategies.scoring import clip, pct, sign


def _channel(window: int) -> list[IndicatorSpec]:
    """Prior-window high/low: the levels today's close must break (excludes today)."""
    return [
        IndicatorSpec("rolling_high", {"window": window, "include_current": False}, name="upper"),
        IndicatorSpec("rolling_low", {"window": window, "include_current": False}, name="lower"),
    ]


def channel_position(close: float, upper: float, lower: float) -> float:
    """-1 at/below the prior low, +1 at/above the prior high, linear in between."""
    if upper <= lower:
        return 0.0
    return clip(2.0 * (close - lower) / (upper - lower) - 1.0)


@register
class DonchianBreakout(Strategy):
    implementation = "bo_donchian"
    family = StrategyFamily.BREAKOUT
    description = "position of the close inside the prior N-bar high/low channel"
    param_specs = (ParamSpec("window", int, 55, 10, 252),)

    def indicators(self) -> list[IndicatorSpec]:
        return _channel(self.p_int("window"))

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        up, lo = ctx.values["upper"], ctx.values["lower"]
        pos = channel_position(ctx.close, up, lo)
        state = (
            "above prior high"
            if ctx.close > up
            else "below prior low"
            if ctx.close < lo
            else "inside channel"
        )
        return Evaluation(
            pos, f"close {ctx.close:.2f} {state} ({lo:.2f}-{up:.2f}, {self.p_int('window')}d)"
        )


@register
class MomentumConfirmedBreakout(Strategy):
    implementation = "bo_momentum_confirmed"
    family = StrategyFamily.BREAKOUT_PLUS_MOMENTUM
    description = "channel position, cut to 25% unless momentum agrees"
    param_specs = (
        ParamSpec("window", int, 20, 10, 252),
        ParamSpec("mom_window", int, 60, 10, 252),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [
            *_channel(self.p_int("window")),
            IndicatorSpec("momentum", {"window": self.p_int("mom_window")}, name="mom"),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        pos = channel_position(ctx.close, ctx.values["upper"], ctx.values["lower"])
        mom = ctx.values["mom"]
        agree = sign(pos) == sign(mom)
        score = pos if agree else 0.25 * pos
        return Evaluation(
            score,
            f"channel position {pos:+.2f}, {self.p_int('mom_window')}d momentum {pct(mom)} "
            + ("confirms" if agree else "does not confirm (cut to 25%)"),
            raw_score=pos,
        )


@register
class BollingerSqueezeBreakout(Strategy):
    implementation = "bo_bollinger_squeeze"
    family = StrategyFamily.BREAKOUT
    description = "direction from %B; full strength only after a low-volatility squeeze"
    param_specs = (
        ParamSpec("window", int, 20, 10, 100),
        ParamSpec("num_std", float, 2.0, 1.0, 3.5),
        ParamSpec("squeeze_percentile", float, 0.3, 0.05, 0.6),
        ParamSpec("vol_lookback", int, 252, 60, 504),
    )

    def indicators(self) -> list[IndicatorSpec]:
        w = self.p_int("window")
        return [
            IndicatorSpec(
                "bollinger",
                {"window": w, "num_std": self.p_float("num_std"), "output": "percent_b"},
                name="pct_b",
            ),
            IndicatorSpec(
                "volatility_percentile",
                {"vol_window": w, "lookback": self.p_int("vol_lookback")},
                name="vol_pct",
            ),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        pb, vp = ctx.values["pct_b"], ctx.values["vol_pct"]
        direction = clip(2.0 * pb - 1.0)
        squeeze = vp <= self.p_float("squeeze_percentile")
        score = direction if squeeze else 0.3 * direction
        return Evaluation(
            score,
            f"%B {pb:.2f}, volatility percentile {vp:.0%} "
            + ("(squeeze: full strength)" if squeeze else "(no squeeze: 30%)"),
            raw_score=direction,
        )


@register
class TrendVolumeBreakout(Strategy):
    """Volume note: Alpaca's free IEX feed reports partial volume; synthetic bars have none."""

    implementation = "bo_trend_volume"
    family = StrategyFamily.BREAKOUT
    description = "break of prior high/low in the direction of the long trend, volume-confirmed"
    param_specs = (
        ParamSpec("window", int, 20, 10, 252),
        ParamSpec("trend_window", int, 200, 50, 400),
        ParamSpec("volume_window", int, 50, 10, 252),
        ParamSpec("volume_factor", float, 1.2, 1.0, 3.0),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [
            *_channel(self.p_int("window")),
            IndicatorSpec("sma", {"window": self.p_int("trend_window")}, name="trend"),
            IndicatorSpec(
                "sma", {"window": self.p_int("volume_window")}, source="volume", name="avg_volume"
            ),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        v = ctx.values
        volume = float(ctx.bars["volume"].iloc[-1])
        ratio = volume / v["avg_volume"] if v["avg_volume"] > 0 else None
        confirmed = ratio is not None and ratio >= self.p_float("volume_factor")
        vol_txt = "volume unavailable" if ratio is None else f"volume {ratio:.2f}x average"
        extra = {} if ratio is None else {"volume_ratio": ratio}
        strength = 1.0 if confirmed else 0.6
        if ctx.close > v["upper"] and ctx.close > v["trend"]:
            return Evaluation(strength, f"upside breakout in uptrend, {vol_txt}", extra=extra)
        if ctx.close < v["lower"] and ctx.close < v["trend"]:
            return Evaluation(-strength, f"downside breakdown in downtrend, {vol_txt}", extra=extra)
        return Evaluation(
            0.0, f"no trend-aligned breakout ({vol_txt})", confidence=0.0, extra=extra
        )
