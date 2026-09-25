"""Global kill switch.

Semantics
---------
* **Engaged** -> no new risk-increasing orders. Risk-reducing orders remain
  allowed if ``trading.kill_switch.allow_risk_reducing_orders`` is true.
  History and configuration are never touched.
* **Fail closed.** If the state cannot be read (missing, corrupt, I/O error)
  the switch is treated as engaged. A fresh installation therefore starts
  *engaged* and an operator must deliberately release it once.
* Engaging is easy and may be done by automated components
  (actor ``system:<component>``). Releasing requires a human actor, a reason
  and the exact confirmation phrase :data:`RELEASE_CONFIRMATION`.
* Every change is appended to an audit log (JSON lines).

The state lives behind :class:`KillSwitchStore`: a file store for a single
host, or the database store in :mod:`.kill_switch_store` shared by every
service of a multi-container deployment (database unreachable -> engaged).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import PersistenceError, SafetyViolation
from adaptive_quant.core.models import UtcDatetime
from adaptive_quant.observability.logging import get_logger

RELEASE_CONFIRMATION = "RE-ENABLE TRADING"
SYSTEM_ACTOR_PREFIX = "system:"

_log = get_logger(__name__)


class KillSwitchState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    engaged: bool
    reason: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    changed_at: UtcDatetime


class KillSwitchStatus(BaseModel):
    """What callers see. ``fail_safe`` is true when state could not be read."""

    model_config = ConfigDict(frozen=True)

    engaged: bool
    reason: str
    actor: str | None
    changed_at: datetime | None
    fail_safe: bool


class KillSwitchStore(Protocol):
    def read(self) -> KillSwitchState | None:
        """Return the stored state, ``None`` if never initialised; raise if unreadable."""
        ...

    def write(self, state: KillSwitchState) -> None: ...

    def append_audit(self, record: dict[str, Any]) -> None: ...


class FileKillSwitchStore:
    """Kill switch persisted as a small JSON file, written atomically."""

    def __init__(self, state_file: Path, audit_file: Path) -> None:
        self.state_file = state_file
        self.audit_file = audit_file

    def read(self) -> KillSwitchState | None:
        if not self.state_file.exists():
            return None
        return KillSwitchState.model_validate_json(self.state_file.read_text(encoding="utf-8"))

    def write(self, state: KillSwitchState) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_file.parent, prefix=".killswitch-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(state.model_dump_json(indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def append_audit(self, record: dict[str, Any]) -> None:
        self.audit_file.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


class KillSwitch:
    def __init__(self, store: KillSwitchStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def status(self) -> KillSwitchStatus:
        try:
            state = self._store.read()
        except (OSError, ValidationError, ValueError, PersistenceError) as exc:
            _log.error("kill_switch_state_unreadable", error=str(exc))
            return KillSwitchStatus(
                engaged=True,
                reason=f"kill switch state unreadable ({type(exc).__name__}); failing closed",
                actor=None,
                changed_at=None,
                fail_safe=True,
            )
        if state is None:
            return KillSwitchStatus(
                engaged=True,
                reason="kill switch has never been released on this installation",
                actor=None,
                changed_at=None,
                fail_safe=True,
            )
        return KillSwitchStatus(
            engaged=state.engaged,
            reason=state.reason,
            actor=state.actor,
            changed_at=state.changed_at,
            fail_safe=False,
        )

    def is_engaged(self) -> bool:
        return self.status().engaged

    def engage(self, actor: str, reason: str) -> KillSwitchState:
        """Stop new risk-increasing orders. Always allowed; idempotent."""
        _require_text(actor=actor, reason=reason)
        state = KillSwitchState(
            engaged=True, reason=reason, actor=actor, changed_at=self._clock.now()
        )
        self._persist("engage", state)
        _log.critical("kill_switch_engaged", actor=actor, reason=reason)
        return state

    def release(self, actor: str, reason: str, confirmation: str) -> KillSwitchState:
        """Re-enable automated trading. Requires a human actor and the exact phrase."""
        _require_text(actor=actor, reason=reason)
        if actor.startswith(SYSTEM_ACTOR_PREFIX):
            raise SafetyViolation(
                "automated components may engage the kill switch but never release it",
                hint="a human operator must release it via the CLI or dashboard",
            )
        if confirmation != RELEASE_CONFIRMATION:
            raise SafetyViolation(
                "kill switch release not confirmed",
                hint=f"type the confirmation phrase exactly: {RELEASE_CONFIRMATION!r}",
            )
        state = KillSwitchState(
            engaged=False, reason=reason, actor=actor, changed_at=self._clock.now()
        )
        self._persist("release", state)
        _log.warning("kill_switch_released", actor=actor, reason=reason)
        return state

    def _persist(self, action: str, state: KillSwitchState) -> None:
        # Audit first: if the state write then fails we still know it was attempted.
        self._store.append_audit({"action": action, **state.model_dump(mode="json")})
        self._store.write(state)


def _require_text(**fields: str) -> None:
    for name, value in fields.items():
        if not value or not value.strip():
            raise SafetyViolation(f"kill switch {name} must not be empty")
