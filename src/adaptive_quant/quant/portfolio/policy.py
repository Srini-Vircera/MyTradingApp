"""Allocation policy: ensemble net exposure -> proposed instrument weights.

Mapping of a net underlying exposure ``E`` (QQQ-equivalent multiple of equity):

* ``qqq_then_tqqq`` (default): ``E <= 1`` -> QQQ ``E``; ``1 < E <= 3`` -> the
  least-leveraged mix with QQQ + TQQQ = 100 %: TQQQ ``(E-1)/2``, QQQ ``1-(E-1)/2``.
* ``tqqq_only``: TQQQ ``E/3``, rest cash.  ``qqq_only``: QQQ ``min(E, 1)``.
* ``E < 0``: SQQQ ``|E|/3`` - bearish exposure only by *holding* an inverse ETF.

``|E| < min_abs_exposure`` is held as cash (dead band against tiny positions).
The policy proposes; the risk engine decides. Regime awareness (volatility
regime caps, drawdown bands) is applied by the risk engine, not here.
"""

from __future__ import annotations

import math
from datetime import datetime

from adaptive_quant.quant.risk.models import ProposedPortfolio

LONG_1X, LONG_3X, SHORT_3X = "QQQ", "TQQQ", "SQQQ"
LONG_MODES = ("qqq_then_tqqq", "tqqq_only", "qqq_only")


def exposure_to_weights(e: float, long_mode: str = "qqq_then_tqqq") -> dict[str, float]:
    if long_mode not in LONG_MODES:
        raise ValueError(f"unknown long_mode {long_mode!r}")
    if not math.isfinite(e):
        raise ValueError("exposure must be finite")
    w = {LONG_1X: 0.0, LONG_3X: 0.0, SHORT_3X: 0.0}
    if e < 0:
        w[SHORT_3X] = min(1.0, -e / 3.0)
    elif long_mode == "qqq_only":
        w[LONG_1X] = min(e, 1.0)
    elif long_mode == "tqqq_only":
        w[LONG_3X] = min(e / 3.0, 1.0)
    elif e <= 1.0:
        w[LONG_1X] = e
    else:
        t = min((e - 1.0) / 2.0, 1.0)
        w[LONG_3X], w[LONG_1X] = t, 1.0 - t
    return w


class AllocationPolicy:
    def __init__(self, long_mode: str = "qqq_then_tqqq", min_abs_exposure: float = 0.0) -> None:
        if long_mode not in LONG_MODES:
            raise ValueError(f"unknown long_mode {long_mode!r}")
        self.long_mode = long_mode
        self.min_abs_exposure = min_abs_exposure

    def propose(self, exposure: float, as_of: datetime, rationale: str = "") -> ProposedPortfolio:
        e = exposure if abs(exposure) >= self.min_abs_exposure else 0.0
        return ProposedPortfolio(
            as_of=as_of,
            weights=exposure_to_weights(e, self.long_mode),
            requested_exposure=exposure,
            rationale=rationale or f"ensemble exposure {exposure:+.3f}",
        )
