"""Interim allocation: net underlying exposure -> instrument weights.

**Superseded by the M7 decision chain** (``quant/portfolio``, ``quant/risk``),
which backtests use by default (``backtest.allocation.risk_engine``). This
allocator remains for comparison runs and engine unit tests: it applies only
the *static* caps from ``risk.yaml`` (net/inverse exposure, per-position,
leveraged-ETF weight, gross exposure, minimum cash).

Mapping of a net exposure ``E`` (QQQ-equivalent multiple of equity):

* ``qqq_then_tqqq`` (default): ``E <= 1`` -> QQQ ``E``; ``1 < E <= 3`` -> the
  least-leveraged mix with QQQ + TQQQ = 100 %: TQQQ ``(E-1)/2``, QQQ ``1-(E-1)/2``.
* ``tqqq_only``: TQQQ ``E/3``, rest cash.  ``qqq_only``: QQQ ``min(E, 1)``.
* ``E < 0``: SQQQ ``|E|/3`` (bearish exposure only by *holding* an inverse ETF).

Every reduction is recorded as a human-readable adjustment.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from adaptive_quant.config.schema import RiskConfig
from adaptive_quant.core.models import Instrument
from adaptive_quant.core.money import quantize_weight, to_decimal
from adaptive_quant.quant.portfolio.policy import exposure_to_weights

LONG_1X, LONG_3X, SHORT_3X = "QQQ", "TQQQ", "SQQQ"


@dataclass(frozen=True)
class Allocation:
    requested_exposure: float
    weights: dict[str, Decimal]
    adjustments: tuple[str, ...]

    @property
    def cash_weight(self) -> Decimal:
        return Decimal(1) - sum(self.weights.values(), Decimal(0))


class ExposureAllocator:
    def __init__(
        self,
        instruments: dict[str, Instrument],
        long_mode: str = "qqq_then_tqqq",
        limits: RiskConfig | None = None,
    ) -> None:
        needed = {LONG_1X, LONG_3X, SHORT_3X}
        missing = needed - set(instruments)
        if missing:
            raise ValueError(f"allocator needs instruments {sorted(missing)}")
        for sym, lev in ((LONG_1X, 1.0), (LONG_3X, 3.0), (SHORT_3X, -3.0)):
            if instruments[sym].leverage != lev:
                raise ValueError(f"{sym} must have leverage {lev}")
        if long_mode not in ("qqq_then_tqqq", "tqqq_only", "qqq_only"):
            raise ValueError(f"unknown long_mode {long_mode!r}")
        self.instruments = instruments
        self.long_mode = long_mode
        self.limits = limits

    @property
    def symbols(self) -> list[str]:
        return [LONG_1X, LONG_3X, SHORT_3X]

    def allocate(self, net_exposure: float) -> Allocation:
        adjustments: list[str] = []
        e = net_exposure
        lim = self.limits
        if lim is not None:
            if e > lim.max_net_underlying_exposure:
                adjustments.append(
                    f"net exposure {e:+.2f} capped at {lim.max_net_underlying_exposure:+.2f}"
                )
                e = lim.max_net_underlying_exposure
            if e < -lim.max_inverse_net_exposure:
                adjustments.append(
                    f"inverse exposure {e:+.2f} capped at {-lim.max_inverse_net_exposure:+.2f}"
                )
                e = -lim.max_inverse_net_exposure
        w = self._map(e)
        if lim is not None:
            w = self._apply_limits(w, lim, adjustments)
        weights = {s: quantize_weight(v) for s, v in w.items()}
        return Allocation(net_exposure, weights, tuple(adjustments))

    def _map(self, e: float) -> dict[str, float]:
        return exposure_to_weights(e, self.long_mode)

    def _apply_limits(
        self, w: dict[str, float], lim: RiskConfig, adjustments: list[str]
    ) -> dict[str, float]:
        for sym in w:
            cap = lim.position_cap(sym)
            if w[sym] > cap + 1e-12:
                adjustments.append(f"{sym} weight {w[sym]:.1%} capped at {cap:.1%}")
                w[sym] = cap
        lev_total = w[LONG_3X] + w[SHORT_3X]
        if lev_total > lim.max_leveraged_etf_weight + 1e-12:
            scale = lim.max_leveraged_etf_weight / lev_total
            adjustments.append(
                f"leveraged ETF weight {lev_total:.1%} scaled to {lim.max_leveraged_etf_weight:.1%}"
            )
            w[LONG_3X] *= scale
            w[SHORT_3X] *= scale
        room = min(lim.max_gross_exposure, 1.0 - lim.min_cash_weight)
        gross = sum(w.values())
        if gross > room + 1e-12:
            adjustments.append(
                f"gross exposure {gross:.1%} scaled to {room:.1%} (cash floor/gross cap)"
            )
            w = {s: v * room / gross for s, v in w.items()}
        return w

    def net_exposure(self, weights: dict[str, Decimal]) -> float:
        return float(
            sum(
                (to_decimal(self.instruments[s].leverage) * v for s, v in weights.items()),
                Decimal(0),
            )
        )
