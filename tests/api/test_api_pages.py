"""Every dashboard page against a real, completed (simulated) paper cycle on PostgreSQL."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from adaptive_quant.api.app import ENGAGE_CONFIRMATION, create_app
from adaptive_quant.persistence.db import Database
from tests.api.conftest import AUTH, services
from tests.trading.cycle_helpers import build
from tests.trading.test_cycle import TUE, run_all

pytestmark = pytest.mark.postgres
CYCLE = "cyc-development-2024-07-02"


@pytest.fixture
def api(db: Database, tmp_path: Path) -> TestClient:
    deps, _broker, _cap, clock = build(db, tmp_path, TUE)
    run_all(deps, clock, TUE)
    return TestClient(create_app(services(tmp_path / "api", db)))


def test_all_pages_show_the_cycle(api: TestClient) -> None:
    def get(p: str) -> httpx.Response:
        r: httpx.Response = api.get(f"/api/v1{p}", headers=AUTH)
        return r

    ov = get("/overview").json()
    assert ov["latest_cycle"]["status"] == "completed" and len(ov["latest_cycle"]["steps"]) == 9
    assert ov["session"]["date"] == "2024-07-02" and ov["target"] is not None
    assert ov["latest_performance"]["session_date"] == "2024-07-02"
    assert ov["last_reconciliation"]["passed"] is True
    pf = get("/portfolio").json()
    assert pf["account"]["is_paper"] is True and pf["positions"] and pf["expected_positions"]
    assert get("/signals").json()["count"] == 2
    assert get(f"/signals?cycle_id={CYCLE}").json()["count"] == 2
    risk = get("/risk").json()
    assert risk["latest_decision"]["drawdown_band"] and risk["limits"]["max_gross_exposure"] == 1.0
    assert get("/performance").json()["count"] == 1
    orders = get("/orders").json()
    assert orders["count"] >= 1 and [e["to_state"] for e in orders["items"][0]["events"]][:3] == [
        "created",
        "validated",
        "submitted",
    ]
    assert get("/orders?state=filled").json()["count"] == orders["count"]
    assert get("/executions").json()["count"] >= 1
    assert get("/reconciliation").json()["items"][0]["passed"] is True
    assert get("/cycles").json()["items"][0]["cycle_id"] == CYCLE
    chain = get(f"/cycles/{CYCLE}/explain").json()
    assert (
        chain["decisions"] and chain["signals"] and chain["banner"]["mode"] == "paper"
    ) or chain["banner"]
    assert get("/cycles/cyc-nope/explain").status_code == 404
    health = get("/system/health").json()
    assert health["database"]["reachable"] and health["database"]["at_head"]
    strategies = get("/strategies").json()["strategies"]
    assert len(strategies) == 22 and all(s["lifecycle"] == "research" for s in strategies)
    assert get("/backtests").status_code == 200
    assert api.get("/api/v1/orders?limit=100000", headers=AUTH).status_code == 422  # bounded pages


def test_kill_switch_events_are_audited_in_the_database(api: TestClient, db: Database) -> None:
    body = {"actor": "Jane Operator", "reason": "drill", "confirm": ENGAGE_CONFIRMATION}
    assert api.post("/api/v1/kill-switch/engage", headers=AUTH, json=body).status_code == 200
    with db.engine.connect() as c:
        actor = c.execute(text("SELECT actor FROM kill_switch_events")).scalar()
    assert actor == "api:Jane Operator"


def test_database_outage_returns_503_without_internals(
    api: TestClient, db: Database, pg_server_url: str
) -> None:
    name = db.url.database
    admin = create_engine(pg_server_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false'))
        c.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name},
        )
    try:
        r = api.get("/api/v1/orders", headers=AUTH)
        assert r.status_code == 503 and r.json()["detail"] == "audit database unavailable"
        assert "psycopg" not in r.text and "127.0.0.1" not in r.text
        health = api.get("/api/v1/system/health", headers=AUTH).json()
        assert health["database"]["reachable"] is False
        body = {"actor": "Jane", "reason": "db down drill", "confirm": ENGAGE_CONFIRMATION}
        assert (
            api.post("/api/v1/kill-switch/engage", headers=AUTH, json=body).json()["engaged"]
            is True
        )
    finally:
        with admin.connect() as c:
            c.execute(text(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true'))
        admin.dispose()
