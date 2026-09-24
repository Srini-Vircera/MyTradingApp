"""Pre-trade checks on broker state and the last reconciliation."""

from __future__ import annotations

from collections.abc import Callable

from adaptive_quant.core.errors import AQError
from adaptive_quant.persistence.records import ReconciliationView
from adaptive_quant.trading.brokers.base import BrokerAdapter
from adaptive_quant.trading.safety.preflight import BlockScope, CheckResult, RefusalReason


class BrokerStateCheck:
    """Account, positions and open orders must be readable; the account must be
    a paper account that is not blocked (equity and positions confirmed)."""

    name = "broker_state"

    def __init__(self, broker: BrokerAdapter) -> None:
        self._broker = broker

    def run(self) -> CheckResult:
        try:
            account = self._broker.get_account()
        except (AQError, Exception) as exc:  # noqa: BLE001 - any failure blocks trading
            return CheckResult.fail(self.name, RefusalReason.BROKER_UNAVAILABLE, f"account: {exc}")
        if account.equity <= 0:
            return CheckResult.fail(
                self.name, RefusalReason.EQUITY_UNCONFIRMED, f"equity {account.equity}"
            )
        if not account.is_paper:
            return CheckResult.fail(
                self.name, RefusalReason.BROKER_UNAVAILABLE, "account is not a paper account"
            )
        if account.trading_blocked:
            return CheckResult.fail(
                self.name, RefusalReason.BROKER_UNAVAILABLE, "account is blocked"
            )
        try:
            self._broker.get_positions()
        except (AQError, Exception) as exc:  # noqa: BLE001
            return CheckResult.fail(
                self.name, RefusalReason.POSITIONS_UNCONFIRMED, f"positions: {exc}"
            )
        try:
            self._broker.get_open_orders()
        except (AQError, Exception) as exc:  # noqa: BLE001
            return CheckResult.fail(
                self.name, RefusalReason.UNCERTAIN_OPEN_ORDERS, f"open orders: {exc}"
            )
        return CheckResult.ok(
            self.name, f"equity {account.equity}, buying power {account.buying_power}"
        )


class ReconciliationCheck:
    """The most recent reconciliation must have passed (or been acknowledged by a human)."""

    name = "reconciliation"

    def __init__(self, latest: Callable[[], ReconciliationView | None]) -> None:
        self._latest = latest

    def run(self) -> CheckResult:
        try:
            last = self._latest()
        except (AQError, Exception) as exc:  # noqa: BLE001
            return CheckResult.fail(
                self.name, RefusalReason.RECONCILIATION_FAILURE, f"cannot read: {exc}"
            )
        if last is None or last.passed:
            return CheckResult.ok(self.name, "no outstanding discrepancy")
        kinds = sorted({str(d.get("kind")) for d in last.differences.get("differences", [])})
        return CheckResult.fail(
            self.name,
            RefusalReason.RECONCILIATION_FAILURE,
            f"reconciliation of {last.cycle_id} failed ({', '.join(kinds)}); "
            "a human must acknowledge",
            BlockScope.RISK_INCREASING,
        )
