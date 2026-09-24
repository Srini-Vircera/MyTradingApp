"""API fixtures (the Postgres fixtures are re-used for page tests)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from adaptive_quant.api.app import create_app
from adaptive_quant.api.services import ApiServices
from adaptive_quant.config.loader import load_config
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.persistence.db import Database
from adaptive_quant.trading.safety.kill_switch import FileKillSwitchStore, KillSwitch
from tests.conftest import REPO_CONFIG
from tests.data_helpers import calendar
from tests.persistence.conftest import db, empty_db, pg_server_url  # noqa: F401

TOKEN = "t0k3n-for-tests-" + "z" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2024, 7, 2, 18, 0, tzinfo=UTC)


def services(tmp: Path, database: Database | None = None) -> ApiServices:
    clock = FrozenClock(NOW)
    ks = KillSwitch(FileKillSwitchStore(tmp / "ks.json", tmp / "ks_audit.jsonl"), clock)
    return ApiServices(
        loaded=load_config("development", config_dir=REPO_CONFIG),
        token=SecretStr(TOKEN),
        kill_switch=ks,
        clock=clock,
        calendar=calendar(),
        db=database,
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(services(tmp_path)))
