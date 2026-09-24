"""Order manager: persist -> validate -> mark submitted -> transmit -> track.

For every planned order:

1. the intent is **committed** (state CREATED) - if the database fails, nothing
   is transmitted and execution stops;
2. VALIDATED, then SUBMITTED is persisted *before* the broker call, so the
   database never under-reports what may exist at the broker;
3. the broker call:
   * success -> adopt the broker's state (and record fills);
   * duplicate client order id -> look the original up and adopt it;
   * definitive rejection -> REJECTED (never retried);
   * timeout / transport failure -> **UNKNOWN**, then one immediate lookup by
     ``client_order_id``; the order is **never resubmitted**.

:meth:`sync` adopts broker state for working orders; :meth:`recover` (after a
restart, or once an UNKNOWN order's grace period has passed) investigates every
non-terminal intent: found at the broker -> adopt; confirmed absent -> CANCELLED
(or REJECTED/CANCELLED for intents that were never transmitted).

Shadow mode never calls ``submit_order``: it records ``shadow_orders`` only.
Live trading is not enabled in this version.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from adaptive_quant.core.clock import Clock
from adaptive_quant.core.enums import OrderSide, OrderState, TradingMode
from adaptive_quant.core.errors import BrokerError, PersistenceError, SafetyViolation
from adaptive_quant.core.models import OrderRequest, OrderSnapshot
from adaptive_quant.persistence.records import ExecutionRecord, IntentView, OrderUpdate
from adaptive_quant.persistence.repositories import CycleRepository, OrderRepository
from adaptive_quant.trading.brokers.base import (
    BrokerAdapter,
    DuplicateClientOrderId,
    OrderRejectedByBroker,
    assert_broker_matches_mode,
)
from adaptive_quant.trading.orders.planner import OrderPlan
from adaptive_quant.trading.orders.state_machine import TERMINAL_STATES

S = OrderState
_WORKING = ("submitted", "acknowledged", "partially_filled", "unknown")
_NOT_TRANSMITTED = ("created", "validated")


@dataclass(frozen=True)
class OrderOutcome:
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    state: str
    detail: str = ""


@dataclass
class ExecutionReport:
    outcomes: list[OrderOutcome] = field(default_factory=list)
    transmitted: int = 0
    halted: str | None = None  # why execution stopped early (e.g. database unavailable)

    def state_of(self, client_order_id: str) -> str | None:
        return next((o.state for o in self.outcomes if o.client_order_id == client_order_id), None)


class OrderManager:
    def __init__(
        self,
        broker: BrokerAdapter,
        orders: OrderRepository,
        cycles: CycleRepository,
        clock: Clock,
        mode: TradingMode,
    ) -> None:
        if mode is TradingMode.BACKTEST:
            raise SafetyViolation("the order manager is not used in backtest mode")
        if mode.uses_real_money:
            raise SafetyViolation(
                "live order transmission is not enabled in this version",
                hint="paper and shadow modes only; see docs/SAFETY.md",
            )
        assert_broker_matches_mode(broker, mode)
        self.broker = broker
        self.orders = orders
        self.cycles = cycles
        self.clock = clock
        self.mode = mode

    # ------------------------------------------------------------------ execution
    def execute(
        self, cycle_id: str, plan: OrderPlan, only_side: OrderSide | None = None
    ) -> ExecutionReport:
        report = ExecutionReport()
        if not plan.ok:
            report.halted = f"plan refused: {plan.refused}: {plan.refusal_detail}"
            return report
        for req in plan.orders:
            if only_side is not None and req.side is not only_side:
                continue
            if self.mode is TradingMode.SHADOW:
                try:
                    self.cycles.record_shadow_order(cycle_id, req)
                except PersistenceError as exc:
                    report.halted = f"database: {exc.message}"
                    return report
                report.outcomes.append(
                    _outcome(req, "shadow", "recorded; not transmitted (shadow mode)")
                )
                continue
            outcome = self._execute_one(cycle_id, req, plan.expected_prices.get(req.symbol))
            if outcome is None:
                report.halted = f"database unavailable before transmitting {req.client_order_id}"
                return report
            report.outcomes.append(outcome)
            report.transmitted += 1  # a transmission was attempted (whatever its outcome)
        return report

    def _execute_one(
        self, cycle_id: str, req: OrderRequest, expected: Decimal | None
    ) -> OrderOutcome | None:
        now = self.clock.now()
        try:
            self.orders.create_intent(cycle_id, req, now)
            self.orders.transition(req.client_order_id, OrderUpdate(S.VALIDATED.value, now))
            self.orders.transition(req.client_order_id, OrderUpdate(S.SUBMITTED.value, now))
        except PersistenceError:
            return None  # nothing was transmitted; recovery resolves any CREATED/VALIDATED intent
        try:
            snap = self.broker.submit_order(req)
        except DuplicateClientOrderId:
            return self._investigate(req, "duplicate client order id at broker", expected)
        except OrderRejectedByBroker as exc:
            self._update(req.client_order_id, S.REJECTED, raw={"reason": exc.message})
            return _outcome(req, S.REJECTED.value, exc.message)
        except BrokerError as exc:
            self._update(req.client_order_id, S.UNKNOWN, raw={"reason": exc.message})
            return self._investigate(req, f"submission outcome unknown: {exc.message}", expected)
        state = self._adopt(req.client_order_id, S.SUBMITTED, snap, expected)
        return _outcome(req, state)

    def _investigate(self, req: OrderRequest, why: str, expected: Decimal | None) -> OrderOutcome:
        current = S(self.orders.get_state(req.client_order_id) or S.UNKNOWN.value)
        try:
            snap = self.broker.get_order_by_client_id(req.client_order_id)
        except BrokerError as exc:
            return _outcome(
                req, current.value, f"{why}; lookup failed ({exc.message}); stays {current}"
            )
        if snap is None:
            return _outcome(
                req, current.value, f"{why}; broker has no such order yet - never resubmitted"
            )
        return _outcome(req, self._adopt(req.client_order_id, current, snap, expected), why)

    # ------------------------------------------------------------------ tracking
    def sync(self, cycle_id: str | None = None) -> list[OrderOutcome]:
        """Adopt broker state for every working order (no cancellation of missing ones)."""
        out = []
        for i in self.orders.intents(states=_WORKING, cycle_id=cycle_id):
            try:
                snap = self.broker.get_order_by_client_id(i.client_order_id)
            except BrokerError as exc:
                out.append(_view(i, i.state, f"lookup failed: {exc.message}"))
                continue
            if snap is None:
                out.append(_view(i, i.state, "not found at broker"))
                continue
            out.append(_view(i, self._adopt(i.client_order_id, S(i.state), snap, None)))
        return out

    def recover(self) -> list[OrderOutcome]:
        """Investigate every non-terminal intent (after a restart / an UNKNOWN grace period)."""
        out = []
        for i in self.orders.intents(states=(*_NOT_TRANSMITTED, *_WORKING)):
            state = S(i.state)
            try:
                snap = self.broker.get_order_by_client_id(i.client_order_id)
            except BrokerError as exc:
                out.append(_view(i, i.state, f"broker unreachable ({exc.message}); unresolved"))
                continue
            if snap is not None:
                out.append(
                    _view(i, self._adopt(i.client_order_id, state, snap, None), "found at broker")
                )
                continue
            if state is S.CREATED:
                self._update(
                    i.client_order_id, S.REJECTED, raw={"reason": "never transmitted (restart)"}
                )
                out.append(_view(i, S.REJECTED.value, "never transmitted"))
            elif state is S.VALIDATED:
                self._update(
                    i.client_order_id, S.CANCELLED, raw={"reason": "never transmitted (restart)"}
                )
                out.append(_view(i, S.CANCELLED.value, "never transmitted"))
            else:
                if state is not S.UNKNOWN:
                    self._update(i.client_order_id, S.UNKNOWN, raw={"reason": "missing at broker"})
                self._update(
                    i.client_order_id, S.CANCELLED, raw={"reason": "broker confirms no such order"}
                )
                out.append(
                    _view(i, S.CANCELLED.value, "broker confirms no such order; not resubmitted")
                )
        return out

    # ------------------------------------------------------------------ helpers
    def _adopt(
        self, cid: str, current: OrderState, snap: OrderSnapshot, expected: Decimal | None
    ) -> str:
        target = snap.state
        if target is S.SUBMITTED and current is not S.SUBMITTED:
            # "pending_new" at the broker: it has the order; never move back to SUBMITTED
            target = S.ACKNOWLEDGED if current is S.UNKNOWN else current
        new_fill = self._record_fills(cid, snap, expected)
        if target is not current or (target is S.PARTIALLY_FILLED and new_fill):
            if current in TERMINAL_STATES:
                return current.value
            self._update(
                cid,
                target,
                broker_order_id=snap.broker_order_id,
                filled=snap.filled_quantity,
                avg=snap.avg_fill_price,
                raw=snap.raw,
            )
        return target.value

    def _record_fills(self, cid: str, snap: OrderSnapshot, expected: Decimal | None) -> bool:
        """Record the fill quantity not yet stored; True if there was any."""
        if snap.filled_quantity <= 0 or snap.avg_fill_price is None:
            return False
        done_qty, done_notional = self.orders.execution_totals(cid)
        new_qty = snap.filled_quantity - done_qty
        if new_qty <= 0:
            return False
        new_price = (snap.filled_quantity * snap.avg_fill_price - done_notional) / new_qty
        self.orders.record_execution(
            ExecutionRecord(
                client_order_id=cid,
                broker_execution_id=f"{snap.broker_order_id}:{snap.filled_quantity}",
                qty=new_qty,
                price=new_price.quantize(Decimal("0.000001")),
                fee=Decimal(0),
                at=snap.updated_at,
                expected_price=expected,
            )
        )
        return True

    def _update(
        self,
        cid: str,
        state: OrderState,
        *,
        broker_order_id: str | None = None,
        filled: Decimal | None = None,
        avg: Decimal | None = None,
        raw: dict[str, object] | None = None,
    ) -> None:
        self.orders.transition(
            cid,
            OrderUpdate(
                state.value, self.clock.now(), broker_order_id, filled, avg, dict(raw or {})
            ),
        )


def _outcome(req: OrderRequest, state: str, detail: str = "") -> OrderOutcome:
    return OrderOutcome(
        req.client_order_id, req.symbol, req.side.value, req.quantity, state, detail
    )


def _view(i: IntentView, state: str, detail: str = "") -> OrderOutcome:
    return OrderOutcome(i.client_order_id, i.symbol, i.side, i.quantity, state, detail)


def pending_orders(outcomes: Sequence[OrderOutcome]) -> list[OrderOutcome]:
    return [o for o in outcomes if o.state in _WORKING]
