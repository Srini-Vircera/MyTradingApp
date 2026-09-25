"""Mean-reversion and market-extension candidates. None of them ever shorts."""

from __future__ import annotations

from adaptive_quant.core.enums import StrategyFamily
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.strategies.base import Evaluation, Strategy, StrategyContext
from adaptive_quant.quant.strategies.params import ParamSpec
from adaptive_quant.quant.strategies.registry import register
from adaptive_quant.quant.strategies.scoring import clip, pct

_NO_SHORT = ParamSpec("max_short_exposure", float, 0.0, 0.0, 0.0, description="never shorts")


@register
class RsiDipInUptrend(Strategy):
    implementation = "mr_rsi_dip"
    family = StrategyFamily.MEAN_REVERSION_LONG
    description = "buy short-term oversold RSI only while price is above the long SMA"
    can_short = False
    param_specs = (
        _NO_SHORT,
        ParamSpec("rsi_window", int, 3, 2, 14),
        ParamSpec("oversold", float, 15.0, 1.0, 40.0),
        ParamSpec("trend_window", int, 200, 50, 400),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec("rsi", {"window": self.p_int("rsi_window")}, name="rsi"),
            IndicatorSpec("sma", {"window": self.p_int("trend_window")}, name="trend"),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        r, trend, lvl = ctx.values["rsi"], ctx.values["trend"], self.p_float("oversold")
        uptrend = ctx.close > trend
        if uptrend and r < lvl:
            score = clip((lvl - r) / lvl, 0.0, 1.0)
            return Evaluation(
                score,
                f"RSI({self.p_int('rsi_window')}) {r:.1f} < {lvl:.0f} in an uptrend: buy the dip",
            )
        why = (
            "not oversold"
            if r >= lvl
            else "oversold but below the long SMA (no dip-buying in downtrends)"
        )
        return Evaluation(0.0, f"RSI {r:.1f}: {why}", confidence=0.0)


@register
class DrawdownDip(Strategy):
    implementation = "mr_drawdown_dip"
    family = StrategyFamily.MEAN_REVERSION_LONG
    description = "short-term pullback deeper than k ATRs inside a long-term uptrend"
    can_short = False
    param_specs = (
        _NO_SHORT,
        ParamSpec("dd_window", int, 10, 3, 63),
        ParamSpec("atr_window", int, 14, 5, 63),
        ParamSpec("atr_multiple", float, 2.0, 0.5, 6.0),
        ParamSpec("trend_window", int, 200, 50, 400),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec("drawdown", {"window": self.p_int("dd_window")}, name="dd"),
            IndicatorSpec("atr", {"window": self.p_int("atr_window")}, name="atr"),
            IndicatorSpec("sma", {"window": self.p_int("trend_window")}, name="trend"),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        v = ctx.values
        depth_atr = (-v["dd"] * ctx.close) / v["atr"] if v["atr"] > 0 else 0.0
        k = self.p_float("atr_multiple")
        extra = {"depth_atr": depth_atr}
        if ctx.close > v["trend"] and depth_atr >= k:
            score = clip(depth_atr / (2 * k), 0.0, 1.0)
            return Evaluation(
                score,
                f"pullback {pct(v['dd'])} = {depth_atr:.1f} ATR (>= {k}) in uptrend",
                extra=extra,
            )
        return Evaluation(
            0.0, f"pullback {depth_atr:.1f} ATR; no qualifying dip", confidence=0.0, extra=extra
        )


@register
class DefensiveExtensionTrim(Strategy):
    """Reduces bullish exposure when price is stretched far above trend. Never shorts."""

    implementation = "dmr_extension_trim"
    family = StrategyFamily.DEFENSIVE_MEAN_REVERSION
    description = "negative (trim-to-cash) score when z-score and distance above trend are extreme"
    can_short = False
    param_specs = (
        _NO_SHORT,
        ParamSpec("z_window", int, 50, 10, 252),
        ParamSpec("z_threshold", float, 2.0, 0.5, 4.0),
        ParamSpec("trend_window", int, 200, 50, 400),
        ParamSpec("min_extension", float, 0.08, 0.0, 0.5),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec("rolling_zscore", {"window": self.p_int("z_window")}, name="z"),
            IndicatorSpec(
                "distance_from_ma", {"window": self.p_int("trend_window")}, name="distance"
            ),
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        z, d = ctx.values["z"], ctx.values["distance"]
        thr = self.p_float("z_threshold")
        if z > thr and d > self.p_float("min_extension"):
            score = -clip(0.25 + (z - thr) / thr, 0.0, 1.0)
            return Evaluation(
                score, f"extended: z {z:+.2f} > {thr}, {pct(d)} above SMA - trim toward cash"
            )
        return Evaluation(0.0, f"not extended (z {z:+.2f}, {pct(d)} vs SMA)", confidence=0.0)


@register
class BollingerExtension(Strategy):
    implementation = "ext_bollinger_percent_b"
    family = StrategyFamily.MARKET_EXTENSION
    description = "contrarian %B: stretched above the band -> reduce, below -> add"
    can_short = False
    param_specs = (
        _NO_SHORT,
        ParamSpec("window", int, 20, 10, 100),
        ParamSpec("num_std", float, 2.0, 1.0, 3.5),
    )

    def indicators(self) -> list[IndicatorSpec]:
        return [
            IndicatorSpec(
                "bollinger",
                {
                    "window": self.p_int("window"),
                    "num_std": self.p_float("num_std"),
                    "output": "percent_b",
                },
                name="pct_b",
            )
        ]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        pb = ctx.values["pct_b"]
        score = clip(-(2.0 * pb - 1.0))
        where = "above upper band" if pb > 1 else "below lower band" if pb < 0 else "inside bands"
        return Evaluation(score, f"%B {pb:.2f} ({where}); contrarian score", raw_score=pb)
