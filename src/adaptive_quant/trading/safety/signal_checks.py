"""Pre-trade check: every eligible strategy must have produced a valid signal."""

from __future__ import annotations

from collections.abc import Callable

from adaptive_quant.quant.strategies.runner import SignalBatch
from adaptive_quant.trading.safety.preflight import CheckResult, RefusalReason


class SignalGenerationCheck:
    """Refuses trading if any strategy failed, was not ready, or none ran."""

    name = "signal_generation"

    def __init__(self, batch: Callable[[], SignalBatch]) -> None:
        self._batch = batch

    def run(self) -> CheckResult:
        batch = self._batch()
        if batch.ok:
            return CheckResult.ok(self.name, f"{len(batch.signals)} strategy signal(s) produced")
        problems = batch.problems() or ["no eligible strategies produced a signal"]
        return CheckResult.fail(self.name, RefusalReason.SIGNAL_FAILURE, "; ".join(problems))
