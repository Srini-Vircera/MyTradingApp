from pathlib import Path

import pytest

from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.enums import TradingMode
from adaptive_quant.core.errors import TradingRefused
from adaptive_quant.trading.safety.kill_switch import (
    RELEASE_CONFIRMATION,
    FileKillSwitchStore,
    KillSwitch,
)
from adaptive_quant.trading.safety.preflight import (
    BlockScope,
    CallableCheck,
    CheckResult,
    KillSwitchCheck,
    PreflightGate,
    RefusalReason,
    TradingModeCheck,
)


def passing(name: str = "ok_check") -> CallableCheck:
    return CallableCheck(name, RefusalReason.MARKET_DATA_STALE, lambda: (True, "fresh"))


def failing(reason: RefusalReason, scope: BlockScope = BlockScope.ALL) -> CallableCheck:
    return CallableCheck(reason.value, reason, lambda: (False, "bad"), scope)


class Exploding:
    name = "exploding"

    def run(self) -> CheckResult:
        raise RuntimeError("broker SDK blew up")


class WrongType:
    name = "wrong_type"

    def run(self) -> CheckResult:
        return True  # type: ignore[return-value]


def test_all_pass(clock: FrozenClock) -> None:
    report = PreflightGate([passing("a"), passing("b")], clock).evaluate()
    assert report.may_increase_risk and report.may_reduce_risk
    report.raise_if_blocked(risk_increasing=True)


def test_no_checks_means_refuse(clock: FrozenClock) -> None:
    report = PreflightGate([], clock).evaluate()
    assert not report.may_increase_risk and not report.may_reduce_risk
    assert report.reasons == [RefusalReason.NO_CHECKS_CONFIGURED]


def test_exception_fails_closed(clock: FrozenClock) -> None:
    report = PreflightGate([passing(), Exploding()], clock).evaluate()
    assert report.reasons == [RefusalReason.CHECK_ERROR]
    assert "broker SDK blew up" in report.summary()
    assert not report.may_reduce_risk


def test_wrong_return_type_fails_closed(clock: FrozenClock) -> None:
    report = PreflightGate([WrongType()], clock).evaluate()
    assert report.reasons == [RefusalReason.CHECK_ERROR]


def test_all_failures_are_reported_not_just_first(clock: FrozenClock) -> None:
    checks = [
        failing(RefusalReason.MARKET_DATA_STALE),
        passing(),
        failing(RefusalReason.POSITIONS_UNCONFIRMED),
        failing(RefusalReason.DUPLICATE_ORDERS),
    ]
    report = PreflightGate(checks, clock).evaluate()
    assert report.reasons == [
        RefusalReason.MARKET_DATA_STALE,
        RefusalReason.POSITIONS_UNCONFIRMED,
        RefusalReason.DUPLICATE_ORDERS,
    ]
    with pytest.raises(TradingRefused, match="3 of 4"):
        report.raise_if_blocked(risk_increasing=False)


def test_risk_increasing_scope_allows_risk_reduction(clock: FrozenClock) -> None:
    report = PreflightGate(
        [failing(RefusalReason.DRAWDOWN_EMERGENCY, BlockScope.RISK_INCREASING)], clock
    ).evaluate()
    assert not report.may_increase_risk
    assert report.may_reduce_risk
    report.raise_if_blocked(risk_increasing=False)
    with pytest.raises(TradingRefused):
        report.raise_if_blocked(risk_increasing=True)


def test_mixed_scopes_block_everything(clock: FrozenClock) -> None:
    report = PreflightGate(
        [
            failing(RefusalReason.KILL_SWITCH_ENGAGED, BlockScope.RISK_INCREASING),
            failing(RefusalReason.MARKET_DATA_MISSING),
        ],
        clock,
    ).evaluate()
    assert not report.may_reduce_risk


class TestKillSwitchCheck:
    def _switch(self, tmp_path: Path, clock: FrozenClock) -> KillSwitch:
        return KillSwitch(FileKillSwitchStore(tmp_path / "ks.json", tmp_path / "a.jsonl"), clock)

    def test_engaged_blocks_risk_increasing_only(self, tmp_path: Path, clock: FrozenClock) -> None:
        report = PreflightGate([KillSwitchCheck(self._switch(tmp_path, clock))], clock).evaluate()
        assert report.reasons == [RefusalReason.KILL_SWITCH_ENGAGED]
        assert report.may_reduce_risk and not report.may_increase_risk

    def test_can_block_everything(self, tmp_path: Path, clock: FrozenClock) -> None:
        ks = self._switch(tmp_path, clock)
        report = PreflightGate([KillSwitchCheck(ks, allow_risk_reducing=False)], clock).evaluate()
        assert not report.may_reduce_risk

    def test_released_passes(self, tmp_path: Path, clock: FrozenClock) -> None:
        ks = self._switch(tmp_path, clock)
        ks.release(actor="srini", reason="ready", confirmation=RELEASE_CONFIRMATION)
        assert PreflightGate([KillSwitchCheck(ks)], clock).evaluate().may_increase_risk


@pytest.mark.parametrize(
    ("mode", "passes"),
    [
        (TradingMode.BACKTEST, False),
        (TradingMode.SHADOW, False),
        (TradingMode.PAPER, True),
        (TradingMode.LIVE, True),
    ],
)
def test_trading_mode_check(mode: TradingMode, passes: bool) -> None:
    assert TradingModeCheck(mode).run().passed is passes


def test_every_required_refusal_condition_exists() -> None:
    """The ten conditions from the requirements must each have a named reason."""
    required = {
        "market_data_stale",
        "market_data_missing",
        "broker_unavailable",
        "positions_unconfirmed",
        "equity_unconfirmed",
        "duplicate_orders",
        "uncertain_open_orders",
        "signal_failure",
        "risk_calculation_failure",
        "reconciliation_failure",
    }
    assert required <= {r.value for r in RefusalReason}
