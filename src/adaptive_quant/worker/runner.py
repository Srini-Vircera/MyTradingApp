"""The worker loop: claim one job at a time, execute it, publish status.

* Jobs are claimed with ``FOR UPDATE SKIP LOCKED`` and finished only by the
  claiming worker; a job whose worker died is marked failed on the next start
  (never re-run automatically - the operator may retry idempotent jobs).
* Any failure inside a job fails that job with a scrubbed error message; it
  never crashes the worker.
* The scheduler supervisor runs in its own thread so a long research job cannot
  delay a trading-cycle step.
"""

from __future__ import annotations

import contextlib
import os
import platform
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from adaptive_quant import __version__
from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.control import jobs as job_catalog
from adaptive_quant.control.state import master_gate
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import AQError
from adaptive_quant.observability.logging import get_logger
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.services.runtime import effective_config
from adaptive_quant.worker.context import ORPHAN_AFTER, JobCancelled, JobContext, safe_error
from adaptive_quant.worker.handlers import HANDLERS, WorkerEnv, refresh_inventory
from adaptive_quant.worker.scheduler import SchedulerSupervisor

_log = get_logger(__name__)
HEARTBEAT_SECONDS = 10.0
SUPERVISE_SECONDS = 15.0


def worker_id() -> str:
    return f"{platform.node()[:40]}-{os.getpid()}"


class Worker:
    def __init__(
        self,
        base: LoadedConfig,
        secrets: Secrets,
        clock: Clock,
        db: Database,
        calendar: TradingCalendar,
        *,
        environ: Mapping[str, str],
        supervisor: SchedulerSupervisor | None = None,
        wid: str | None = None,
    ) -> None:
        self.env = WorkerEnv(base, secrets, clock, db, calendar)
        self.environ = environ
        self.supervisor = supervisor
        self.id = wid or worker_id()
        self.started_at = clock.now()
        self.current: dict[str, Any] | None = None
        self._last_beat: datetime | None = None

    @property
    def repo(self) -> ControlRepository:
        return ControlRepository(self.env.db)

    # ------------------------------------------------------------ lifecycle
    def startup(self) -> None:
        orphans = self.repo.fail_orphans(self.env.clock.now(), ORPHAN_AFTER)
        if orphans:
            _log.warning("orphaned_jobs_failed", count=orphans)
        try:
            effective, _row = effective_config(self.env.base, self.env.db)
            refresh_inventory(self.env, effective)
        except AQError as exc:
            _log.error("inventory_refresh_failed", error=safe_error(exc, self.env.secrets))
        self.beat(force=True)

    def beat(self, *, force: bool = False) -> None:
        now = self.env.clock.now()
        if (
            not force
            and self._last_beat is not None
            and (now - self._last_beat).total_seconds() < HEARTBEAT_SECONDS
        ):
            return
        self._last_beat = now
        try:
            effective, row = effective_config(self.env.base, self.env.db)
            config_version, revision = effective.config_version, row["revision"]
        except AQError as exc:
            config_version, revision = f"invalid: {exc.message}"[:64], None
        status = {
            "role": "worker",
            "version": __version__,
            "config_version": config_version,
            "runtime_revision": revision,
            "master_gate": master_gate(self.environ),
            "scheduler": None if self.supervisor is None else self.supervisor.status.to_json(),
            "current_job": self.current,
            "job_types": sorted(job_catalog.JOB_TYPES),
            "credentials_configured": {
                k: v for k, v in self.env.secrets.present().items() if k != "AQ_API_TOKEN"
            },
        }
        self.repo.beat(self.id, self.started_at, now, status)

    # ------------------------------------------------------------ jobs
    def run_once(self) -> bool:
        """Claim and execute one job; False when the queue is empty."""
        job = self.repo.claim_next(self.id, self.env.clock.now(), list(job_catalog.JOB_TYPES))
        if job is None:
            return False
        self.execute(job)
        return True

    def execute(self, job: dict[str, Any]) -> None:
        repo, clock = self.repo, self.env.clock
        self.current = {"id": job["id"], "type": job["job_type"]}
        self.beat(force=True)
        status, result, error, version = "failed", None, None, None
        with JobContext(repo, job["id"], self.id, clock, self.env.secrets) as ctx:
            try:
                params = job_catalog.parse(job["job_type"], job["params"])
                effective, _row = effective_config(self.env.base, self.env.db)
                version = effective.config_version
                ctx.progress(f"{job_catalog.TITLES[job['job_type']]} (config {version})", 0.0)
                result = HANDLERS[job["job_type"]](ctx, self.env, effective, params)
                status = "succeeded"
            except JobCancelled:
                status, error = "cancelled", "cancelled by the operator"
            except Exception as exc:  # noqa: BLE001 - a job failure never crashes the worker
                error = safe_error(exc, self.env.secrets)
                _log.error("job_failed", job=job["id"], type=job["job_type"], error=error)
                with contextlib.suppress(AQError):  # database down: finish() reports it
                    ctx.log("error", error)
        repo.finish(
            job["id"], self.id, status, clock.now(), result=result, error=error,
            config_version=version,
        )  # fmt: skip
        self.current = None
        self.beat(force=True)

    # ------------------------------------------------------------ loop
    def run(
        self, should_stop: Callable[[], bool], *, poll_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:  # fmt: skip
        self.startup()
        stop_event = threading.Event()
        thread = None
        if self.supervisor is not None:
            sup = self.supervisor

            def supervise() -> None:
                while not stop_event.is_set():
                    try:
                        sup.step()
                    except Exception as exc:  # noqa: BLE001  # pragma: no cover - keep going
                        _log.error("scheduler_supervisor_error", error=str(exc))
                    stop_event.wait(SUPERVISE_SECONDS)
                sup.stop("worker shutting down")

            thread = threading.Thread(target=supervise, name="scheduler-supervisor", daemon=True)
            thread.start()
        try:
            while not should_stop():
                self.beat()
                try:
                    worked = self.run_once()
                except AQError as exc:  # database unavailable: back off, keep running
                    _log.error("job_claim_failed", error=safe_error(exc, self.env.secrets))
                    worked = False
                if not worked:
                    sleep(poll_seconds)
        finally:
            stop_event.set()
            if thread is not None:
                thread.join(timeout=60)
