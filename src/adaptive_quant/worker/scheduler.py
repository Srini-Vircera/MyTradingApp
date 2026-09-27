"""Scheduler supervision inside the worker.

The trading scheduler runs only while **both** hold:

* the deployment master gate ``AQ_SCHEDULER_ENABLED`` is true (environment of
  the worker service; the UI can never change it), and
* the audited operator switch ``control_state['scheduler'].desired == "running"``.

Starting builds the cycle dependencies exactly like ``aq trade run`` (paper or
shadow only, human-promoted strategies only, database + paper broker checks)
and takes the single-scheduler advisory lock; every cycle still runs the
kill-switch, data, broker, reconciliation and risk pre-flight checks. Stopping
is always allowed and fails safe: the next due step simply does not run, and a
step already in progress finishes (it is never interrupted half-way).

Configuration changes made while the scheduler runs are **not** hot-swapped:
the supervisor reports that a stop/start is needed to apply them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.control.state import SCHEDULER_KEY, desired_running, master_gate
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import AQError, SafetyViolation
from adaptive_quant.observability.logging import get_logger
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.locks import AdvisoryLock
from adaptive_quant.services.runtime import effective_config
from adaptive_quant.trading.scheduler.cycle import CycleDeps
from adaptive_quant.trading.scheduler.runner import Scheduler
from adaptive_quant.worker.context import scrub

_log = get_logger(__name__)
RETRY_AFTER = timedelta(seconds=60)

BuildDeps = Callable[[LoadedConfig, Secrets, Clock], CycleDeps]


@dataclass
class SchedulerStatus:
    master_gate: bool = False
    desired: str = "stopped"
    state: str = "stopped"  # stopped / running / blocked
    detail: str = "scheduler stopped"
    since: str | None = None
    mode: str | None = None
    config_version: str | None = None
    restart_needed: bool = False
    last_tick: dict[str, str] = field(default_factory=dict)
    next_wake: str | None = None

    def to_json(self) -> dict[str, Any]:
        return dict(vars(self))


class SchedulerSupervisor:
    def __init__(
        self,
        base: LoadedConfig,
        secrets: Secrets,
        clock: Clock,
        db: Database,
        environ: Mapping[str, str],
        build: BuildDeps,
    ) -> None:
        self.base = base
        self.secrets = secrets
        self.clock = clock
        self.db = db
        self.environ = environ
        self.build = build
        self.status = SchedulerStatus()
        self._scheduler: Scheduler | None = None
        self._lock: AdvisoryLock | None = None
        self._retry_at: datetime | None = None
        self._next_wake: datetime | None = None

    @property
    def running(self) -> bool:
        return self._scheduler is not None

    def step(self) -> None:
        """One supervision round (called periodically by the worker)."""
        now = self.clock.now()
        gate = master_gate(self.environ)
        repo = ControlRepository(self.db)
        desired = desired_running(repo.get_state(SCHEDULER_KEY))
        self.status.master_gate = gate
        self.status.desired = "running" if desired else "stopped"
        if not gate or not desired:
            reason = (
                "master gate AQ_SCHEDULER_ENABLED is off (deployment setting)"
                if not gate
                else "stopped by the operator"
            )
            if self.running:
                self.stop(reason)
            self._set("stopped" if gate or not desired else "blocked", reason, now)
            self._retry_at = None
            return
        if not self.running:
            if self._retry_at is not None and now < self._retry_at:
                return
            self._start(now)
            if not self.running:
                return
        self._tick(now)

    def _start(self, now: datetime) -> None:
        try:
            effective, _row = effective_config(self.base, self.db)
            if effective.settings.trading.mode.uses_real_money:  # defence in depth
                raise SafetyViolation("the scheduler never runs in live mode here")
            deps = self.build(effective, self.secrets, self.clock)
            lock = AdvisoryLock(deps.db, f"aq-scheduler-{effective.settings.app.environment.value}")
            if not lock.acquire(wait_seconds=0):
                deps.db.dispose()
                raise SafetyViolation(
                    "another scheduler instance holds the single-instance lock",
                    hint="only one worker may run the trading cycle",
                )
        except AQError as exc:
            msg = scrub(exc.message + (f" - {exc.hint}" if exc.hint else ""), self.secrets)
            _log.warning("scheduler_start_refused", reason=msg)
            self._set("blocked", f"start refused: {msg}", now)
            self._retry_at = now + RETRY_AFTER
            return
        self._scheduler = Scheduler(deps)
        self._lock = lock
        self._next_wake = None
        self.status.mode = effective.settings.trading.mode.value
        self.status.config_version = effective.config_version
        self.status.restart_needed = False
        self._set("running", f"running in {self.status.mode} mode", now)
        _log.info("scheduler_started", mode=self.status.mode, config=effective.config_version)

    def _tick(self, now: datetime) -> None:
        sched = self._scheduler
        if sched is None:  # pragma: no cover - guarded by caller
            return
        try:
            effective, _row = effective_config(self.base, self.db)
            self.status.restart_needed = effective.config_version != self.status.config_version
        except AQError:
            self.status.restart_needed = True
        if self._next_wake is not None and now < self._next_wake:
            return
        try:
            self.status.last_tick = sched.tick()
            self._next_wake = sched.next_wake()
            self.status.next_wake = self._next_wake.isoformat()
        except AQError as exc:
            msg = scrub(exc.message, self.secrets)
            _log.error("scheduler_tick_failed", error=msg)
            self.status.detail = f"last step failed: {msg}"
            self._next_wake = now + RETRY_AFTER

    def stop(self, reason: str) -> None:
        if self._lock is not None:  # release before the engine holding it is disposed
            self._lock.release()
            self._lock = None
        if self._scheduler is not None:
            _log.warning("scheduler_stopped", reason=reason)
            try:
                self._scheduler.deps.db.dispose()
            finally:
                self._scheduler = None
        self._next_wake = None

    def _set(self, state: str, detail: str, now: datetime) -> None:
        if state != self.status.state:
            self.status.since = now.isoformat()
        self.status.state = state
        self.status.detail = detail
