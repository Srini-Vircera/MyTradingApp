"""Only one scheduler may run at a time (overlapping redeploys, duplicate replicas)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from adaptive_quant.core.errors import DatabaseUnavailableError
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.locks import AdvisoryLock, lock_key

pytestmark = pytest.mark.postgres


def test_second_instance_is_refused_until_the_first_stops(db: Database) -> None:
    first, second = AdvisoryLock(db, "aq-scheduler-test"), AdvisoryLock(db, "aq-scheduler-test")
    assert first.acquire()
    waits: list[float] = []
    assert not second.acquire(wait_seconds=4, poll_seconds=2, sleep=waits.append)
    assert waits == [2, 2]  # it waited for the old instance, then gave up
    first.release()
    assert second.acquire()
    other = AdvisoryLock(db, "another-name")
    assert other.acquire()  # names are independent
    other.release()
    second.release()


def test_lock_is_released_when_the_process_connection_dies(db: Database) -> None:
    holder = AdvisoryLock(db, "aq-scheduler-crash")
    assert holder.acquire()
    assert holder._conn is not None
    pid = holder._conn.execute(text("SELECT pg_backend_pid()")).scalar()
    with db.session() as s:  # the process dies: PostgreSQL ends its session
        s.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
    successor = AdvisoryLock(db, "aq-scheduler-crash")
    assert successor.acquire()
    successor.release()
    holder.release()  # tolerates the dead connection


def test_keys_are_stable_signed_64_bit() -> None:
    k = lock_key("aq-scheduler-production")
    assert k == lock_key("aq-scheduler-production")
    assert -(2**63) <= k < 2**63


def test_unreachable_database_raises() -> None:
    down = Database("postgresql://aq:x@127.0.0.1:1/none", pool_size=1)
    try:
        with pytest.raises(DatabaseUnavailableError):
            AdvisoryLock(down, "x").acquire()
    finally:
        down.dispose()
