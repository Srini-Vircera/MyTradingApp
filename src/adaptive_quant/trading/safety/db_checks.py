"""Pre-trade check: the audit database must be reachable and fully migrated.

Order intents are persisted *before* transmission, so without the database no
order may be created at all (``database_unavailable`` blocks everything).
"""

from __future__ import annotations

from adaptive_quant.core.errors import AQError
from adaptive_quant.persistence import migrate
from adaptive_quant.persistence.db import Database
from adaptive_quant.trading.safety.preflight import CheckResult, RefusalReason


class DatabaseCheck:
    name = "audit_database"

    def __init__(self, db: Database) -> None:
        self._db = db

    def run(self) -> CheckResult:
        try:
            self._db.ping()
            current = migrate.current_revision(self._db)
        except (AQError, Exception) as exc:  # noqa: BLE001 - any failure blocks trading
            return CheckResult.fail(
                self.name, RefusalReason.DATABASE_UNAVAILABLE, f"{type(exc).__name__}: {exc}"
            )
        head = migrate.head_revision()
        if current != head:
            return CheckResult.fail(
                self.name,
                RefusalReason.DATABASE_UNAVAILABLE,
                f"schema at revision {current}, expected {head}: run `aq db upgrade`",
            )
        return CheckResult.ok(self.name, f"reachable, schema at {head}")
