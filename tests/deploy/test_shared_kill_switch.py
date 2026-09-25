"""The database kill-switch store shared by the API and worker containers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from adaptive_quant.api.app import ENGAGE_CONFIRMATION, create_app
from adaptive_quant.config.loader import load_config
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.errors import (
    ConfigurationError,
    DatabaseUnavailableError,
    SafetyViolation,
)
from adaptive_quant.persistence import models as m
from adaptive_quant.persistence.db import Database
from adaptive_quant.trading.safety.kill_switch import RELEASE_CONFIRMATION, KillSwitch
from adaptive_quant.trading.safety.kill_switch_store import (
    DatabaseKillSwitchStore,
    build_kill_switch,
)
from tests.api.conftest import AUTH, services
from tests.conftest import REPO_CONFIG

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)


def switch(db: Database) -> KillSwitch:
    return KillSwitch(DatabaseKillSwitchStore(db), FrozenClock(NOW))


def events(db: Database) -> int:
    with db.session() as s:
        return int(s.scalar(select(func.count()).select_from(m.KillSwitchEvent)) or 0)


def test_fresh_database_reads_as_engaged(db: Database) -> None:
    st = switch(db).status()
    assert st.engaged and st.fail_safe
    assert "never been released" in st.reason


def test_api_and_worker_see_the_same_state(db: Database) -> None:
    api, worker = switch(db), switch(db)  # two processes, one database
    api.release("operator:ann", "reviewed reconciliation", RELEASE_CONFIRMATION)
    assert not worker.is_engaged()
    worker.engage("system:scheduler", "stale data")
    assert api.is_engaged()
    assert api.status().actor == "system:scheduler"
    assert events(db) == 2  # every change is audited
    with pytest.raises(SafetyViolation):
        api.release("operator:ann", "retry", "re-enable trading")  # phrase is exact
    with pytest.raises(SafetyViolation):
        api.release("system:scheduler", "automation", RELEASE_CONFIRMATION)
    assert api.is_engaged()


def test_unreachable_database_fails_closed() -> None:
    down = Database("postgresql://aq:x@127.0.0.1:1/none", pool_size=1)
    try:
        st = switch(down).status()
        assert st.engaged and st.fail_safe
        assert "unreadable" in st.reason
        with pytest.raises(DatabaseUnavailableError):
            switch(down).release("operator:ann", "try", RELEASE_CONFIRMATION)
    finally:
        down.dispose()


def test_database_store_requires_a_database() -> None:
    loaded = load_config("production", config_dir=REPO_CONFIG)
    assert loaded.settings.trading.kill_switch.store == "database"
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        build_kill_switch(loaded, FrozenClock(NOW), None)


def test_api_engage_with_database_store_is_audited_once(tmp_path: Path, db: Database) -> None:
    prod = load_config("production", config_dir=REPO_CONFIG)
    sv = services(tmp_path, db)
    sv = type(sv)(
        loaded=prod,
        token=sv.token,
        kill_switch=build_kill_switch(prod, FrozenClock(NOW), db),
        clock=sv.clock,
        calendar=sv.calendar,
        db=db,
    )
    client = TestClient(create_app(sv))
    body = {"actor": "ann", "reason": "manual stop", "confirm": ENGAGE_CONFIRMATION}
    assert client.post("/api/v1/kill-switch/engage", json=body, headers=AUTH).status_code == 200
    assert switch(db).status().actor == "operator:ann"  # the worker sees it
    assert events(db) == 1


def test_readiness_needs_a_migrated_reachable_database(tmp_path: Path, db: Database) -> None:
    ready = TestClient(create_app(services(tmp_path, db))).get("/api/v1/health/ready")
    assert (ready.status_code, ready.json()) == (200, {"status": "ready"})
    down = Database("postgresql://aq:x@127.0.0.1:1/none", pool_size=1)
    try:
        r = TestClient(create_app(services(tmp_path, down))).get("/api/v1/health/ready")
        assert (r.status_code, r.json()) == (503, {"status": "not ready"})
    finally:
        down.dispose()


def test_readiness_is_false_before_migrations(tmp_path: Path, empty_db: Database) -> None:
    r = TestClient(create_app(services(tmp_path, empty_db))).get("/api/v1/health/ready")
    assert r.status_code == 503
