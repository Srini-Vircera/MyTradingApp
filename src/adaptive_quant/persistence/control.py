"""Control-plane persistence: jobs, uploads, inventory, results, runtime config, audit.

Depends only on ``core`` and the database layer (like every repository).

Job safety:

* :meth:`ControlRepository.claim_next` takes the oldest queued job with
  ``SELECT ... FOR UPDATE SKIP LOCKED`` inside one transaction, so two workers
  can never claim the same job;
* :meth:`finish` / :meth:`heartbeat` only act on a job that is still ``running``
  *and* owned by the calling worker; a database trigger additionally refuses any
  change of a terminal state, so a finished job can never run again;
* identical queued/running requests are deduplicated (double-click safe).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, desc, select, update

from adaptive_quant.core.clock import ensure_utc
from adaptive_quant.core.errors import PersistenceError
from adaptive_quant.persistence import models as m
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.repositories import jsonable

Row = dict[str, Any]
TERMINAL = ("succeeded", "failed", "cancelled")
ACTIVE = ("queued", "running")


def new_id() -> str:
    return uuid.uuid4().hex


def _canonical(params: Any) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)


def _job_row(j: m.ControlJob) -> Row:
    out: Row = jsonable(
        {
            "id": j.id,
            "job_type": j.job_type,
            "params": j.params_json,
            "status": j.status,
            "requested_by": j.requested_by,
            "requested_at": j.requested_at,
            "started_at": j.started_at,
            "finished_at": j.finished_at,
            "heartbeat_at": j.heartbeat_at,
            "worker_id": j.worker_id,
            "progress": j.progress,
            "message": j.message,
            "result": j.result_json,
            "error": j.error,
            "config_version": j.config_version,
            "retry_of": j.retry_of,
            "cancel_requested": j.cancel_requested,
        }
    )
    return out


class ControlRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ================================================================ jobs
    def create_job(
        self,
        job_type: str,
        params: Row,
        requested_by: str,
        now: datetime,
        *,
        retry_of: str | None = None,
        dedupe: bool = True,
    ) -> tuple[Row, bool]:
        """Queue a job; returns (job, created). An identical active job is returned instead."""
        with self.db.session() as s:
            if dedupe:
                key = _canonical(params)
                for j in s.scalars(
                    select(m.ControlJob).where(
                        m.ControlJob.job_type == job_type, m.ControlJob.status.in_(ACTIVE)
                    )
                ):
                    if _canonical(j.params_json) == key:
                        return _job_row(j), False
            job = m.ControlJob(
                id=new_id(),
                job_type=job_type,
                params_json=jsonable(params),
                status="queued",
                requested_by=requested_by,
                requested_at=ensure_utc(now),
                message="waiting for the worker",
                retry_of=retry_of,
                cancel_requested=False,
            )
            s.add(job)
            s.flush()
            return _job_row(job), True

    def get_job(self, job_id: str) -> Row | None:
        with self.db.session() as s:
            j = s.get(m.ControlJob, job_id)
            return None if j is None else _job_row(j)

    def list_jobs(
        self, limit: int, *, status: str | None = None, job_type: str | None = None
    ) -> list[Row]:
        with self.db.session() as s:
            q = select(m.ControlJob).order_by(desc(m.ControlJob.requested_at))
            if status:
                q = q.where(m.ControlJob.status == status)
            if job_type:
                q = q.where(m.ControlJob.job_type.startswith(job_type))
            return [_job_row(j) for j in s.scalars(q.limit(limit))]

    def job_logs(self, job_id: str, limit: int = 500) -> list[Row]:
        with self.db.session() as s:
            q = (
                select(m.ControlJobLog)
                .where(m.ControlJobLog.job_id == job_id)
                .order_by(m.ControlJobLog.id)
                .limit(limit)
            )
            return [
                jsonable({"at": x.at, "level": x.level, "message": x.message}) for x in s.scalars(q)
            ]

    def claim_next(self, worker_id: str, now: datetime, types: Sequence[str]) -> Row | None:
        """Atomically claim the oldest queued job of the given types (or None)."""
        with self.db.session() as s:
            job = s.scalars(
                select(m.ControlJob)
                .where(
                    m.ControlJob.status == "queued",
                    m.ControlJob.job_type.in_(list(types)),
                    m.ControlJob.cancel_requested.is_(False),
                )
                .order_by(m.ControlJob.requested_at, m.ControlJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).first()
            if job is None:
                return None
            now = ensure_utc(now)
            job.status = "running"
            job.worker_id = worker_id
            job.started_at = now
            job.heartbeat_at = now
            job.progress = 0.0
            job.message = "started"
            s.flush()
            return _job_row(job)

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        now: datetime,
        *,
        progress: float | None = None,
        message: str | None = None,
    ) -> bool:
        """Refresh a running job; returns True if cancellation was requested."""
        values: Row = {"heartbeat_at": ensure_utc(now)}
        if progress is not None:
            values["progress"] = max(0.0, min(1.0, float(progress)))
        if message is not None:
            values["message"] = message[:2000]
        with self.db.session() as s:
            s.execute(
                update(m.ControlJob)
                .where(
                    m.ControlJob.id == job_id,
                    m.ControlJob.status == "running",
                    m.ControlJob.worker_id == worker_id,
                )
                .values(**values)
            )
            flag = s.scalar(select(m.ControlJob.cancel_requested).where(m.ControlJob.id == job_id))
            return bool(flag)

    def log(self, job_id: str, level: str, message: str, now: datetime) -> None:
        with self.db.session() as s:
            s.add(
                m.ControlJobLog(
                    job_id=job_id, at=ensure_utc(now), level=level[:12], message=message[:4000]
                )
            )

    def finish(
        self,
        job_id: str,
        worker_id: str,
        status: str,
        now: datetime,
        *,
        result: Row | None = None,
        error: str | None = None,
        config_version: str | None = None,
    ) -> bool:
        if status not in TERMINAL:
            raise PersistenceError(f"{status!r} is not a terminal job state")
        with self.db.session() as s:
            res = s.execute(
                update(m.ControlJob)
                .where(
                    m.ControlJob.id == job_id,
                    m.ControlJob.status == "running",
                    m.ControlJob.worker_id == worker_id,
                )
                .values(
                    status=status,
                    finished_at=ensure_utc(now),
                    heartbeat_at=ensure_utc(now),
                    progress=1.0 if status == "succeeded" else None,
                    message={"succeeded": "finished", "failed": "failed"}.get(status, status),
                    result_json=None if result is None else jsonable(result),
                    error=None if error is None else error[:4000],
                    config_version=config_version,
                )
            )
            return bool(getattr(res, "rowcount", 0))

    def request_cancel(self, job_id: str, now: datetime) -> Row:
        """Queued -> cancelled now; running -> cooperative cancel at the next checkpoint."""
        with self.db.session() as s:
            j = s.get(m.ControlJob, job_id, with_for_update=True)
            if j is None:
                raise PersistenceError(f"unknown job {job_id}")
            if j.status == "queued":
                j.status = "cancelled"
                j.finished_at = ensure_utc(now)
                j.message = "cancelled before it started"
            elif j.status == "running":
                j.cancel_requested = True
                j.message = "cancellation requested; stops at the next safe checkpoint"
            else:
                raise PersistenceError(f"job {job_id} already {j.status}")
            s.flush()
            return _job_row(j)

    def fail_orphans(self, now: datetime, stale_after: timedelta) -> int:
        """Running jobs whose worker stopped heart-beating are failed (never re-run)."""
        cutoff = ensure_utc(now) - stale_after
        with self.db.session() as s:
            res = s.execute(
                update(m.ControlJob)
                .where(m.ControlJob.status == "running", m.ControlJob.heartbeat_at < cutoff)
                .values(
                    status="failed",
                    finished_at=ensure_utc(now),
                    message="failed",
                    error="the worker stopped while this job was running; "
                    "nothing was retried automatically",
                )
            )
            return int(getattr(res, "rowcount", 0) or 0)

    # ================================================================ uploads
    def create_upload(
        self,
        *,
        symbol: str,
        kind: str,
        frequency: str,
        filename_hint: str,
        content: bytes,
        sha256: str,
        status: str,
        preview: Row,
        requested_by: str,
        now: datetime,
    ) -> Row:
        with self.db.session() as s:
            up = m.DataUpload(
                id=new_id(),
                symbol=symbol,
                kind=kind,
                frequency=frequency,
                filename_hint=filename_hint[:128],
                size_bytes=len(content),
                sha256=sha256,
                content=content,
                status=status,
                preview_json=jsonable(preview),
                requested_by=requested_by,
                updated_at=ensure_utc(now),
            )
            s.add(up)
            s.flush()
            return self._upload_row(up)

    @staticmethod
    def _upload_row(up: m.DataUpload) -> Row:
        out: Row = jsonable(
            {
                "id": up.id,
                "symbol": up.symbol,
                "kind": up.kind,
                "frequency": up.frequency,
                "filename_hint": up.filename_hint,
                "size_bytes": up.size_bytes,
                "sha256": up.sha256,
                "status": up.status,
                "preview": up.preview_json,
                "requested_by": up.requested_by,
                "job_id": up.job_id,
                "created_at": up.created_at,
                "updated_at": up.updated_at,
            }
        )
        return out

    def get_upload(self, upload_id: str) -> Row | None:
        with self.db.session() as s:
            up = s.get(m.DataUpload, upload_id)
            return None if up is None else self._upload_row(up)

    def upload_content(self, upload_id: str) -> bytes:
        with self.db.session() as s:
            up = s.get(m.DataUpload, upload_id)
            if up is None:
                raise PersistenceError(f"unknown upload {upload_id}")
            return bytes(up.content)

    def list_uploads(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.DataUpload).order_by(desc(m.DataUpload.created_at)).limit(limit)
            return [self._upload_row(u) for u in s.scalars(q)]

    def set_upload_status(
        self,
        upload_id: str,
        status: str,
        now: datetime,
        *,
        job_id: str | None = None,
        expect: Sequence[str] | None = None,
    ) -> Row:
        with self.db.session() as s:
            up = s.get(m.DataUpload, upload_id, with_for_update=True)
            if up is None:
                raise PersistenceError(f"unknown upload {upload_id}")
            if expect is not None and up.status not in expect:
                raise PersistenceError(f"upload {upload_id} is {up.status}")
            up.status = status
            up.updated_at = ensure_utc(now)
            if job_id is not None:
                up.job_id = job_id
            if status == "discarded":
                up.content = b""  # drop the payload; the metadata stays for the audit trail
            s.flush()
            return self._upload_row(up)

    # ================================================================ inventory
    def replace_inventory(self, rows: Sequence[Row], now: datetime) -> None:
        with self.db.session() as s:
            s.execute(delete(m.DatasetInventoryRow))
            for r in rows:
                s.add(
                    m.DatasetInventoryRow(
                        source=r["source"],
                        symbol=r["symbol"],
                        frequency=r["frequency"],
                        adjustment=r["adjustment"],
                        payload_json=jsonable(r),
                        updated_at=ensure_utc(now),
                    )
                )

    def inventory(self) -> list[Row]:
        with self.db.session() as s:
            q = select(m.DatasetInventoryRow).order_by(
                m.DatasetInventoryRow.symbol,
                m.DatasetInventoryRow.source,
                m.DatasetInventoryRow.frequency,
                m.DatasetInventoryRow.adjustment,
            )
            return [{**x.payload_json, "updated_at": jsonable(x.updated_at)} for x in s.scalars(q)]

    # ================================================================ backtests
    def save_backtest(
        self,
        job_id: str,
        *,
        strategies: list[str],
        source: str,
        start: date,
        end: date,
        initial_capital: float,
        summary: Row,
        report_path: str,
        config_version: str,
    ) -> None:
        with self.db.session() as s:
            s.add(
                m.BacktestRunRow(
                    job_id=job_id,
                    strategies=list(strategies),
                    source=source,
                    start_date=start,
                    end_date=end,
                    initial_capital=float(initial_capital),
                    summary_json=jsonable(summary),
                    report_path=report_path,
                    config_version=config_version,
                )
            )

    @staticmethod
    def _bt_row(b: m.BacktestRunRow, full: bool) -> Row:
        summary = dict(b.summary_json)
        if not full:
            summary.pop("curve", None)
            summary.pop("metrics", None)
        out: Row = jsonable(
            {
                "job_id": b.job_id,
                "strategies": b.strategies,
                "source": b.source,
                "start": b.start_date,
                "end": b.end_date,
                "initial_capital": b.initial_capital,
                "config_version": b.config_version,
                "created_at": b.created_at,
                "summary": summary,
            }
        )
        return out

    def list_backtests(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.BacktestRunRow).order_by(desc(m.BacktestRunRow.created_at)).limit(limit)
            return [self._bt_row(b, full=False) for b in s.scalars(q)]

    def get_backtest(self, job_id: str) -> Row | None:
        with self.db.session() as s:
            b = s.get(m.BacktestRunRow, job_id)
            return None if b is None else self._bt_row(b, full=True)

    # ================================================================ runtime config
    def runtime_config(self) -> Row:
        with self.db.session() as s:
            r = s.get(m.RuntimeConfigRow, 1)
            if r is None:
                return {
                    "revision": 0,
                    "overlay": {},
                    "strategy_overrides": {},
                    "updated_by": None,
                    "updated_at": None,
                }
            out: Row = jsonable(
                {
                    "revision": r.revision,
                    "overlay": r.overlay_json,
                    "strategy_overrides": r.strategy_overrides_json,
                    "updated_by": r.updated_by,
                    "updated_at": r.updated_at,
                }
            )
            return out

    def save_runtime_config(
        self,
        *,
        expected_revision: int,
        overlay: Row,
        strategy_overrides: Row,
        actor: str,
        now: datetime,
        changes: Sequence[Row],
        version_before: str,
        version_after: str,
    ) -> int:
        """Optimistic concurrency: fails if someone else changed the config meanwhile."""
        now = ensure_utc(now)
        with self.db.session() as s:
            r = s.get(m.RuntimeConfigRow, 1, with_for_update=True)
            current = 0 if r is None else r.revision
            if current != expected_revision:
                raise PersistenceError(
                    f"runtime configuration changed (revision {current}, expected "
                    f"{expected_revision}); reload and try again"
                )
            revision = current + 1
            if r is None:
                r = m.RuntimeConfigRow(id=1)
                s.add(r)
            r.revision = revision
            r.overlay_json = jsonable(overlay)
            r.strategy_overrides_json = jsonable(strategy_overrides)
            r.updated_by = actor
            r.updated_at = now
            for c in changes:
                s.add(
                    m.RuntimeConfigChange(
                        revision=revision,
                        kind=c["kind"],
                        path=c["path"][:160],
                        old_json={"value": jsonable(c.get("old"))},
                        new_json={"value": jsonable(c.get("new"))},
                        actor=actor,
                        reason=c.get("reason", ""),
                        config_version_before=version_before,
                        config_version_after=version_after,
                        at=now,
                    )
                )
            return revision

    def runtime_changes(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.RuntimeConfigChange).order_by(desc(m.RuntimeConfigChange.id)).limit(limit)
            return [
                jsonable(
                    {
                        "revision": c.revision,
                        "kind": c.kind,
                        "path": c.path,
                        "old": c.old_json.get("value"),
                        "new": c.new_json.get("value"),
                        "actor": c.actor,
                        "reason": c.reason,
                        "config_version_before": c.config_version_before,
                        "config_version_after": c.config_version_after,
                        "at": c.at,
                    }
                )
                for c in s.scalars(q)
            ]

    # ================================================================ control state
    def get_state(self, key: str) -> Row | None:
        with self.db.session() as s:
            r = s.get(m.ControlStateRow, key)
            if r is None:
                return None
            out: Row = jsonable(
                {"value": r.value_json, "updated_by": r.updated_by, "updated_at": r.updated_at}
            )
            return out

    def set_state(self, key: str, value: Row, actor: str, now: datetime) -> None:
        with self.db.session() as s:
            r = s.get(m.ControlStateRow, key, with_for_update=True)
            if r is None:
                r = m.ControlStateRow(key=key)
                s.add(r)
            r.value_json = jsonable(value)
            r.updated_by = actor
            r.updated_at = ensure_utc(now)

    # ================================================================ audit
    def record_event(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        outcome: str,
        detail: Row,
        client: str,
        now: datetime,
    ) -> None:
        with self.db.session() as s:
            s.add(
                m.ControlEvent(
                    at=ensure_utc(now),
                    actor=actor[:128],
                    action=action[:64],
                    target=target[:160],
                    outcome=outcome[:16],
                    detail_json=jsonable(detail),
                    client=client[:64],
                )
            )

    def events(self, limit: int, action_prefix: str | None = None) -> list[Row]:
        with self.db.session() as s:
            q = select(m.ControlEvent).order_by(desc(m.ControlEvent.id))
            if action_prefix:
                q = q.where(m.ControlEvent.action.startswith(action_prefix))
            return [
                jsonable(
                    {
                        "at": e.at,
                        "actor": e.actor,
                        "action": e.action,
                        "target": e.target,
                        "outcome": e.outcome,
                        "detail": e.detail_json,
                    }
                )
                for e in s.scalars(q.limit(limit))
            ]

    # ================================================================ heartbeats
    def beat(self, worker_id: str, started_at: datetime, now: datetime, status: Row) -> None:
        with self.db.session() as s:
            r = s.get(m.WorkerHeartbeat, worker_id)
            if r is None:
                r = m.WorkerHeartbeat(worker_id=worker_id, started_at=ensure_utc(started_at))
                s.add(r)
            r.last_seen_at = ensure_utc(now)
            r.status_json = jsonable(status)

    def heartbeats(self) -> list[Row]:
        with self.db.session() as s:
            q = select(m.WorkerHeartbeat).order_by(desc(m.WorkerHeartbeat.last_seen_at))
            return [
                jsonable(
                    {
                        "worker_id": h.worker_id,
                        "started_at": h.started_at,
                        "last_seen_at": h.last_seen_at,
                        "status": h.status_json,
                    }
                )
                for h in s.scalars(q)
            ]
