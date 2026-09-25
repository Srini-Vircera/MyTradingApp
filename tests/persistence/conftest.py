"""Real-PostgreSQL fixtures.

``AQ_TEST_DATABASE_URL`` (a server URL whose user may CREATE DATABASE) is used
when set - CI provides one. Otherwise a temporary cluster is started from the
local PostgreSQL binaries (``initdb``/``pg_ctl``). Without either, these tests
are skipped with a clear reason. Each test gets a fresh, fully migrated database.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from adaptive_quant.persistence import migrate
from adaptive_quant.persistence.db import Database

pytestmark = pytest.mark.postgres


def _bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    cands = sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}"))
    return cands[-1] if cands else None


def _as_postgres(cmd: list[str]) -> list[str]:
    if os.geteuid() == 0:  # initdb refuses to run as root
        return ["su", "postgres", "-c", " ".join(cmd)]
    return cmd


@pytest.fixture(scope="session")
def pg_server_url() -> Iterator[str]:
    env = os.environ.get("AQ_TEST_DATABASE_URL")
    if env:
        yield env
        return
    initdb, pg_ctl = _bin("initdb"), _bin("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("no PostgreSQL available: set AQ_TEST_DATABASE_URL or install PostgreSQL")
    root = Path(tempfile.mkdtemp(prefix="aq-pg-"))
    if os.geteuid() == 0:
        shutil.chown(root, "postgres")
    data = root / "data"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run(  # noqa: S603 - fixed local binaries, test-only
        _as_postgres([initdb, "-D", str(data), "-U", "aq", "--auth=trust", "-E", "UTF8"]),
        check=True,
        capture_output=True,
    )
    opts = f"'-p {port} -k {root} -c listen_addresses=127.0.0.1 -c fsync=off'"
    subprocess.run(  # noqa: S603
        _as_postgres([pg_ctl, "-D", str(data), "-o", opts, "-l", str(root / "log"), "-w", "start"]),
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql+psycopg://aq@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(  # noqa: S603
            _as_postgres([pg_ctl, "-D", str(data), "-m", "immediate", "stop"]), capture_output=True
        )
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def empty_db(pg_server_url: str) -> Iterator[Database]:
    """A fresh, empty (un-migrated) database."""
    name = f"aq_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(pg_server_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_server_url).set(database=name).render_as_string(hide_password=False)
    db = Database(url, pool_size=2)
    try:
        yield db
    finally:
        db.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def db(empty_db: Database) -> Database:
    """A fresh database migrated to head."""
    migrate.upgrade(empty_db)
    return empty_db
