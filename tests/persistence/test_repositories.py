"""Repositories against real PostgreSQL: atomicity, idempotency and database-level safety."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from adaptive_quant.core.enums import StrategyLifecycle as L
from adaptive_quant.core.errors import OrderStateError, PersistenceError
from adaptive_quant.governance.lifecycle import Actor, ActorKind, transition
from adaptive_quant.persistence import models as m
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.records import (
    CycleRecord,
    DataSnapshotRecord,
    ExecutionRecord,
    OrderUpdate,
)
from adaptive_quant.persistence.repositories import (
    CycleRepository,
    MonitoringRepository,
    ReferenceRepository,
)
from adaptive_quant.quant.strategies.catalog import StrategyCatalog
from adaptive_quant.trading.audit import (
    lifecycle_record,
    order_repository,
    strategy_version_record,
)
from tests.persistence.helpers import CFG, CYCLE, NOW, config, order, seed, signals
from tests.unit.backtest.helpers import SETTINGS


def count(db: Database, model: type[m.Base]) -> int:
    with db.session() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_reference_data_is_idempotent(db: Database) -> None:
    ref = ReferenceRepository(db)
    ref.record_config(config())
    ref.record_config(config())
    assert count(db, m.ConfigVersion) == 1
    ref.sync_instruments(SETTINGS.universe.instruments)
    ref.sync_instruments(SETTINGS.universe.instruments)
    with db.session() as s:
        tqqq = s.get(m.InstrumentRow, "TQQQ")
        assert tqqq is not None and tqqq.leverage == 3.0 and tqqq.tradeable
    v = StrategyCatalog.from_config(SETTINGS.strategies).get("st_ema_cross").version
    a = ref.ensure_strategy_version(strategy_version_record(v))
    assert ref.ensure_strategy_version(strategy_version_record(v)) == a
    snap = DataSnapshotRecord(
        "file", "QQQ", "1d", "all", NOW, NOW, 10, "h" * 64, False, "var/x.parquet", NOW
    )
    sid = ref.record_data_snapshot(snap)
    assert ref.record_data_snapshot(snap) == sid
    ref.record_quality_report(sid, True, [], NOW)


def test_lifecycle_history_is_append_only(db: Database) -> None:
    ref = ReferenceRepository(db)
    v = StrategyCatalog.from_config(SETTINGS.strategies).get("st_ema_cross").version
    ref.ensure_strategy_version(strategy_version_record(v))
    assert ref.current_lifecycle(v.strategy_id, v.version_id) is None
    t1 = transition(
        strategy_id=v.strategy_id,
        strategy_version=v.version_id,
        current=L.RESEARCH,
        target=L.VALIDATED,
        actor=Actor("research-pipeline", ActorKind.SYSTEM),
        reason="all research gates passed",
        at=NOW,
    )
    ref.record_lifecycle_event(lifecycle_record(t1))
    assert ref.current_lifecycle(v.strategy_id, v.version_id) == "validated"
    with pytest.raises(PersistenceError, match="append-only"), db.session() as s:
        s.execute(text("UPDATE strategy_lifecycle_events SET to_state = 'live_approved'"))
    with pytest.raises(PersistenceError, match="not registered"):
        ref.current_lifecycle("nope", "x")


def test_cycle_lifecycle(db: Database) -> None:
    cycles = CycleRepository(db)
    with pytest.raises(PersistenceError, match="config version"):
        cycles.start(CycleRecord(CYCLE, date(2024, 1, 2), "paper", "paper", CFG, "run-1", NOW))
    seed(db)
    with pytest.raises(PersistenceError, match="already exists"):
        cycles.start(CycleRecord(CYCLE, date(2024, 1, 2), "paper", "paper", CFG, "run-1", NOW))
    cycles.finish(CYCLE, "completed", NOW + timedelta(minutes=5))
    with pytest.raises(PersistenceError, match="already finished"):
        cycles.finish(CYCLE, "failed", NOW)
    with pytest.raises(PersistenceError, match=r"append-only|no_delete"), db.session() as s:
        s.execute(text(f"DELETE FROM trading_cycles WHERE cycle_id = '{CYCLE}'"))


def test_decision_chain_is_written_atomically(db: Database) -> None:
    rec = seed(db)
    assert count(db, m.StrategySignalRow) == 2 and count(db, m.RiskDecisionRow) == 1
    with db.session() as s:
        risk = s.get(m.RiskDecisionRow, "dec-1")
        assert risk is not None and risk.drawdown_band == "caution"
        assert risk.adjustments_json == rec.risk.adjustments
        target = s.get(m.TargetPortfolioRow, "dec-1")
        assert target is not None and target.cash_weight == rec.target.cash_weight
    # a second decision with an unregistered strategy version: nothing of it is stored
    bad_sig = signals()[0].model_copy(update={"strategy_version": "unregistered#1"})
    bad = type(rec)(
        **{
            **rec.__dict__,
            "signals": [("unregistered#1", bad_sig)],
            "risk": type(rec.risk)(**{**rec.risk.__dict__, "decision_id": "dec-2"}),
            "target": rec.target.model_copy(update={"decision_id": "dec-2"}),
        }
    )
    with pytest.raises(PersistenceError, match="not registered"):
        CycleRepository(db).record_decision(bad, CFG)
    assert count(db, m.RiskDecisionRow) == 1 and count(db, m.PortfolioProposalRow) == 1
    assert count(db, m.EnsembleDecisionRow) == 1


@pytest.mark.parametrize("table", ["risk_decisions", "strategy_signals", "target_portfolios"])
def test_decisions_cannot_be_rewritten(db: Database, table: str) -> None:
    seed(db)
    for sql in (f"UPDATE {table} SET created_at = now()", f"DELETE FROM {table}"):
        with pytest.raises(PersistenceError, match="append-only"), db.session() as s:
            s.execute(text(sql))


def test_signal_lookahead_is_rejected_by_the_database(db: Database) -> None:
    seed(db)
    with pytest.raises(PersistenceError, match="no_lookahead"), db.session() as s:
        s.execute(
            text(
                "INSERT INTO strategy_signals (cycle_id, strategy_version_id, strategy_id, "
                "timestamp, data_timestamp, direction, raw_score, normalized_score, confidence, "
                "suggested_exposure, reason, indicator_values_json) "
                "SELECT :c, id, strategy_id, now(), now() + interval '1 day', "
                "'neutral', 0, 0, 0, 0, 'x', '{}' FROM strategy_versions LIMIT 1"
            ),
            {"c": CYCLE},
        )


def test_order_intent_lifecycle(db: Database) -> None:
    seed(db)
    orders = order_repository(db)
    req = order()
    orders.create_intent(CYCLE, req, NOW)
    assert orders.get_state(req.client_order_id) == "created"
    with pytest.raises(PersistenceError, match="do not transmit"):
        orders.create_intent(CYCLE, req, NOW)  # duplicate client_order_id
    with pytest.raises(PersistenceError, match="do not transmit"):
        orders.create_intent(CYCLE, order("QQQ", decision="no-such-decision"), NOW)
    for st in ("validated", "submitted"):
        orders.transition(req.client_order_id, OrderUpdate(st, NOW))
    orders.transition(req.client_order_id, OrderUpdate("unknown", NOW, raw={"why": "timeout"}))
    with pytest.raises(OrderStateError):  # state machine: never resubmit an UNKNOWN order
        orders.transition(req.client_order_id, OrderUpdate("submitted", NOW))
    assert [x[0] for x in orders.open_intents()] == [req.client_order_id]
    orders.transition(
        req.client_order_id, OrderUpdate("filled", NOW, "brk-1", Decimal("10"), Decimal("50.1"))
    )
    assert orders.open_intents() == []
    ex = ExecutionRecord(
        req.client_order_id,
        "exec-1",
        Decimal("10"),
        Decimal("50.1"),
        Decimal("0"),
        NOW,
        Decimal("50"),
    )
    assert orders.record_execution(ex) is True
    assert orders.record_execution(ex) is False  # duplicate broker report ignored
    with db.session() as s:
        e = s.scalars(select(m.Execution)).one()
        assert e.slippage_bps == pytest.approx(20.0)
        events = s.scalars(select(m.OrderEvent.to_state).order_by(m.OrderEvent.id)).all()
    assert events == ["created", "validated", "submitted", "unknown", "filled"]


@pytest.mark.parametrize(
    ("setup", "sql", "match"),
    [
        (
            ["validated", "submitted", "unknown"],
            "UPDATE order_intents SET state = 'submitted'",
            "never resubmit",
        ),
        (["rejected"], "UPDATE order_intents SET state = 'validated'", "terminal"),
        ([], "UPDATE order_intents SET quantity = 999", "immutable"),
        ([], "DELETE FROM order_intents", "append-only"),
    ],
)
def test_database_guards_orders_even_if_code_is_bypassed(
    db: Database, setup: list[str], sql: str, match: str
) -> None:
    seed(db)
    orders = order_repository(db)
    orders.create_intent(CYCLE, order(), NOW)
    for st in setup:
        orders.transition(order().client_order_id, OrderUpdate(st, NOW))
    with pytest.raises(PersistenceError, match=match), db.session() as s:
        s.execute(text(sql))


def test_snapshots_shadow_orders_and_monitoring(db: Database) -> None:
    seed(db)
    cycles = CycleRepository(db)
    cycles.record_preflight(CYCLE, True, [{"name": "x", "passed": True}], NOW)
    cycles.record_account(CYCLE, Decimal("100000"), Decimal("1000"), Decimal("2000"), True, NOW)
    cycles.record_positions(CYCLE, "broker", [("QQQ", Decimal("100"), Decimal("40000"))], NOW)
    cycles.record_reconciliation(CYCLE, True, [], NOW)
    cycles.record_shadow_order(CYCLE, order("QQQ", 1))
    with pytest.raises(ValueError, match="position source"):
        cycles.record_positions(CYCLE, "guess", [], NOW)
    mon = MonitoringRepository(db)
    mon.record_system_event("info", "test", "started", {"a": 1}, NOW)
    mon.record_error("test", ValueError("boom"), NOW, CYCLE)
    mon.record_kill_switch_event(True, "tester", "drill", NOW)
    mon.record_notification("cycle_done", "info", "email", False, NOW, "smtp down")
    for pnl in ("10", "20"):  # corrections of the same session replace the row
        mon.upsert_daily_performance(
            date(2024, 1, 2),
            "paper",
            Decimal("100000"),
            Decimal(pnl),
            0.001,
            0.0,
            0.1,
            {"QQQ": 0.002},
        )
    assert count(db, m.DailyPerformance) == 1
    with db.session() as s:
        assert s.scalars(select(m.DailyPerformance.pnl)).one() == Decimal("20.00")
