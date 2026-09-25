"""Reconciliation: broker positions, open orders and cash vs. what we expect.

Expected positions = positions at the start of the cycle + confirmed fills of
our orders. Any difference beyond tolerance (including a position we never
traded, or an open order we do not know) fails reconciliation. A failed report
is persisted and blocks risk-increasing trading (``ReconciliationCheck``) until
a named human acknowledges it with a written reason.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from adaptive_quant.core.enums import OrderState
from adaptive_quant.core.errors import GovernanceError
from adaptive_quant.core.models import OrderSnapshot, Position
from adaptive_quant.persistence.records import IntentView
from adaptive_quant.persistence.repositories import CycleRepository
from adaptive_quant.trading.orders.state_machine import TERMINAL_STATES

MIN_ACK_REASON = 20


@dataclass(frozen=True)
class ReconciliationResult:
    passed: bool
    differences: list[dict[str, Any]] = field(default_factory=list)


def expected_positions(
    start: Mapping[str, Decimal], fills: Iterable[tuple[str, str, Decimal]]
) -> dict[str, Decimal]:
    """Start-of-cycle positions plus signed confirmed fills (symbol, side, qty)."""
    out = dict(start)
    for symbol, side, qty in fills:
        out[symbol] = out.get(symbol, Decimal(0)) + (qty if side == "buy" else -qty)
    return {s: q for s, q in out.items() if q != 0}


class Reconciler:
    def __init__(self, qty_tolerance: float, cash_tolerance: float) -> None:
        self.qty_tol = Decimal(str(qty_tolerance))
        self.cash_tol = Decimal(str(cash_tolerance))

    def reconcile(
        self,
        *,
        expected: Mapping[str, Decimal],
        broker_positions: Sequence[Position],
        internal_open: Sequence[IntentView],
        broker_open: Sequence[OrderSnapshot],
        expected_cash: Decimal | None = None,
        broker_cash: Decimal | None = None,
    ) -> ReconciliationResult:
        diffs: list[dict[str, Any]] = []
        actual = {p.symbol: p.quantity for p in broker_positions if p.quantity != 0}
        for sym in sorted(set(expected) | set(actual)):
            e, a = expected.get(sym, Decimal(0)), actual.get(sym, Decimal(0))
            if abs(e - a) > self.qty_tol:
                kind = "unexpected_position" if sym not in expected else "position_mismatch"
                diffs.append({"kind": kind, "symbol": sym, "expected": str(e), "broker": str(a)})
        ours = {
            i.client_order_id for i in internal_open if OrderState(i.state) not in TERMINAL_STATES
        }
        theirs = {o.client_order_id for o in broker_open}
        for cid in sorted(theirs - ours):
            diffs.append({"kind": "unknown_broker_order", "client_order_id": cid})
        for cid in sorted(ours - theirs):
            diffs.append({"kind": "order_missing_at_broker", "client_order_id": cid})
        if (
            expected_cash is not None
            and broker_cash is not None
            and abs(expected_cash - broker_cash) > self.cash_tol
        ):
            diffs.append(
                {
                    "kind": "cash_mismatch",
                    "expected": str(expected_cash),
                    "broker": str(broker_cash),
                }
            )
        return ReconciliationResult(passed=not diffs, differences=diffs)


def record(
    cycles: CycleRepository, cycle_id: str, result: ReconciliationResult, at: datetime
) -> None:
    cycles.record_reconciliation(cycle_id, result.passed, result.differences, at)


def acknowledge(
    cycles: CycleRepository, cycle_id: str, actor: str, reason: str, at: datetime
) -> None:
    """A human accepts the last failed reconciliation (appends a passing, attributed report)."""
    if not actor.strip():
        raise GovernanceError("acknowledging a reconciliation failure requires a named person")
    if len(reason.strip()) < MIN_ACK_REASON:
        raise GovernanceError(f"write a reason of at least {MIN_ACK_REASON} characters")
    last = cycles.latest_reconciliation()
    if last is None or last.passed:
        raise GovernanceError("there is no failed reconciliation to acknowledge")
    cycles.record_reconciliation(
        cycle_id,
        True,
        [
            {
                "kind": "acknowledged",
                "acknowledges": last.id,
                "actor": actor.strip(),
                "reason": reason.strip(),
            }
        ],
        at,
    )
