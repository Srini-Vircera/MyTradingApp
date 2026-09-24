"""Order manager + reconciliation against real PostgreSQL and a fault-injecting broker.

Covers the M9 acceptance scenarios: API timeout, duplicate submission, partial
fill, rejection, restart mid-trade, unexpected position, existing unknown order,
insufficient buying power, shadow mode, database outage and reconciliation halt.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.enums import OrderSide, TradingMode
from adaptive_quant.core.errors import GovernanceError, OrderStateError, SafetyViolation
from adaptive_quant.core.models import TargetPortfolio
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.records import OrderUpdate
from adaptive_quant.persistence.repositories import CycleRepository
from adaptive_quant.trading.audit import order_repository
from adaptive_quant.trading.brokers.simulated import SimulatedBroker
from adaptive_quant.trading.orders.manager import OrderManager
from adaptive_quant.trading.orders.planner import OrderPlan, OrderPlanner, PlanningInput
from adaptive_quant.trading.reconciliation import reconciler as rc
from adaptive_quant.trading.safety.broker_checks import BrokerStateCheck, ReconciliationCheck
from adaptive_quant.trading.safety.preflight import BlockScope, RefusalReason
from tests.persistence.helpers import CYCLE, seed
from tests.unit.backtest.helpers import SETTINGS

pytestmark = pytest.mark.postgres
NOW = datetime(2024, 1, 2, 20, 50, tzinfo=UTC)
PRICES = {
    "QQQ": Decimal("400"),
    "TQQQ": Decimal("50"),
    "SQQQ": Decimal("20"),
    "SPY": Decimal("470"),
}


class Rig:
    def __init__(
        self, db: Database, mode: TradingMode = TradingMode.PAPER, **broker_kw: object
    ) -> None:
        seed(db)
        self.db = db
        self.clock = FrozenClock(NOW)
        self.broker = SimulatedBroker(self.clock, dict(PRICES), **broker_kw)  # type: ignore[arg-type]
        self.orders = order_repository(db)
        self.cycles = CycleRepository(db)
        self.manager = OrderManager(self.broker, self.orders, self.cycles, self.clock, mode)
        trading = SETTINGS.trading.model_copy(
            update={"allow_fractional_shares": False, "sizing_cash_buffer": 0.0}
        )
        self.planner = OrderPlanner(trading, SETTINGS.risk, SETTINGS.universe.by_symbol, True)

    def plan(self, **weights: str) -> OrderPlan:
        tgt = TargetPortfolio(
            as_of=NOW,
            weights={k: Decimal(v) for k, v in weights.items()},
            decision_id="dec-1",
            config_version="paper-test0001",
        )
        inp = PlanningInput(
            cycle_id=CYCLE,
            target=tgt,
            account=self.broker.get_account(),
            positions=self.broker.get_positions(),
            broker_open_orders=self.broker.get_open_orders(),
            internal_open=self.orders.intents(
                states=(
                    "created",
                    "validated",
                    "submitted",
                    "acknowledged",
                    "partially_filled",
                    "unknown",
                )
            ),
            prices=self.broker.prices,
            reference_prices=PRICES,
        )
        return self.planner.plan(
            inp, lambda sym, side: self.orders.next_sequence(CYCLE, sym, side.value)
        )


def test_happy_path_fills_and_records_executions(db: Database) -> None:
    r = Rig(db, slippage_bps=Decimal(10))
    plan = r.plan(QQQ="0.5")
    report = r.manager.execute(CYCLE, plan)
    (o,) = report.outcomes
    assert o.state == "filled" and report.transmitted == 1
    with db.session() as s:
        slip = s.execute(text("SELECT slippage_bps, qty FROM executions")).one()
    assert slip[0] == pytest.approx(10.0) and slip[1] == Decimal("125")
    assert r.plan(QQQ="0.5").orders == ()  # re-planning is idempotent


def test_timeout_after_accept_is_investigated_never_resubmitted(db: Database) -> None:
    r = Rig(db)
    r.broker.inject("timeout_after_accept")
    (o,) = r.manager.execute(CYCLE, r.plan(QQQ="0.5")).outcomes
    assert o.state == "filled" and "outcome unknown" in o.detail
    assert r.broker.submit_calls == 1
    states = [
        x[0]
        for x in db.engine.connect()
        .execute(text("SELECT to_state FROM order_events ORDER BY id"))
        .all()
    ]
    assert states == ["created", "validated", "submitted", "unknown", "filled"]


def test_lost_order_stays_unknown_blocks_planning_then_recovery_cancels(db: Database) -> None:
    r = Rig(db)
    r.broker.inject("timeout_before_accept")
    (o,) = r.manager.execute(CYCLE, r.plan(QQQ="0.5")).outcomes
    assert o.state == "unknown" and "never resubmitted" in o.detail
    blocked = r.plan(QQQ="0.5")  # existing unknown order: no new orders at all
    assert blocked.refused is RefusalReason.UNCERTAIN_OPEN_ORDERS
    (rec,) = r.manager.recover()
    assert rec.state == "cancelled" and "not resubmitted" in rec.detail
    assert r.broker.submit_calls == 1
    again = r.plan(QQQ="0.5")  # a fresh decision may now plan a NEW order (new sequence)
    assert again.ok and again.orders[0].client_order_id != o.client_order_id


def test_duplicate_submission_is_impossible(db: Database) -> None:
    r = Rig(db, fill_ratio=Decimal(0))  # order rests at the broker
    plan = r.plan(QQQ="0.5")
    r.manager.execute(CYCLE, plan)
    second = r.manager.execute(CYCLE, plan)  # same plan replayed (e.g. retry loop, restart)
    assert second.halted is not None and second.transmitted == 0
    assert r.broker.submit_calls == 1
    assert r.plan(QQQ="0.5").orders == ()  # working order counted: nothing new


def test_broker_side_duplicate_is_adopted(db: Database) -> None:
    r = Rig(db)
    plan = r.plan(QQQ="0.5")
    r.broker.submit_order(plan.orders[0])  # the broker already has it (response was lost earlier)
    (o,) = r.manager.execute(CYCLE, plan).outcomes
    assert o.state == "filled" and "duplicate" in o.detail


def test_partial_fill_then_completion(db: Database) -> None:
    r = Rig(db, fill_ratio=Decimal("0.4"))
    (o,) = r.manager.execute(CYCLE, r.plan(QQQ="0.5")).outcomes
    assert o.state == "partially_filled"
    assert r.plan(QQQ="0.5").orders == ()  # the remaining 75 shares are still working
    r.broker.fill_open_orders()
    (synced,) = r.manager.sync(CYCLE)
    assert synced.state == "filled"
    fills = r.orders.executions_for_cycle(CYCLE)
    assert sum(q for _, _, q in fills) == Decimal("125") and len(fills) == 2


def test_rejection_is_final_and_not_retried(db: Database) -> None:
    r = Rig(db)
    r.broker.inject("reject")
    (o,) = r.manager.execute(CYCLE, r.plan(QQQ="0.5")).outcomes
    assert o.state == "rejected" and r.broker.submit_calls == 1
    with pytest.raises(OrderStateError, match="illegal order transition rejected -> submitted"):
        r.orders.transition(o.client_order_id, OrderUpdate("submitted", NOW))


def test_insufficient_buying_power_at_the_broker_is_a_rejection(db: Database) -> None:
    r = Rig(db, cash=Decimal(100_000))
    plan = r.plan(QQQ="0.5")
    r.broker.cash = Decimal(1000)  # buying power collapsed after planning
    (o,) = r.manager.execute(CYCLE, plan).outcomes
    assert o.state == "rejected" and "buying power" in o.detail


def test_restart_mid_trade_recovers_without_duplicates(db: Database) -> None:
    r = Rig(db)
    plan = r.plan(QQQ="0.3", TQQQ="0.2")
    tqqq, qqq = sorted(plan.orders, key=lambda o: o.symbol, reverse=True)
    # crash 1: QQQ was marked SUBMITTED and transmitted, the process died before the response
    r.orders.create_intent(CYCLE, qqq, NOW)
    for st in ("validated", "submitted"):
        r.orders.transition(qqq.client_order_id, OrderUpdate(st, NOW))
    r.broker.submit_order(qqq)
    # crash 2: TQQQ was persisted and validated but never transmitted
    r.orders.create_intent(CYCLE, tqqq, NOW)
    r.orders.transition(tqqq.client_order_id, OrderUpdate("validated", NOW))
    restarted = OrderManager(r.broker, order_repository(db), r.cycles, r.clock, TradingMode.PAPER)
    outcomes = {o.symbol: o for o in restarted.recover()}
    assert outcomes["QQQ"].state == "filled" and outcomes["TQQQ"].state == "cancelled"
    assert r.broker.submit_calls == 1  # recovery never transmits
    replan = r.plan(QQQ="0.3", TQQQ="0.2")
    assert [(o.symbol, o.client_order_id != tqqq.client_order_id) for o in replan.orders] == [
        ("TQQQ", True)
    ]


def test_shadow_mode_never_submits(db: Database) -> None:
    r = Rig(db, mode=TradingMode.SHADOW)
    report = r.manager.execute(CYCLE, r.plan(QQQ="0.5", TQQQ="0.2"))
    assert r.broker.submit_calls == 0 and report.transmitted == 0
    assert {o.state for o in report.outcomes} == {"shadow"}
    with db.session() as s:
        assert s.execute(text("SELECT count(*) FROM shadow_orders")).scalar() == 2
        assert s.execute(text("SELECT count(*) FROM order_intents")).scalar() == 0


def test_database_outage_transmits_nothing(db: Database, pg_server_url: str) -> None:
    from sqlalchemy import create_engine

    r = Rig(db)
    plan = r.plan(QQQ="0.5")
    admin = create_engine(pg_server_url, isolation_level="AUTOCOMMIT")
    name = db.url.database
    with admin.connect() as c:
        c.execute(text(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false'))
        c.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name},
        )
    try:
        report = r.manager.execute(CYCLE, plan)
    finally:
        with admin.connect() as c:
            c.execute(text(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true'))
        admin.dispose()
    assert report.halted is not None and "database" in report.halted
    assert r.broker.submit_calls == 0


def test_sells_first_phase(db: Database) -> None:
    r = Rig(db, positions={"TQQQ": Decimal(600)}, cash=Decimal(70_000))
    plan = r.plan(QQQ="0.5")
    sells = r.manager.execute(CYCLE, plan, only_side=OrderSide.SELL)
    assert [o.side for o in sells.outcomes] == ["sell"]
    buys = r.manager.execute(CYCLE, r.plan(QQQ="0.5"), only_side=OrderSide.BUY)
    assert [(o.symbol, o.state) for o in buys.outcomes] == [("QQQ", "filled")]


def test_live_and_mismatched_modes_are_refused(db: Database) -> None:
    r = Rig(db)
    for mode in (TradingMode.LIVE, TradingMode.BACKTEST):
        with pytest.raises(SafetyViolation):
            OrderManager(r.broker, r.orders, r.cycles, r.clock, mode)


def test_reconciliation_halts_on_unexpected_position_until_acknowledged(db: Database) -> None:
    r = Rig(db)
    start = {p.symbol: p.quantity for p in r.broker.get_positions()}
    r.manager.execute(CYCLE, r.plan(QQQ="0.5"))
    r.broker.inject_position("SPY", Decimal(3))  # someone traded the account by hand
    recon = rc.Reconciler(1e-6, 1.0)
    expected = rc.expected_positions(start, r.orders.executions_for_cycle(CYCLE))
    result = recon.reconcile(
        expected=expected,
        broker_positions=r.broker.get_positions(),
        internal_open=r.orders.intents(),
        broker_open=r.broker.get_open_orders(),
    )
    assert not result.passed and result.differences[0]["kind"] == "unexpected_position"
    rc.record(r.cycles, CYCLE, result, NOW)
    check = ReconciliationCheck(r.cycles.latest_reconciliation).run()
    assert (
        check.reason is RefusalReason.RECONCILIATION_FAILURE
        and check.scope is BlockScope.RISK_INCREASING
    )
    with pytest.raises(GovernanceError, match="reason"):
        rc.acknowledge(r.cycles, CYCLE, "ops", "ok", NOW)
    rc.acknowledge(
        r.cycles, CYCLE, "Jane Operator", "manual SPY purchase confirmed by the account owner", NOW
    )
    assert ReconciliationCheck(r.cycles.latest_reconciliation).run().passed
    with pytest.raises(GovernanceError, match="no failed"):
        rc.acknowledge(r.cycles, CYCLE, "Jane Operator", "nothing left to acknowledge here", NOW)


def test_reconciliation_detects_order_and_cash_mismatches(db: Database) -> None:
    r = Rig(db, fill_ratio=Decimal(0))
    r.manager.execute(CYCLE, r.plan(QQQ="0.5"))
    recon = rc.Reconciler(1e-6, 1.0)
    ok = recon.reconcile(
        expected={},
        broker_positions=[],
        internal_open=r.orders.intents(),
        broker_open=r.broker.get_open_orders(),
        expected_cash=Decimal(100_000),
        broker_cash=r.broker.cash,
    )
    assert ok.passed
    bid = r.broker.get_open_orders()[0].broker_order_id
    assert bid is not None
    r.broker.cancel_order(bid)  # cancelled behind our back
    bad = recon.reconcile(
        expected={},
        broker_positions=[],
        internal_open=r.orders.intents(),
        broker_open=r.broker.get_open_orders(),
        expected_cash=Decimal(100_000),
        broker_cash=Decimal(99_000),
    )
    assert {d["kind"] for d in bad.differences} == {"order_missing_at_broker", "cash_mismatch"}


def test_broker_state_check(db: Database) -> None:
    r = Rig(db)
    assert BrokerStateCheck(r.broker).run().passed
    r.broker.set_unavailable()
    res = BrokerStateCheck(r.broker).run()
    assert res.reason is RefusalReason.BROKER_UNAVAILABLE and res.scope is BlockScope.ALL
    r.broker.set_unavailable(False)
    r.broker.trading_blocked = True
    assert not BrokerStateCheck(r.broker).run().passed
