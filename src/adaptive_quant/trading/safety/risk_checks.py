"""Pre-trade checks fed by the M7 risk engine.

* a risk calculation that fails (any exception, including a failed
  verification) refuses **all** trading: ``risk_calculation_failure``;
* a decision taken in a blocking drawdown band refuses risk-increasing orders:
  ``drawdown_emergency``; a breached daily-loss limit likewise:
  ``daily_loss_limit`` (risk-reducing orders remain allowed).
"""

from __future__ import annotations

from adaptive_quant.quant.risk.engine import RiskDecisionProvider
from adaptive_quant.trading.safety.preflight import BlockScope, CheckResult, RefusalReason


class RiskEngineCheck:
    name = "risk_engine"

    def __init__(self, decision: RiskDecisionProvider, max_daily_loss: float) -> None:
        self._decision = decision
        self._max_daily_loss = max_daily_loss

    def run(self) -> CheckResult:
        try:
            d = self._decision()
        except Exception as exc:  # noqa: BLE001 - fail closed: any failure blocks everything
            return CheckResult.fail(
                self.name, RefusalReason.RISK_CALCULATION_FAILURE, f"{type(exc).__name__}: {exc}"
            )
        if d.daily_loss >= self._max_daily_loss:
            return CheckResult.fail(
                self.name,
                RefusalReason.DAILY_LOSS_LIMIT,
                f"daily loss {d.daily_loss:.2%} >= {self._max_daily_loss:.2%}",
                BlockScope.RISK_INCREASING,
            )
        if d.risk_increasing_blocked:
            return CheckResult.fail(
                self.name,
                RefusalReason.DRAWDOWN_EMERGENCY,
                f"risk-increasing changes blocked ({'; '.join(d.flags)}; band {d.band})",
                BlockScope.RISK_INCREASING,
            )
        return CheckResult.ok(self.name, f"band {d.band}, vol scale {d.vol_scale:.3f}")
