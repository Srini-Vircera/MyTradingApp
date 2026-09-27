"""Golden Cross / Death Cross: a moving-average crossover *regime* strategy.

A technical-analysis hypothesis, not a claim of an edge. By default a 50-session
SMA above the 200-session SMA defines the bullish regime, below it the bearish
regime (the classic Golden Cross / Death Cross). Other periods or an EMA make it
a moving-average crossover *variant*.

Two separate concepts are reported:

* **regime** - persists after a crossover: ``fast > slow`` bullish, ``fast < slow``
  bearish. A day with ``fast == slow`` is not a crossover: the regime stays what
  it was (neutral only before the averages have ever differed).
* **cross event** - happens once per actual transition, on the bar where the
  regime flips: Golden Cross when it turns bullish (``prev fast <= prev slow``
  and ``fast > slow``, looking through any days of equality), Death Cross when it
  turns bearish. Staying above the slow MA never reports another Golden Cross.

Point in time: everything is computed from the visible, causal indicator history
(:class:`StrategyContext`), so a later bar can never change an earlier signal.
No signal is produced before both averages are fully warmed up plus one bar (so
the previous day's relationship is known): 201 bars for the default 50/200.

Exposure is only a *suggestion* (the ensemble, allocation policy and risk engine
decide the portfolio): bullish -> ``max_long_exposure`` (default 1.0 = QQQ);
bearish -> ``cash`` (default, 0), ``qqq_reduced`` (``reduced_exposure``) or
``sqqq`` (``-max_short_exposure``, which must then be set above 0 explicitly).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from adaptive_quant.core.enums import StrategyFamily
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.strategies.base import Evaluation, Strategy, StrategyContext
from adaptive_quant.quant.strategies.params import ParamSpec
from adaptive_quant.quant.strategies.registry import register

MA_TYPES = ("SMA", "EMA")
BEARISH_ACTIONS = ("cash", "qqq_reduced", "sqqq")
CLASSIC = (50, 200, "SMA")

GOLDEN, DEATH = 1, -1
REGIME_LABEL = {1: "bullish", -1: "bearish", 0: "neutral"}
CROSS_LABEL = {GOLDEN: "Golden Cross", DEATH: "Death Cross"}


@dataclass(frozen=True)
class CrossHistory:
    """Regime and cross events over a (visible) history of fast/slow averages."""

    regime: np.ndarray  # persistent regime per bar: +1 / -1 / 0 (NaN rows: 0)
    events: np.ndarray  # +1 Golden Cross, -1 Death Cross, 0 none - per bar

    @property
    def last_event_index(self) -> int | None:
        hits = np.flatnonzero(self.events)
        return int(hits[-1]) if hits.size else None


def cross_history(fast: pd.Series[float], slow: pd.Series[float]) -> CrossHistory:
    """Persistent regime and exactly-once cross events.

    Rows where either average is undefined (warm-up) carry regime 0 and no event.
    Equality keeps the previous regime, so below -> equal -> above is one Golden
    Cross (on the "above" bar) and above -> equal -> above is no event at all.
    The first regime ever established is not a cross (nothing was crossed).
    """
    f = fast.to_numpy(dtype=float)
    s = slow.to_numpy(dtype=float)
    diff = f - s
    strict = np.where(np.isnan(diff), 0.0, np.sign(diff))
    regime = np.zeros(len(diff), dtype=int)
    events = np.zeros(len(diff), dtype=int)
    current = 0
    for i, d in enumerate(strict):
        if d != 0 and d != current:
            if current != 0:
                events[i] = int(d)  # a real transition between two strict regimes
            current = int(d)
        regime[i] = current
    return CrossHistory(regime, events)


@register
class GoldenDeathCross(Strategy):
    implementation = "golden_death_cross"
    family = StrategyFamily.LONG_TERM_TREND
    version = "1.0.0"
    description = (
        "Golden Cross / Death Cross: fast MA above slow MA = bullish regime, below = bearish "
        "(classic 50/200 SMA; other periods or EMA make a crossover variant)"
    )
    title = "Golden Cross / Death Cross"
    summary = (
        "Classic moving-average trend strategy. By default, a 50-day SMA above the 200-day "
        "SMA defines the bullish regime; below defines the bearish regime. 50 SMA / 200 SMA "
        "is the classic Golden Cross / Death Cross; other values make a moving-average "
        "crossover variant. A technical-analysis hypothesis, not a proven edge."
    )
    param_specs = (
        ParamSpec("fast_period", int, 50, 2, 250, description="fast moving-average length"),
        ParamSpec(
            "slow_period",
            int,
            200,
            3,
            400,
            description="slow moving-average length (must exceed fast_period)",
        ),
        ParamSpec("ma_type", str, "SMA", choices=MA_TYPES, description="SMA (classic) or EMA"),
        ParamSpec(
            "bearish_action",
            str,
            "cash",
            choices=BEARISH_ACTIONS,
            description="suggested exposure in the bearish regime: cash (default), "
            "qqq_reduced (reduced_exposure x QQQ) or sqqq (needs max_short_exposure > 0)",
        ),
        ParamSpec(
            "reduced_exposure",
            float,
            0.5,
            0.0,
            1.0,
            description="QQQ exposure kept in the bearish regime when bearish_action=qqq_reduced",
        ),
    )

    def validate_params(self) -> None:
        fast, slow = self.p_int("fast_period"), self.p_int("slow_period")
        if not slow > fast:
            raise ValueError(f"slow_period ({slow}) must be greater than fast_period ({fast})")
        action = self.p_str("bearish_action")
        if action == "sqqq" and self.p_float("max_short_exposure") <= 0:
            raise ValueError("bearish_action=sqqq requires max_short_exposure > 0")
        if action != "sqqq" and self.p_float("max_short_exposure") > 0:
            raise ValueError("max_short_exposure > 0 is only used with bearish_action=sqqq")
        if action == "qqq_reduced" and self.p_float("reduced_exposure") > self.p_float(
            "max_long_exposure"
        ):
            raise ValueError("reduced_exposure must not exceed max_long_exposure")

    @property
    def is_classic(self) -> bool:
        return (
            self.p_int("fast_period"),
            self.p_int("slow_period"),
            self.p_str("ma_type"),
        ) == CLASSIC

    @property
    def label(self) -> str:
        t = self.p_str("ma_type")
        pair = f"{self.p_int('fast_period')}/{self.p_int('slow_period')} {t}"
        return f"Golden/Death Cross {pair}" + ("" if self.is_classic else " (variant)")

    def indicators(self) -> list[IndicatorSpec]:
        kind = self.p_str("ma_type").lower()
        return [
            IndicatorSpec(kind, {"window": self.p_int("fast_period")}, name="fast"),
            IndicatorSpec(kind, {"window": self.p_int("slow_period")}, name="slow"),
        ]

    def extra_history_bars(self) -> int:
        return 1  # the previous bar's relationship is needed to recognise a cross

    def bearish_exposure(self) -> float:
        action = self.p_str("bearish_action")
        if action == "qqq_reduced":
            return self.p_float("reduced_exposure")
        if action == "sqqq":
            return -self.p_float("max_short_exposure")
        return 0.0

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        fast, slow = ctx.values["fast"], ctx.values["slow"]
        hist = cross_history(ctx.frame["fast"], ctx.frame["slow"])
        regime = int(hist.regime[-1])
        event = int(hist.events[-1])
        last = hist.last_event_index
        index = ctx.frame.index
        if last is None:
            recent, since, last_kind = "no crossover yet in the visible history", -1, 0
        else:
            last_kind = int(hist.events[last])
            when = pd.Timestamp(index[last]).tz_convert("America/New_York").date()
            since = len(index) - 1 - last
            recent = f"most recent: {CROSS_LABEL[last_kind]} on {when}"
        t = self.p_str("ma_type")
        mas = (
            f"{t}({self.p_int('fast_period')}) {fast:.2f} vs "
            f"{t}({self.p_int('slow_period')}) {slow:.2f}"
        )
        today = f"{CROSS_LABEL[event]} today; " if event else ""
        if regime > 0:
            score, exposure, action = 1.0, self.p_float("max_long_exposure"), "long"
        elif regime < 0:
            score, exposure = -1.0, self.bearish_exposure()
            action = self.p_str("bearish_action")
        else:
            score, exposure, action = 0.0, 0.0, "cash (averages equal since warm-up)"
        reason = (
            f"{self.label}: {today}{REGIME_LABEL[regime]} regime ({mas}); {recent}; "
            f"suggestion: {action}"
        )
        return Evaluation(
            score,
            reason,
            confidence=1.0 if regime else 0.0,
            raw_score=fast / slow - 1.0,
            exposure=exposure,
            extra={
                "fast_ma": fast,
                "slow_ma": slow,
                "regime": float(regime),
                "cross_event": float(event),
                "last_cross": float(last_kind),
                "bars_since_cross": float(since),
            },
        )
