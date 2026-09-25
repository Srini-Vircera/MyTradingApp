"""The risk engine: proposed portfolio -> approved weights + an adjustment per change.

Rules, in order (``docs/RISK_MANAGEMENT.md``):

1. sanity          finite, >= 0, known tradeable symbols, sum <= 1 (else refuse)
2. hard state      kill switch / blocking drawdown band / daily-loss limit
                   -> freeze: no position may increase (risk-reducing changes allowed)
3. vol target      scale = clamp(target / forecast, min_scale, max_scale), forecast =
                   EWMA vol of the underlying x |net exposure|; never > 1 while frozen
4. drawdown band   |net exposure| <= band cap (with hysteresis on the way out)
5. vol regime      optional |net exposure| cap per regime
6. exposure caps   net / inverse / per-symbol / leveraged-ETF / gross / cash floor
7. turnover        increases limited to what is left of max_daily_turnover after
                   decreases; decreases (risk-reducing) are never blocked
8. verification    every configured limit is re-checked on the result

Reductions move weight into cash, never into another risky asset, and come out
of the most leveraged exposure first. Any exception or failed verification
raises :class:`RiskCalculationError`: the caller must refuse the cycle.
Order-level limits (max order notional, buying power) belong to the order
planner (M9).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from decimal import ROUND_DOWN, Decimal

from adaptive_quant.config.schema import DrawdownBand, RiskConfig
from adaptive_quant.core.models import Instrument
from adaptive_quant.core.money import WEIGHT_QUANTUM
from adaptive_quant.quant.risk.estimators import (
    ewma_volatility,
    required_history,
    volatility_regime,
)
from adaptive_quant.quant.risk.models import (
    ProposedPortfolio,
    RiskAdjustment,
    RiskCalculationError,
    RiskContext,
    RiskDecision,
)

TOL = 1e-9
Weights = dict[str, float]


class RiskEngine:
    def __init__(self, limits: RiskConfig, instruments: Mapping[str, Instrument]) -> None:
        self.limits = limits
        self.instruments = dict(instruments)
        self.leverage = {s: float(i.leverage) for s, i in instruments.items() if i.tradeable}
        if not self.leverage:
            raise ValueError("risk engine needs at least one tradeable instrument")

    @property
    def required_history(self) -> int:
        vt = self.limits.volatility_target
        return required_history(vt.enabled, vt.lookback_days, self.limits.volatility_regimes)

    # ------------------------------------------------------------------ public
    def evaluate(self, proposal: ProposedPortfolio, ctx: RiskContext) -> RiskDecision:
        """Approve (and usually reduce) ``proposal``. Fails closed on any error."""
        try:
            return self._evaluate(proposal, ctx)
        except RiskCalculationError:
            raise
        except Exception as exc:  # every failure must refuse, never pass through
            raise RiskCalculationError(
                f"risk calculation failed: {type(exc).__name__}: {exc}"
            ) from exc

    def net(self, w: Mapping[str, float]) -> float:
        return sum(v * self.leverage[s] for s, v in w.items())

    def band_for(self, drawdown: float, previous: str | None) -> DrawdownBand:
        bands = self.limits.drawdown_bands
        k = max(i for i, b in enumerate(bands) if drawdown >= b.threshold - TOL)
        names = [b.name for b in bands]
        if previous in names:
            p = names.index(previous)
            # stay in a deeper band until drawdown recovers past its threshold minus hysteresis
            if p > k and drawdown >= bands[p].threshold - self.limits.drawdown_hysteresis - TOL:
                k = p
        return bands[k]

    # ------------------------------------------------------------------ pipeline
    def _evaluate(self, proposal: ProposedPortfolio, ctx: RiskContext) -> RiskDecision:
        lim = self.limits
        adj: list[RiskAdjustment] = []
        flags: list[str] = []
        w = self._sanity(proposal.weights, "proposal")
        cur = self._sanity(ctx.current_weights, "current weights", allow_excess=True)
        for s in self.leverage:
            w.setdefault(s, 0.0)
            cur.setdefault(s, 0.0)
        if not (math.isfinite(ctx.equity) and ctx.equity > 0 and ctx.peak_equity > 0):
            raise RiskCalculationError("equity and peak equity must be positive and finite")
        if not (math.isfinite(ctx.previous_equity) and ctx.previous_equity > 0):
            raise RiskCalculationError("previous equity must be positive and finite")
        r = ctx.underlying_returns
        if len(r) < self.required_history:
            raise RiskCalculationError(
                f"risk estimates need {self.required_history} underlying returns, have {len(r)}"
            )

        # 2. hard state ------------------------------------------------------
        drawdown = max(0.0, 1.0 - ctx.equity / max(ctx.peak_equity, ctx.equity))
        band = self.band_for(drawdown, ctx.previous_band)
        daily_loss = max(0.0, 1.0 - ctx.equity / ctx.previous_equity)
        reasons = []
        if ctx.kill_switch_engaged:
            reasons.append("kill switch engaged")
        if band.block_risk_increasing:
            reasons.append(f"drawdown band {band.name}")
        if daily_loss >= lim.max_daily_loss - TOL:
            reasons.append(f"daily loss {daily_loss:.2%} >= {lim.max_daily_loss:.2%}")
        frozen = bool(reasons)
        if frozen:
            flags += reasons
            w = self._freeze(w, cur, adj, "; ".join(reasons))

        # 3. volatility target ------------------------------------------------
        vt = lim.volatility_target
        scale, forecast = 1.0, math.nan
        if vt.enabled:
            forecast = ewma_volatility(r, vt.lookback_days) * abs(self.net(w))
            hi = min(vt.max_scale, 1.0) if frozen else vt.max_scale
            scale = (
                hi
                if forecast <= TOL
                else min(max(vt.annualized_target / forecast, vt.min_scale), hi)
            )
            if abs(scale - 1.0) > TOL:
                before = self.net(w)
                w = {s: v * scale for s, v in w.items()}
                adj.append(
                    RiskAdjustment(
                        "volatility_target",
                        before,
                        self.net(w),
                        f"forecast vol {forecast:.1%} vs target "
                        f"{vt.annualized_target:.1%}: scale {scale:.3f}",
                    )
                )

        # 4. drawdown band ------------------------------------------------------
        self._cap_net(
            w,
            band.max_net_exposure,
            band.max_net_exposure,
            adj,
            "drawdown_band",
            f"band {band.name} at drawdown {drawdown:.1%}",
        )

        # 5. volatility regime ------------------------------------------------------
        regime = None
        caps = lim.volatility_regimes.max_net_exposure
        if caps:
            regime, pct = volatility_regime(r, lim.volatility_regimes)
            if regime in caps:
                self._cap_net(
                    w,
                    caps[regime],
                    caps[regime],
                    adj,
                    "volatility_regime",
                    f"{regime} volatility (percentile {pct:.0%})",
                )
            flags.append(f"regime {regime}")

        # 6. exposure caps --------------------------------------------------------
        cap_long = min(lim.max_net_underlying_exposure, band.max_net_exposure)
        cap_short = min(lim.max_inverse_net_exposure, band.max_net_exposure)
        if regime is not None and regime in caps:
            cap_long, cap_short = min(cap_long, caps[regime]), min(cap_short, caps[regime])
        self._exposure_caps(w, adj, cap_long, cap_short)

        # 7. turnover ----------------------------------------------------------------
        for _ in range(20):  # caps only reduce; iterate until turnover and caps agree
            prior = dict(w)
            w = self._turnover(w, cur, adj)
            self._exposure_caps(w, adj, cap_long, cap_short)
            if all(abs(w[s] - prior[s]) <= TOL for s in w):
                break
        if self._turnover_used(w, cur) > lim.max_daily_turnover + TOL and any(
            w[s] > cur[s] + TOL for s in w
        ):
            # not converged: keep only reductions (always allowed), never an over-budget increase
            before = self.net(w)
            w = {s: min(w[s], cur[s]) for s in w}
            self._exposure_caps(w, adj, cap_long, cap_short)
            adj.append(RiskAdjustment("turnover", before, self.net(w), "increases dropped"))

        # 8. verification -------------------------------------------------------------
        approved = {s: _round_down(v) for s, v in w.items() if v > 0}
        self._verify({s: float(v) for s, v in approved.items()}, cur, band, regime, frozen)
        return RiskDecision(
            as_of=ctx.as_of,
            weights=approved,
            proposed=dict(proposal.weights),
            adjustments=tuple(adj),
            band=band.name,
            drawdown=drawdown,
            vol_scale=scale,
            forecast_vol=forecast,
            regime=regime,
            daily_loss=daily_loss,
            risk_increasing_blocked=frozen,
            flags=tuple(flags),
        )

    # ------------------------------------------------------------------ rules
    def _sanity(
        self, weights: Mapping[str, float], what: str, allow_excess: bool = False
    ) -> Weights:
        out: Weights = {}
        for s, v in weights.items():
            if s not in self.leverage:
                raise RiskCalculationError(f"{what}: unknown or non-tradeable symbol {s!r}")
            v = float(v)
            if not math.isfinite(v) or v < -TOL:
                raise RiskCalculationError(f"{what}: invalid weight {s}={v}")
            out[s] = max(v, 0.0)
        total = sum(out.values())
        if total > 1.0 + 1e-6 and not allow_excess:
            raise RiskCalculationError(
                f"{what}: weights sum to {total:.6f} > 1 (no account leverage)"
            )
        return out

    def _freeze(self, w: Weights, cur: Weights, adj: list[RiskAdjustment], why: str) -> Weights:
        frozen = {s: min(w[s], cur[s]) for s in w}
        if abs(self.net(frozen)) > abs(self.net(cur)) + TOL:
            # cutting one leg would raise |net exposure|: hold everything instead
            frozen = dict(cur)
        if any(abs(frozen[s] - w[s]) > TOL for s in w):
            adj.append(
                RiskAdjustment(
                    "freeze",
                    self.net(w),
                    self.net(frozen),
                    f"risk-increasing changes blocked: {why}",
                )
            )
        return frozen

    def _by_leverage(self, sign: int) -> list[str]:
        syms = [s for s, lev in self.leverage.items() if lev * sign > 0]
        return sorted(syms, key=lambda s: (-abs(self.leverage[s]), s))

    def _cap_net(
        self,
        w: Weights,
        cap_long: float,
        cap_short: float,
        adj: list[RiskAdjustment],
        rule: str,
        why: str,
    ) -> None:
        before = self.net(w)
        for sign, cap in ((1, cap_long), (-1, cap_short)):
            excess = sign * self.net(w) - cap
            for s in self._by_leverage(sign):
                if excess <= TOL:
                    break
                cut = min(w[s], excess / abs(self.leverage[s]))
                w[s] -= cut
                excess -= cut * abs(self.leverage[s])
        after = self.net(w)
        if abs(after - before) > TOL:
            adj.append(
                RiskAdjustment(
                    rule,
                    before,
                    after,
                    f"{why}: |net| capped at {cap_long if before > 0 else cap_short:.2f}",
                )
            )

    def _exposure_caps(
        self, w: Weights, adj: list[RiskAdjustment], cap_long: float, cap_short: float
    ) -> None:
        """Weight caps, then the combined net caps last: scaling leveraged legs or the
        gross cap can move net exposure, and net caps only ever reduce weights."""
        lim = self.limits
        for s in sorted(w):
            cap = lim.position_cap(s)
            if w[s] > cap + TOL:
                adj.append(RiskAdjustment(f"position_cap:{s}", w[s], cap, f"{s} weight capped"))
                w[s] = cap
        lev = [s for s in w if abs(self.leverage[s]) > 1.0]
        total = sum(w[s] for s in lev)
        if total > lim.max_leveraged_etf_weight + TOL:
            k = lim.max_leveraged_etf_weight / total
            for s in lev:
                w[s] *= k
            adj.append(
                RiskAdjustment(
                    "leveraged_etf_weight",
                    total,
                    lim.max_leveraged_etf_weight,
                    "leveraged ETF weight scaled down",
                )
            )
        room = min(lim.max_gross_exposure, 1.0 - lim.min_cash_weight)
        excess = sum(w.values()) - room
        if excess > TOL:
            before = sum(w.values())
            for s in sorted(w, key=lambda s: (-abs(self.leverage[s]), s)):
                cut = min(w[s], excess)
                w[s] -= cut
                excess -= cut
                if excess <= TOL:
                    break
            adj.append(
                RiskAdjustment(
                    "gross_exposure",
                    before,
                    sum(w.values()),
                    "gross cap / cash floor (most leveraged reduced first)",
                )
            )

        self._cap_net(
            w,
            cap_long,
            cap_short,
            adj,
            "net_exposure",
            "net / inverse exposure limits (configured, band and regime)",
        )

    @staticmethod
    def _turnover_used(w: Weights, cur: Weights) -> float:
        return sum(abs(w[s] - cur[s]) for s in w)

    def _turnover(self, w: Weights, cur: Weights, adj: list[RiskAdjustment]) -> Weights:
        budget = self.limits.max_daily_turnover
        dec = sum(max(cur[s] - w[s], 0.0) for s in w)
        inc = sum(max(w[s] - cur[s], 0.0) for s in w)
        if dec + inc <= budget + TOL or inc <= TOL:
            return w
        k = max(budget - dec, 0.0) / inc
        out = {s: (cur[s] + (w[s] - cur[s]) * k if w[s] > cur[s] else w[s]) for s in w}
        adj.append(
            RiskAdjustment(
                "turnover",
                dec + inc,
                dec + inc * k,
                f"increases scaled by {k:.3f} (limit {budget:.2f}; reductions never blocked)",
            )
        )
        return out

    def _verify(
        self, w: Weights, cur: Weights, band: DrawdownBand, regime: str | None, frozen: bool
    ) -> None:
        lim = self.limits
        problems = []
        net = self.net(w)
        gross = sum(w.values())
        caps_long = [lim.max_net_underlying_exposure, band.max_net_exposure]
        caps_short = [lim.max_inverse_net_exposure, band.max_net_exposure]
        rc = lim.volatility_regimes.max_net_exposure
        if regime is not None and regime in rc:
            caps_long.append(rc[regime])
            caps_short.append(rc[regime])
        eps = 1e-5  # weights are rounded down to 1e-6; net exposure multiplies by up to 3
        if any(v < 0 or not math.isfinite(v) for v in w.values()):
            problems.append("negative or non-finite weight")
        if net > min(caps_long) + eps:
            problems.append(f"net {net:.4f} > {min(caps_long):.4f}")
        if -net > min(caps_short) + eps:
            problems.append(f"inverse net {net:.4f} beyond {-min(caps_short):.4f}")
        if gross > min(lim.max_gross_exposure, 1.0 - lim.min_cash_weight) + eps:
            problems.append(f"gross {gross:.4f} over limit")
        for s, v in w.items():
            if v > lim.position_cap(s) + eps:
                problems.append(f"{s} {v:.4f} > cap {lim.position_cap(s):.4f}")
        if (
            sum(v for s, v in w.items() if abs(self.leverage[s]) > 1)
            > lim.max_leveraged_etf_weight + eps
        ):
            problems.append("leveraged ETF weight over limit")
        if frozen and any(v > cur.get(s, 0.0) + eps for s, v in w.items()):
            problems.append("a position increased while risk-increasing changes are blocked")
        inc = sum(max(v - cur.get(s, 0.0), 0.0) for s, v in w.items())
        dec = sum(max(cur.get(s, 0.0) - w.get(s, 0.0), 0.0) for s in cur)
        if inc > eps and inc + dec > lim.max_daily_turnover + eps:
            problems.append(f"turnover {inc + dec:.4f} over limit with risk-increasing changes")
        if problems:
            raise RiskCalculationError("risk verification failed: " + "; ".join(problems))


def _round_down(v: float) -> Decimal:
    return Decimal(repr(v)).quantize(WEIGHT_QUANTUM, rounding=ROUND_DOWN)


#: hook type for the pre-trade gate (M10 wires it to the live cycle)
RiskDecisionProvider = Callable[[], RiskDecision]
