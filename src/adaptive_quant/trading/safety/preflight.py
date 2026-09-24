"""Pre-trade safety gate: the system refuses to trade unless every check passes.

Design rules
------------
* **Fail closed.** A check that raises, returns the wrong type, or is missing
  counts as a failure. An empty check list is itself a failure.
* **All checks run** (no short-circuit) so the operator sees every problem at
  once, not one per cycle.
* Each failure has a :class:`BlockScope`:

  - ``ALL``             - no orders of any kind (e.g. stale data, unknown positions);
  - ``RISK_INCREASING`` - only risk-reducing orders may proceed (e.g. kill switch).

Concrete checks for data freshness, broker state and reconciliation are plugged
in by later milestones; they only need to satisfy :class:`PreflightCheck`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from adaptive_quant.core.clock import Clock
from adaptive_quant.core.enums import TradingMode
from adaptive_quant.core.errors import TradingRefused
from adaptive_quant.observability.logging import get_logger
from adaptive_quant.trading.safety.kill_switch import KillSwitch

_log = get_logger(__name__)


class RefusalReason(StrEnum):
    """Every condition under which the platform refuses to trade."""

    MARKET_DATA_STALE = "market_data_stale"
    MARKET_DATA_MISSING = "market_data_missing"
    MARKET_DATA_INVALID = "market_data_invalid"
    BROKER_UNAVAILABLE = "broker_unavailable"
    POSITIONS_UNCONFIRMED = "positions_unconfirmed"
    EQUITY_UNCONFIRMED = "equity_unconfirmed"
    DUPLICATE_ORDERS = "duplicate_orders"
    UNCERTAIN_OPEN_ORDERS = "uncertain_open_orders"
    SIGNAL_FAILURE = "signal_failure"
    RISK_CALCULATION_FAILURE = "risk_calculation_failure"
    RECONCILIATION_FAILURE = "reconciliation_failure"
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    DRAWDOWN_EMERGENCY = "drawdown_emergency"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MODE_DOES_NOT_SUBMIT = "mode_does_not_submit"
    CHECK_ERROR = "check_error"
    NO_CHECKS_CONFIGURED = "no_checks_configured"


class BlockScope(StrEnum):
    ALL = "all"
    RISK_INCREASING = "risk_increasing"


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    reason: RefusalReason | None = None
    scope: BlockScope = BlockScope.ALL

    @classmethod
    def ok(cls, name: str, detail: str = "ok") -> CheckResult:
        return cls(name=name, passed=True, detail=detail)

    @classmethod
    def fail(
        cls,
        name: str,
        reason: RefusalReason,
        detail: str,
        scope: BlockScope = BlockScope.ALL,
    ) -> CheckResult:
        return cls(name=name, passed=False, detail=detail, reason=reason, scope=scope)


class PreflightCheck(Protocol):
    """One independent safety condition."""

    @property
    def name(self) -> str: ...

    def run(self) -> CheckResult: ...


@dataclass(frozen=True)
class PreflightReport:
    checked_at: datetime
    results: tuple[CheckResult, ...]
    failures: tuple[CheckResult, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", tuple(r for r in self.results if not r.passed))

    @property
    def may_increase_risk(self) -> bool:
        """True only if every check passed."""
        return not self.failures

    @property
    def may_reduce_risk(self) -> bool:
        """True if no failure blocks *all* trading."""
        return all(f.scope is BlockScope.RISK_INCREASING for f in self.failures)

    @property
    def reasons(self) -> list[RefusalReason]:
        return [f.reason for f in self.failures if f.reason is not None]

    def summary(self) -> str:
        if not self.failures:
            return f"all {len(self.results)} safety checks passed"
        lines = [f"{len(self.failures)} of {len(self.results)} safety checks failed:"]
        lines += [f"  - [{f.scope}] {f.name}: {f.detail}" for f in self.failures]
        return "\n".join(lines)

    def raise_if_blocked(self, *, risk_increasing: bool) -> None:
        allowed = self.may_increase_risk if risk_increasing else self.may_reduce_risk
        if not allowed:
            raise TradingRefused(self.summary(), hint="resolve the failed checks listed above")


class PreflightGate:
    def __init__(self, checks: Sequence[PreflightCheck], clock: Clock) -> None:
        self._checks = tuple(checks)
        self._clock = clock

    def evaluate(self) -> PreflightReport:
        results = [self._run_one(c) for c in self._checks]
        if not results:
            results.append(
                CheckResult.fail(
                    "preflight",
                    RefusalReason.NO_CHECKS_CONFIGURED,
                    "no safety checks are configured; refusing to trade",
                )
            )
        report = PreflightReport(checked_at=self._clock.now(), results=tuple(results))
        if report.failures:
            _log.warning(
                "preflight_failed",
                reasons=[r.value for r in report.reasons],
                summary=report.summary(),
            )
        else:
            _log.info("preflight_passed", checks=len(results))
        return report

    @staticmethod
    def _run_one(check: PreflightCheck) -> CheckResult:
        name = getattr(check, "name", type(check).__name__)
        try:
            result = check.run()
        except Exception as exc:  # noqa: BLE001 - fail closed on *any* check error
            _log.exception("preflight_check_error", check=name)
            return CheckResult.fail(
                name, RefusalReason.CHECK_ERROR, f"check raised {type(exc).__name__}: {exc}"
            )
        if not isinstance(result, CheckResult):
            return CheckResult.fail(
                name, RefusalReason.CHECK_ERROR, f"check returned {type(result).__name__}"
            )
        return result


# ---------------------------------------------------------------- built-in checks
class KillSwitchCheck:
    """Blocks risk-increasing orders while the kill switch is engaged."""

    name = "kill_switch"

    def __init__(self, kill_switch: KillSwitch, allow_risk_reducing: bool = True) -> None:
        self._ks = kill_switch
        self._scope = BlockScope.RISK_INCREASING if allow_risk_reducing else BlockScope.ALL

    def run(self) -> CheckResult:
        status = self._ks.status()
        if status.engaged:
            return CheckResult.fail(
                self.name, RefusalReason.KILL_SWITCH_ENGAGED, status.reason, self._scope
            )
        return CheckResult.ok(self.name, "released")


class TradingModeCheck:
    """Fails when the configured mode never transmits orders (backtest/shadow).

    Shadow mode still runs the whole pipeline; the execution layer consults this
    check to *record* instead of *submit*.
    """

    name = "trading_mode"

    def __init__(self, mode: TradingMode) -> None:
        self._mode = mode

    def run(self) -> CheckResult:
        if self._mode.submits_orders:
            return CheckResult.ok(self.name, f"mode={self._mode}")
        return CheckResult.fail(
            self.name, RefusalReason.MODE_DOES_NOT_SUBMIT, f"mode={self._mode} does not submit"
        )


class CallableCheck:
    """Adapt a function returning ``(passed, detail)`` into a :class:`PreflightCheck`."""

    def __init__(
        self,
        name: str,
        reason: RefusalReason,
        fn: Callable[[], tuple[bool, str]],
        scope: BlockScope = BlockScope.ALL,
    ) -> None:
        self.name = name
        self._reason = reason
        self._fn = fn
        self._scope = scope

    def run(self) -> CheckResult:
        passed, detail = self._fn()
        if passed:
            return CheckResult.ok(self.name, detail)
        return CheckResult.fail(self.name, self._reason, detail, self._scope)
