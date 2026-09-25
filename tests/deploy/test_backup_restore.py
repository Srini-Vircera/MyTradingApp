"""Backup/restore drill: scripts/db_backup.sh then scripts/db_restore.sh into an empty database."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from adaptive_quant.persistence import migrate
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.repositories import MonitoringRepository
from tests.conftest import REPO_ROOT

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed"),
]


def libpq(db: Database) -> str:
    return db.url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def target(pg_server_url: str) -> Iterator[Database]:
    name = f"aq_restore_{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_server_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_server_url).set(database=name).render_as_string(hide_password=False)
    db = Database(url, pool_size=1)
    try:
        yield db
    finally:
        db.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def script(name: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - repository scripts, test-only
        ["sh", str(REPO_ROOT / "scripts" / name), *args],  # noqa: S607
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_backup_then_restore_into_an_empty_database(
    db: Database, target: Database, tmp_path: Path
) -> None:
    at = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
    MonitoringRepository(db).write_kill_switch(True, "operator:ann", "before backup", at)
    MonitoringRepository(db).record_kill_switch_event(True, "operator:ann", "before backup", at)

    b = script("db_backup.sh", str(tmp_path), DATABASE_URL=libpq(db))
    assert b.returncode == 0, b.stderr
    assert libpq(db) not in b.stdout + b.stderr  # never prints the URL
    dumps = list(tmp_path.glob("aq-*.dump"))
    assert len(dumps) == 1 and (tmp_path / f"{dumps[0].name}.sha256").is_file()

    r = script("db_restore.sh", str(dumps[0]), TARGET_DATABASE_URL=libpq(target))
    assert r.returncode == 0, r.stderr
    assert migrate.current_revision(target) == migrate.head_revision()
    state = MonitoringRepository(target).read_kill_switch()
    assert state is not None and state["reason"] == "before backup"
    assert not migrate.schema_drift(target)

    again = script("db_restore.sh", str(dumps[0]), TARGET_DATABASE_URL=libpq(target))
    assert again.returncode == 2 and "not empty" in again.stderr  # never over an existing db
