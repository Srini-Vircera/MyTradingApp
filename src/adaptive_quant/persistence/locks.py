"""Single-instance guard: a PostgreSQL session-level advisory lock.

Held on a dedicated autocommit connection for the life of the process, so a
second scheduler (for example during an overlapping redeploy) cannot run at
the same time. If the process dies its connection closes and PostgreSQL
releases the lock automatically.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

from adaptive_quant.core.errors import DatabaseUnavailableError
from adaptive_quant.persistence.db import Database


def lock_key(name: str) -> int:
    """A stable signed 64-bit key for ``name``."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


class AdvisoryLock:
    def __init__(self, db: Database, name: str) -> None:
        self.name = name
        self.key = lock_key(name)
        self._db = db
        self._conn: Connection | None = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    def acquire(
        self,
        wait_seconds: float = 0.0,
        poll_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> bool:
        """Try to take the lock, retrying for up to ``wait_seconds``."""
        if self._conn is not None:
            return True
        try:
            conn = self._db.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        except SQLAlchemyError as exc:
            raise DatabaseUnavailableError(
                f"cannot take the {self.name!r} lock: database unavailable",
                hint="check PostgreSQL and DATABASE_URL",
            ) from exc
        waited = 0.0
        while True:
            if conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key}).scalar():
                self._conn = conn
                return True
            if waited >= wait_seconds:
                conn.close()
                return False
            sleep(poll_seconds)
            waited += poll_seconds

    def release(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
        except SQLAlchemyError:
            pass  # closing the connection releases it anyway
        finally:
            conn.close()
