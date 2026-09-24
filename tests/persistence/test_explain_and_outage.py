"""The "explain decision" chain, and fail-closed behaviour when the database is down."""

import json
import socket
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from adaptive_quant.core.errors import DatabaseUnavailableError, PersistenceError
from adaptive_quant.persistence import models as m
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.explain import explain_decision
from adaptive_quant.persistence.records import ExecutionRecord, OrderUpdate
from adaptive_quant.persistence.repositories import CycleRepository
from adaptive_quant.trading.audit import order_repository
from adaptive_quant.trading.safety.db_checks import DatabaseCheck
from adaptive_quant.trading.safety.preflight import BlockScope, RefusalReason
from tests.persistence.helpers import CYCLE, NOW, STRATS, order, seed


def test_explain_returns_the_full_chain(db: Database) -> None:
    rec = seed(db)
    orders = order_repository(db)
    req = order()
    orders.create_intent(CYCLE, req, NOW)
    orders.transition(req.client_order_id, OrderUpdate("validated", NOW))
    orders.transition(req.client_order_id, OrderUpdate("submitted", NOW, "brk-9"))
    orders.transition(
        req.client_order_id, OrderUpdate("filled", NOW, "brk-9", Decimal("10"), Decimal("50"))
    )
    orders.record_execution(
        ExecutionRecord(req.client_order_id, "x-1", Decimal("10"), Decimal("50"), Decimal("0"), NOW)
    )
    CycleRepository(db).record_preflight(
        CYCLE, True, [{"name": "kill_switch", "passed": True}], NOW
    )

    out = explain_decision(db, CYCLE)
    json.dumps(out)  # fully serialisable
    assert (
        out["cycle"]["cycle_id"] == CYCLE
        and out["config"]["config_version"] == rec.target.config_version
    )
    d = out["decisions"][0]
    assert d["decision_id"] == "dec-1"
    assert d["target"]["weights_json"] == {k: str(v) for k, v in rec.target.weights.items()}
    assert [a["rule"] for a in d["risk"]["adjustments_json"]] == [
        a["rule"] for a in rec.risk.adjustments
    ]
    assert d["risk"]["drawdown_band"] == "caution"
    assert d["proposal"]["requested_exposure"] == pytest.approx(rec.proposal.requested_exposure)
    assert out["ensemble"][0]["strategy_weights_json"] == rec.ensemble.strategy_weights
    assert sorted(sig["strategy_id"] for sig in out["signals"]) == sorted(STRATS)
    versions = {sig.strategy_name: v for v, sig in rec.signals}
    for sig in out["signals"]:
        assert sig["reason"].endswith("test reason")
        assert sig["strategy_version"]["version"] == versions[sig["strategy_id"]]
        assert sig["data_timestamp"] < sig["timestamp"]
    o = out["orders"][0]
    assert [e["to_state"] for e in o["events"]] == ["created", "validated", "submitted", "filled"]
    assert o["executions"][0]["broker_execution_id"] == "x-1"
    assert out["preflight"][0]["passed"] is True
    with pytest.raises(PersistenceError, match="unknown trading cycle"):
        explain_decision(db, "cyc-nope")


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_unreachable_database_refuses_order_intents_without_leaking_the_password() -> None:
    db = Database(f"postgresql://aq:s3cr3t-pw@127.0.0.1:{_closed_port()}/x", connect_timeout_s=2)
    with pytest.raises(DatabaseUnavailableError) as err:
        order_repository(db).create_intent(CYCLE, order(), NOW)
    assert "s3cr3t-pw" not in str(err.value) and "***" in str(err.value)
    assert "s3cr3t-pw" not in repr(db)
    res = DatabaseCheck(db).run()
    assert not res.passed and res.reason is RefusalReason.DATABASE_UNAVAILABLE
    assert res.scope is BlockScope.ALL


def test_database_going_down_mid_session_refuses_the_next_intent(
    db: Database, pg_server_url: str
) -> None:
    seed(db)
    orders = order_repository(db)
    orders.create_intent(CYCLE, order("TQQQ", 0), NOW)
    name = db.url.database
    admin = create_engine(pg_server_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:  # the database becomes unreachable (connections killed and refused)
        c.execute(text(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false'))
        c.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name},
        )
    try:
        with pytest.raises(DatabaseUnavailableError):
            orders.create_intent(CYCLE, order("QQQ", 1), NOW)
        assert not DatabaseCheck(db).run().passed
    finally:
        with admin.connect() as c:
            c.execute(text(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true'))
        admin.dispose()
    with db.session() as s:  # after recovery: only the intent created while the DB was up exists
        ids = s.scalars(select(m.OrderIntent.client_order_id)).all()
    assert ids == [order("TQQQ", 0).client_order_id]
    assert DatabaseCheck(db).run().passed


def test_unmigrated_schema_fails_the_preflight(empty_db: Database) -> None:
    res = DatabaseCheck(empty_db).run()
    assert not res.passed and "aq db upgrade" in res.detail


def test_non_postgres_urls_are_rejected() -> None:
    from adaptive_quant.core.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="PostgreSQL"):
        Database("sqlite:///x.db")
    with pytest.raises(ConfigurationError, match="not a valid"):
        Database("::not a url::")
    assert make_url("postgresql://a@b/c")
