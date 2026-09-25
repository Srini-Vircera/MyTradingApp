"""Kill-switch stores and the factory every entry point uses.

``trading.kill_switch.store``:

* ``file`` - JSON state file plus JSON-lines audit log (one host).
* ``database`` - the ``kill_switch_state`` row plus ``kill_switch_events``,
  shared by the API and the worker when they run in separate containers.
  If the database cannot be read the switch reads as engaged (fail closed),
  and engaging/releasing fails loudly rather than pretending to succeed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import ConfigurationError
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.repositories import MonitoringRepository
from adaptive_quant.trading.safety.kill_switch import (
    FileKillSwitchStore,
    KillSwitch,
    KillSwitchState,
    KillSwitchStore,
)


class DatabaseKillSwitchStore:
    def __init__(self, db: Database) -> None:
        self._repo = MonitoringRepository(db)

    def read(self) -> KillSwitchState | None:
        row = self._repo.read_kill_switch()
        return None if row is None else KillSwitchState.model_validate(row)

    def write(self, state: KillSwitchState) -> None:
        self._repo.write_kill_switch(state.engaged, state.actor, state.reason, state.changed_at)

    def append_audit(self, record: dict[str, Any]) -> None:
        at = record["changed_at"]
        self._repo.record_kill_switch_event(
            bool(record["engaged"]),
            str(record["actor"]),
            str(record["reason"]),
            at if isinstance(at, datetime) else datetime.fromisoformat(str(at)),
        )


def build_kill_switch(loaded: LoadedConfig, clock: Clock, db: Database | None) -> KillSwitch:
    """The kill switch as configured. ``db`` is required for the database store."""
    cfg = loaded.settings.trading.kill_switch
    store: KillSwitchStore
    if cfg.store == "database":
        if db is None:
            raise ConfigurationError(
                "trading.kill_switch.store is 'database' but DATABASE_URL is not set",
                hint="set DATABASE_URL; the shared kill switch lives in the audit database",
            )
        store = DatabaseKillSwitchStore(db)
    else:
        store = FileKillSwitchStore(
            loaded.resolve_path(cfg.state_file), loaded.resolve_path(cfg.audit_file)
        )
    return KillSwitch(store, clock)
