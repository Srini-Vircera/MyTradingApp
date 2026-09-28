"""Control-plane endpoints (PR #4): jobs, data, backtests, research, strategies,
trading controls, settings and the audit trail.

Every mutation here:

* is listed in :data:`CONTROL_MUTATIONS` (the app's allowlist is asserted by tests);
* requires the operator bearer token (router dependency);
* uses a strict request model (``extra="forbid"``, bounded values, enums);
* is rate limited per bucket and audited in ``control_events`` - accepted *or*
  refused, with the named operator;
* returns readable errors and never secrets.

The API never runs a long task itself: it validates the request and queues a
job for the worker. It cannot reach a broker or the order manager, cannot set
live mode, and cannot change any deployment secret or the scheduler master gate.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from adaptive_quant.api.security import RateLimiter, client_of
from adaptive_quant.api.services import ApiServices
from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.runtime import (
    EDITABLE,
    SECRET_SETTINGS,
    TRADING_MODE_PATH,
    SettingClass,
    get_path,
    settings_catalog,
)
from adaptive_quant.control import jobs as job_catalog
from adaptive_quant.control import readiness
from adaptive_quant.control import state as ctl
from adaptive_quant.core.enums import StrategyLifecycle, TradingMode
from adaptive_quant.core.errors import AQError, DatabaseUnavailableError, PersistenceError
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.reads import AuditReads
from adaptive_quant.persistence.records import LifecycleEventRecord, StrategyVersionRecord
from adaptive_quant.persistence.repositories import ReferenceRepository
from adaptive_quant.quant.data.bars import Frequency
from adaptive_quant.quant.strategies.catalog import StrategyCatalog
from adaptive_quant.quant.strategies.registry import registry
from adaptive_quant.services import backtests as backtest_service
from adaptive_quant.services import data as data_service
from adaptive_quant.services import strategies as strategy_service
from adaptive_quant.services.runtime import effective_config, resolve

Row = dict[str, Any]
WORKER_STALE = timedelta(minutes=2)
PAPER_HOST = readiness.PAPER_HOST

#: every state-changing control-plane route: (method, path) -> rate-limit bucket
#: rate-limit bucket for mutations that make things safer (never throttled)
UNLIMITED = "unlimited"

CONTROL_MUTATIONS: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/jobs/data/download"): "job",
    ("POST", "/api/v1/jobs/data/validate"): "job",
    ("POST", "/api/v1/jobs/data/synthesize"): "job",
    ("POST", "/api/v1/jobs/data/inventory"): "job",
    ("POST", "/api/v1/jobs/backtest"): "job",
    ("POST", "/api/v1/jobs/research"): "job",
    ("POST", "/api/v1/jobs/broker/verify"): "job",
    ("POST", "/api/v1/jobs/{job_id}/cancel"): "job",
    ("POST", "/api/v1/jobs/{job_id}/retry"): "job",
    ("POST", "/api/v1/data/uploads"): "upload",
    ("POST", "/api/v1/data/uploads/{upload_id}/import"): "job",
    ("POST", "/api/v1/data/uploads/{upload_id}/discard"): "upload",
    ("POST", "/api/v1/strategies/{strategy_id}/params"): "control",
    ("POST", "/api/v1/strategies/{strategy_id}/enabled"): "control",
    ("POST", "/api/v1/strategies/{strategy_id}/lifecycle"): "control",
    ("POST", "/api/v1/settings"): "control",
    ("POST", "/api/v1/trading/scheduler/start"): "control",
    ("POST", "/api/v1/trading/scheduler/stop"): UNLIMITED,  # stopping is always possible
    ("POST", "/api/v1/trading/mode"): "control",
}

Actor = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=2, max_length=128, pattern=r"^[^\x00-\x1f]+$"
    ),
]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=1000)]
JobId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ================================================================== request models
class JobRequest(Strict):
    actor: Actor = Field(description="the operator's name (audited)")


class DownloadRequest(JobRequest):
    params: job_catalog.DataDownloadParams = job_catalog.DataDownloadParams()


class ValidateRequest(JobRequest):
    params: job_catalog.DataValidateParams = job_catalog.DataValidateParams()


class SynthesizeRequest(JobRequest):
    params: job_catalog.DataSynthesizeParams = job_catalog.DataSynthesizeParams()


class InventoryRequest(JobRequest):
    params: job_catalog.DataInventoryParams = job_catalog.DataInventoryParams()


class BacktestRequest(JobRequest):
    params: job_catalog.BacktestParams


class ResearchRequest(JobRequest):
    params: job_catalog.ResearchParams = job_catalog.ResearchParams()


class BrokerVerifyRequest(JobRequest):
    params: job_catalog.BrokerVerifyParams = job_catalog.BrokerVerifyParams()


class ReasonedRequest(Strict):
    actor: Actor
    reason: Reason


class UploadQuery(Strict):
    symbol: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.]{0,9}$")]
    kind: Literal["bars", "actions"] = "bars"
    frequency: Literal["1d", "1min", "5min", "15min", "30min"] = "1d"
    filename: Annotated[str, Field(min_length=1, max_length=128)] = "upload.csv"
    actor: Actor


class ParamsRequest(ReasonedRequest):
    params: dict[
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")], int | float | str | bool
    ] = Field(min_length=1, max_length=40)


class EnabledRequest(ReasonedRequest):
    enabled: bool


class LifecycleRequest(ReasonedRequest):
    target: Literal["research", "validated", "paper", "shadow", "disabled"]
    confirm: str = ""


class SettingChange(Strict):
    path: Annotated[str, Field(pattern=r"^[a-z_]+(\.[a-z_]+){1,5}$")]
    value: int | float | str | bool | None = None
    reset: bool = False


class SettingsRequest(ReasonedRequest):
    expected_revision: Annotated[int, Field(ge=0)]
    changes: list[SettingChange] = Field(min_length=1, max_length=20)
    confirm: str = ""


class SchedulerStartRequest(ReasonedRequest):
    confirm: str


class ModeRequest(ReasonedRequest):
    target: Literal["shadow", "paper"]
    confirm: str


# ================================================================== response models
class Job(BaseModel):
    id: str
    job_type: str
    title: str
    params: Row
    status: str
    requested_by: str
    requested_at: str | None
    started_at: str | None
    finished_at: str | None
    heartbeat_at: str | None
    progress: float | None
    message: str
    result: Row | None
    error: str | None
    config_version: str | None
    retry_of: str | None
    cancel_requested: bool
    retryable: bool


class JobDetail(Job):
    logs: list[Row]


class JobList(BaseModel):
    jobs: list[Job]
    worker: Row | None


class MutationResult(BaseModel):
    ok: bool
    message: str
    job: Job | None = None
    detail: Row = Field(default_factory=dict)


def _job(row: Row) -> Job:
    return Job(
        **{k: row.get(k) for k in Job.model_fields if k not in ("title", "retryable")},
        title=job_catalog.TITLES.get(row["job_type"], row["job_type"]),
        retryable=row["job_type"] in job_catalog.IDEMPOTENT
        and row["status"] in ("failed", "cancelled", "succeeded"),
    )


# ================================================================== router
def control_router(sv: ApiServices, auth: Any, limiter: RateLimiter) -> APIRouter:
    r = APIRouter(prefix="/api/v1", dependencies=[auth])

    # ---------------------------------------------------------------- helpers
    def repo() -> ControlRepository:
        if sv.db is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "audit database not configured (DATABASE_URL)"
            )
        return ControlRepository(sv.db)

    def effective() -> tuple[LoadedConfig, Row]:
        try:
            return effective_config(sv.loaded, sv.db)
        except DatabaseUnavailableError:
            raise
        except AQError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"runtime configuration cannot be applied: {exc.message}",
            ) from exc

    def audit(
        request: Request, actor: str, action: str, target: str, outcome: str, detail: Row
    ) -> None:
        repo().record_event(
            actor=actor,
            action=action,
            target=target,
            outcome=outcome,
            detail=detail,
            client=client_of(request),
            now=sv.clock.now(),
        )

    def refuse(
        request: Request, actor: str, action: str, target: str, code: int, message: str
    ) -> HTTPException:
        audit(request, actor, action, target, "refused", {"reason": message})
        return HTTPException(code, message)

    def limit(request: Request, method: str, path: str) -> None:
        bucket = CONTROL_MUTATIONS[(method, path)]
        if bucket != UNLIMITED:
            limiter.check(bucket, client_of(request))

    def worker_status() -> Row | None:
        beats = repo().heartbeats()
        if not beats:
            return None
        latest = beats[0]
        from datetime import datetime

        seen = datetime.fromisoformat(str(latest["last_seen_at"]))
        latest["online"] = sv.clock.now() - seen < WORKER_STALE
        return latest

    def queue(
        request: Request, job_type: str, params: job_catalog.JobParams, actor: str, path: str
    ) -> MutationResult:
        limit(request, "POST", path)
        job, created = repo().create_job(
            job_type, params.model_dump(mode="json"), actor, sv.clock.now()
        )
        audit(
            request,
            actor,
            f"job.{job_type}",
            job["id"],
            "accepted",
            {"params": job["params"], "created": created},
        )
        w = worker_status()
        note = "" if w and w.get("online") else " No worker is online; it starts when one is."
        msg = ("queued" if created else "an identical job is already queued or running") + note
        return MutationResult(ok=True, message=msg.strip(), job=_job(job))

    # ================================================================ jobs
    @r.get("/jobs", response_model=JobList, tags=["jobs"])
    def jobs(
        limit_: int = Query(100, ge=1, le=500, alias="limit"),
        status_: str | None = Query(
            None, alias="status", pattern="^(queued|running|succeeded|failed|cancelled)$"
        ),
        job_type: str | None = Query(None, pattern=r"^[a-z_.]{1,40}$"),
    ) -> JobList:
        rows = repo().list_jobs(limit_, status=status_, job_type=job_type)
        return JobList(jobs=[_job(x) for x in rows], worker=worker_status())

    @r.get("/jobs/{job_id}", response_model=JobDetail, tags=["jobs"])
    def job_detail(job_id: JobId) -> JobDetail:
        row = repo().get_job(job_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job")
        return JobDetail(**_job(row).model_dump(), logs=repo().job_logs(job_id))

    @r.post("/jobs/data/download", response_model=MutationResult, tags=["jobs"])
    def job_download(body: DownloadRequest, request: Request) -> MutationResult:
        return queue(
            request, "data.download", body.params, body.actor, "/api/v1/jobs/data/download"
        )

    @r.post("/jobs/data/validate", response_model=MutationResult, tags=["jobs"])
    def job_validate(body: ValidateRequest, request: Request) -> MutationResult:
        return queue(
            request, "data.validate", body.params, body.actor, "/api/v1/jobs/data/validate"
        )

    @r.post("/jobs/data/synthesize", response_model=MutationResult, tags=["jobs"])
    def job_synthesize(body: SynthesizeRequest, request: Request) -> MutationResult:
        return queue(
            request, "data.synthesize", body.params, body.actor, "/api/v1/jobs/data/synthesize"
        )

    @r.post("/jobs/data/inventory", response_model=MutationResult, tags=["jobs"])
    def job_inventory(body: InventoryRequest, request: Request) -> MutationResult:
        return queue(
            request, "data.inventory", body.params, body.actor, "/api/v1/jobs/data/inventory"
        )

    @r.post("/jobs/backtest", response_model=MutationResult, tags=["jobs"])
    def job_backtest(body: BacktestRequest, request: Request) -> MutationResult:
        loaded, _row = effective()
        catalog = StrategyCatalog.from_config(loaded.settings.strategies)
        eligible = {s.strategy_id for s in catalog.eligible(TradingMode.BACKTEST)}
        unknown = [s for s in body.params.strategies if s not in eligible]
        if unknown:
            raise refuse(
                request,
                body.actor,
                "job.backtest.run",
                ",".join(unknown),
                400,
                f"not available for backtesting (unknown or disabled): {unknown}",
            )
        if body.params.strategy_params:
            try:
                backtest_service.apply_strategy_params(
                    loaded,
                    backtest_service.BacktestRequest(
                        strategies=list(body.params.strategies),
                        strategy_params={
                            k: dict(v) for k, v in body.params.strategy_params.items()
                        },
                    ),
                )
            except AQError as exc:
                raise refuse(
                    request,
                    body.actor,
                    "job.backtest.run",
                    ",".join(body.params.strategy_params),
                    400,
                    f"invalid strategy parameters: {exc.message}",
                ) from exc
        return queue(request, "backtest.run", body.params, body.actor, "/api/v1/jobs/backtest")

    @r.post("/jobs/research", response_model=MutationResult, tags=["jobs"])
    def job_research(body: ResearchRequest, request: Request) -> MutationResult:
        return queue(request, "research.run", body.params, body.actor, "/api/v1/jobs/research")

    @r.post("/jobs/broker/verify", response_model=MutationResult, tags=["jobs"])
    def job_broker(body: BrokerVerifyRequest, request: Request) -> MutationResult:
        return queue(
            request, "broker.verify", body.params, body.actor, "/api/v1/jobs/broker/verify"
        )

    @r.post("/jobs/{job_id}/cancel", response_model=MutationResult, tags=["jobs"])
    def job_cancel(job_id: JobId, body: ReasonedRequest, request: Request) -> MutationResult:
        limit(request, "POST", "/api/v1/jobs/{job_id}/cancel")
        try:
            row = repo().request_cancel(job_id, sv.clock.now())
        except PersistenceError as exc:
            raise refuse(request, body.actor, "job.cancel", job_id, 409, exc.message) from exc
        audit(request, body.actor, "job.cancel", job_id, "accepted", {"reason": body.reason})
        return MutationResult(ok=True, message=row["message"], job=_job(row))

    @r.post("/jobs/{job_id}/retry", response_model=MutationResult, tags=["jobs"])
    def job_retry(job_id: JobId, body: JobRequest, request: Request) -> MutationResult:
        limit(request, "POST", "/api/v1/jobs/{job_id}/retry")
        row = repo().get_job(job_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job")
        if row["job_type"] not in job_catalog.IDEMPOTENT:
            raise refuse(
                request,
                body.actor,
                "job.retry",
                job_id,
                409,
                f"{row['job_type']} jobs are not safe to retry automatically",
            )
        if row["status"] in ("queued", "running"):
            raise refuse(request, body.actor, "job.retry", job_id, 409, "job is still active")
        params = job_catalog.parse(row["job_type"], row["params"])
        job, created = repo().create_job(
            row["job_type"],
            params.model_dump(mode="json"),
            body.actor,
            sv.clock.now(),
            retry_of=job_id,
        )
        audit(request, body.actor, "job.retry", job_id, "accepted", {"new_job": job["id"]})
        return MutationResult(
            ok=True, message="queued again" if created else "already queued", job=_job(job)
        )

    # ================================================================ data
    @r.get("/data/datasets", tags=["data"])
    def datasets() -> Row:
        rows = repo().inventory()
        loaded, _row = effective()
        return {
            "datasets": rows,
            "updated_at": max((str(x.get("updated_at")) for x in rows), default=None),
            "primary_provider": loaded.settings.data.primary_provider,
            "file_import_adjustment": loaded.settings.data.file_import.adjustment,
            "synthetic_warning": data_service.SYNTHETIC_WARNING,
        }

    @r.get("/data/options", tags=["data"])
    def data_options() -> Row:
        loaded, _row = effective()
        s = loaded.settings
        w = worker_status()
        creds = (w or {}).get("status", {}).get("credentials_configured", {})
        providers = [
            {"name": "file", "label": "Uploaded / imported files", "configured": True},
            {
                "name": "alpaca",
                "label": "Alpaca market data",
                "configured": bool(creds.get("ALPACA_API_KEY_ID"))
                and bool(creds.get("ALPACA_API_SECRET_KEY")),
            },
            {
                "name": "polygon",
                "label": "Polygon",
                "configured": bool(creds.get("POLYGON_API_KEY")),
            },
        ]
        return {
            "providers": providers,
            "symbols": [i.symbol for i in s.universe.instruments],
            "frequencies": [f.value for f in Frequency],
            "default_provider": s.data.primary_provider,
            "default_start": s.data.history_start.isoformat(),
            "synthetic_products": sorted(s.data.synthetic.products),
            "max_upload_mb": data_service.MAX_UPLOAD_BYTES // (1024 * 1024),
            "upload_format": {
                "bars": "date,open,high,low,close,volume (daily) - extra columns are ignored",
                "actions": "ex_date,type,ratio,amount (type = split | cash_dividend)",
                "adjustment": s.data.file_import.adjustment,
            },
        }

    @r.get("/data/uploads", tags=["data"])
    def uploads(limit_: int = Query(50, ge=1, le=200, alias="limit")) -> Row:
        return {"uploads": repo().list_uploads(limit_)}

    @r.post("/data/uploads", response_model=MutationResult, tags=["data"])
    async def upload(request: Request, q: Annotated[UploadQuery, Depends()]) -> MutationResult:
        limit(request, "POST", "/api/v1/data/uploads")
        name = q.filename
        if (
            "/" in name
            or "\\" in name
            or ".." in name
            or not re.fullmatch(r"[A-Za-z0-9 _.()-]+\.csv", name)
        ):
            raise refuse(
                request,
                q.actor,
                "data.upload",
                q.symbol,
                400,
                "file name must be a plain name ending in .csv (no paths)",
            )
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in ("text/csv", "text/plain", "application/octet-stream", "application/csv"):
            raise refuse(
                request, q.actor, "data.upload", q.symbol, 415, "upload must be a CSV file"
            )
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > data_service.MAX_UPLOAD_BYTES:
            raise refuse(request, q.actor, "data.upload", q.symbol, 413, "file is too large")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > data_service.MAX_UPLOAD_BYTES:
                raise refuse(request, q.actor, "data.upload", q.symbol, 413, "file is too large")
        content = bytes(body)
        loaded, _row = effective()
        try:
            preview = data_service.preview_upload(
                loaded,
                sv.calendar,
                sv.clock,
                content,
                symbol=q.symbol,
                kind=q.kind,
                frequency=Frequency(q.frequency),
            )
        except AQError as exc:
            raise refuse(request, q.actor, "data.upload", q.symbol, 400, exc.message) from exc
        row = repo().create_upload(
            symbol=q.symbol,
            kind=q.kind,
            frequency=q.frequency,
            filename_hint=name,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            status="validated" if preview.ok else "invalid",
            preview=preview.to_json(),
            requested_by=q.actor,
            now=sv.clock.now(),
        )
        audit(
            request,
            q.actor,
            "data.upload",
            row["id"],
            "accepted",
            {
                "symbol": q.symbol,
                "kind": q.kind,
                "rows": preview.rows,
                "valid": preview.ok,
                "sha256": row["sha256"],
            },
        )
        msg = (
            "validated - review the preview, then import"
            if preview.ok
            else "the file failed validation and cannot be imported"
        )
        return MutationResult(ok=preview.ok, message=msg, detail={"upload": row})

    @r.get("/data/uploads/{upload_id}", tags=["data"])
    def upload_detail(upload_id: JobId) -> Row:
        row = repo().get_upload(upload_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown upload")
        return row

    @r.post("/data/uploads/{upload_id}/import", response_model=MutationResult, tags=["data"])
    def upload_import(upload_id: JobId, body: JobRequest, request: Request) -> MutationResult:
        limit(request, "POST", "/api/v1/data/uploads/{upload_id}/import")
        try:
            repo().set_upload_status(upload_id, "queued", sv.clock.now(), expect=("validated",))
        except PersistenceError as exc:
            raise refuse(
                request,
                body.actor,
                "data.import",
                upload_id,
                409,
                f"{exc.message}: only a validated upload can be imported",
            ) from exc
        params = job_catalog.DataImportParams(upload_id=upload_id)
        job, _created = repo().create_job(
            "data.import_upload", params.model_dump(mode="json"), body.actor, sv.clock.now()
        )
        repo().set_upload_status(upload_id, "queued", sv.clock.now(), job_id=job["id"])
        audit(request, body.actor, "data.import", upload_id, "accepted", {"job": job["id"]})
        return MutationResult(ok=True, message="import queued", job=_job(job))

    @r.post("/data/uploads/{upload_id}/discard", response_model=MutationResult, tags=["data"])
    def upload_discard(upload_id: JobId, body: ReasonedRequest, request: Request) -> MutationResult:
        limit(request, "POST", "/api/v1/data/uploads/{upload_id}/discard")
        try:
            repo().set_upload_status(
                upload_id, "discarded", sv.clock.now(), expect=("validated", "invalid")
            )
        except PersistenceError as exc:
            raise refuse(request, body.actor, "data.discard", upload_id, 409, exc.message) from exc
        audit(request, body.actor, "data.discard", upload_id, "accepted", {"reason": body.reason})
        return MutationResult(ok=True, message="discarded")

    # ================================================================ backtests & research
    @r.get("/backtests/options", tags=["backtests"])
    def backtest_options() -> Row:
        loaded, _row = effective()
        s = loaded.settings
        catalog = StrategyCatalog.from_config(s.strategies)
        classes = registry()
        strategies = [
            {
                "strategy_id": st.strategy_id,
                "label": f"{classes[e.version.implementation].description} ({st.strategy_id})",
                "family": e.version.family.value,
                "lifecycle": e.version.lifecycle.value,
                "signal_symbol": st.signal_symbol,
                "long_only_1x": st.p_float("max_long_exposure") <= 1.0
                and st.p_float("max_short_exposure") == 0.0,
                "implementation": e.version.implementation,
                "title": classes[e.version.implementation].title or st.strategy_id,
                "description": classes[e.version.implementation].description,
                "summary": classes[e.version.implementation].summary,
                "params": dict(st.params),
                "param_schema": strategy_service.param_schema(e.version.implementation),
            }
            for e in catalog.entries
            for st in [e.strategy]
            if st in catalog.eligible(TradingMode.BACKTEST)
        ]
        bt = s.backtest
        sources = sorted(
            {
                row["source"]
                for row in repo().inventory()
                if not row.get("is_synthetic") and row.get("adjustment") == "all"
            }
        )
        return {
            "strategies": strategies,
            "sources": sources,
            "defaults": {
                "source": s.data.primary_provider,
                "initial_capital": bt.initial_capital,
                "execution": bt.execution,
                "execution_delay_bars": bt.execution_delay_bars,
                "use_synthetic_history": bt.use_synthetic_history,
                "costs": {
                    "commission_per_share": bt.costs.commission_per_share,
                    "commission_per_order": bt.costs.commission_per_order,
                    "commission_minimum": bt.costs.commission_minimum,
                    "slippage_bps": bt.costs.slippage_bps,
                    "impact_coefficient_bps": bt.costs.impact_coefficient_bps,
                    "max_participation": bt.costs.max_participation,
                },
                "half_spread_bps": bt.costs.half_spread_bps,
            },
            "executions": ["near_close", "next_open", "next_close", "closing_auction"],
            "run_modes": {
                "ensemble": "Combined ensemble: ONE backtest; the selected strategies' signals "
                "are combined by the configured ensemble, allocation policy and risk engine into "
                "one portfolio (the long-standing behaviour).",
                "independent": "Independent comparison: one backtest PER strategy over the "
                "identical period, data, starting capital, execution, costs and risk settings, "
                "shown side by side. Not a portfolio.",
            },
            "allocation": bt.allocation.model_dump(mode="json"),
            "disclaimer": "Backtests are hypothetical: simulated fills and costs on historical "
            "data. They do not show that a strategy is profitable or predict future returns.",
        }

    @r.get("/backtests/runs", tags=["backtests"])
    def backtest_runs(limit_: int = Query(50, ge=1, le=200, alias="limit")) -> Row:
        jobs = repo().list_jobs(limit_, job_type="backtest.run")
        return {"runs": repo().list_backtests(limit_), "jobs": [_job(j) for j in jobs]}

    @r.get("/backtests/runs/{job_id}", tags=["backtests"])
    def backtest_run(job_id: JobId) -> Row:
        row = repo().get_backtest(job_id)
        job = repo().get_job(job_id)
        if row is None and job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown backtest")
        return {"run": row, "job": None if job is None else _job(job)}

    @r.get("/research/runs", tags=["research"])
    def research_runs(limit_: int = Query(50, ge=1, le=200, alias="limit")) -> Row:
        loaded, _row = effective()
        from adaptive_quant.quant.research.pipeline import research_candidates

        catalog = StrategyCatalog.from_config(loaded.settings.strategies)
        eligible = {st.strategy_id for st in catalog.eligible(TradingMode.BACKTEST)}
        versions = [e.version for e in catalog.entries if e.version.strategy_id in eligible]
        return {
            "jobs": [_job(j) for j in repo().list_jobs(limit_, job_type="research.run")],
            "candidates": [v.strategy_id for v in research_candidates(versions)],
            "eligible": sorted(eligible),
            "defaults": {
                "simulations": loaded.settings.research.monte_carlo.simulations,
                "scheme": loaded.settings.research.walk_forward.scheme,
                "use_synthetic_history": loaded.settings.backtest.use_synthetic_history,
            },
            "gates": loaded.settings.research.gates.model_dump(mode="json"),
            "promotion": "A research run can only move a strategy between research and "
            "validated in the governance ledger; paper, shadow and live need a human.",
        }

    # ================================================================ strategies
    def _evidence(sid: str) -> Row:
        research_jobs = repo().list_jobs(50, status="succeeded", job_type="research.run")
        latest_research = None
        for j in research_jobs:
            for entry in (j.get("result") or {}).get("ranked", []):
                if entry.get("strategy_id") == sid:
                    latest_research = {"job_id": j["id"], "finished_at": j["finished_at"], **entry}
                    break
            if latest_research:
                break
        latest_bt = next(
            (b for b in repo().list_backtests(100) if sid in (b.get("strategies") or [])), None
        )
        return {"research": latest_research, "backtest": latest_bt}

    @r.get("/strategies/manager", tags=["strategies"])
    def strategy_manager() -> Row:
        loaded, row = effective()
        rows = strategy_service.strategy_rows(loaded, sv.loaded)
        for x in rows:
            x["evidence"] = _evidence(x["strategy_id"])
        return {
            "strategies": rows,
            "runtime_revision": row["revision"],
            "lifecycle_events": AuditReads(repo().db).lifecycle_events(100),
            "rules": {
                "order": "research -> validated -> paper -> shadow -> live_approved",
                "live_approved": "never from the UI: reviewed change to strategies.yaml",
                "params": "editable only in research or disabled (a new version needs "
                "new evidence)",
                "enabled_vs_eligible": "enabled allows research/backtests; trading also needs "
                "lifecycle paper or later",
            },
            "promotion_confirm": ctl.PROMOTION_CONFIRM,
        }

    def _save_overrides(
        request: Request,
        actor: str,
        reason: str,
        action: str,
        sid: str,
        new_overrides: Row,
        change: Row,
    ) -> LoadedConfig:
        before, row = effective()
        try:
            after = resolve(sv.loaded, row["overlay"], new_overrides, int(row["revision"]) + 1)
        except AQError as exc:
            raise refuse(request, actor, action, sid, 400, exc.message) from exc
        try:
            repo().save_runtime_config(
                expected_revision=int(row["revision"]),
                overlay=row["overlay"],
                strategy_overrides=new_overrides,
                actor=actor,
                now=sv.clock.now(),
                changes=[{**change, "reason": reason}],
                version_before=before.config_version,
                version_after=after.config_version,
            )
        except PersistenceError as exc:
            raise refuse(request, actor, action, sid, 409, exc.message) from exc
        return after

    @r.post("/strategies/{strategy_id}/params", response_model=MutationResult, tags=["strategies"])
    def strategy_params(
        strategy_id: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")],
        body: ParamsRequest,
        request: Request,
    ) -> MutationResult:
        limit(request, "POST", "/api/v1/strategies/{strategy_id}/params")
        loaded, row = effective()
        try:
            overrides = strategy_service.with_params(
                loaded, row["strategy_overrides"], strategy_id, dict(body.params)
            )
        except AQError as exc:
            raise refuse(
                request, body.actor, "strategy.params", strategy_id, 409, exc.message
            ) from exc
        after = _save_overrides(
            request,
            body.actor,
            body.reason,
            "strategy.params",
            strategy_id,
            overrides,
            {
                "kind": "strategy",
                "path": f"{strategy_id}.params",
                "old": row["strategy_overrides"].get(strategy_id, {}).get("params"),
                "new": overrides[strategy_id]["params"],
            },
        )
        audit(
            request,
            body.actor,
            "strategy.params",
            strategy_id,
            "accepted",
            {"params": body.params, "reason": body.reason, "config_version": after.config_version},
        )
        return MutationResult(ok=True, message=f"parameters saved (config {after.config_version})")

    @r.post("/strategies/{strategy_id}/enabled", response_model=MutationResult, tags=["strategies"])
    def strategy_enabled(
        strategy_id: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")],
        body: EnabledRequest,
        request: Request,
    ) -> MutationResult:
        limit(request, "POST", "/api/v1/strategies/{strategy_id}/enabled")
        _loaded, row = effective()
        overrides = strategy_service.with_enabled(
            row["strategy_overrides"], strategy_id, body.enabled
        )
        after = _save_overrides(
            request,
            body.actor,
            body.reason,
            "strategy.enabled",
            strategy_id,
            overrides,
            {
                "kind": "strategy",
                "path": f"{strategy_id}.enabled",
                "old": None,
                "new": body.enabled,
            },
        )
        audit(
            request,
            body.actor,
            "strategy.enabled",
            strategy_id,
            "accepted",
            {"enabled": body.enabled, "reason": body.reason},
        )
        return MutationResult(
            ok=True,
            message=f"{'enabled' if body.enabled else 'disabled'} (config {after.config_version}); "
            "trading eligibility still depends on the lifecycle",
        )

    @r.post(
        "/strategies/{strategy_id}/lifecycle", response_model=MutationResult, tags=["strategies"]
    )
    def strategy_lifecycle(
        strategy_id: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")],
        body: LifecycleRequest,
        request: Request,
    ) -> MutationResult:
        limit(request, "POST", "/api/v1/strategies/{strategy_id}/lifecycle")
        loaded, row = effective()
        current = next(
            (e.lifecycle for e in loaded.settings.strategies.strategies if e.id == strategy_id),
            None,
        )
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown strategy")
        from adaptive_quant.governance.lifecycle import is_promotion

        target = StrategyLifecycle(body.target)
        if is_promotion(current, target) and body.confirm != ctl.PROMOTION_CONFIRM:
            raise refuse(
                request,
                body.actor,
                "strategy.lifecycle",
                strategy_id,
                400,
                f"promotions need the confirmation phrase {ctl.PROMOTION_CONFIRM!r}",
            )
        try:
            overrides, record = strategy_service.with_lifecycle(
                loaded,
                row["strategy_overrides"],
                strategy_id,
                body.target,
                f"operator:{body.actor}",
                body.reason,
                sv.clock.now(),
            )
        except AQError as exc:
            raise refuse(
                request,
                body.actor,
                "strategy.lifecycle",
                strategy_id,
                400,
                exc.message + (f" - {exc.hint}" if exc.hint else ""),
            ) from exc
        after = _save_overrides(
            request,
            body.actor,
            body.reason,
            "strategy.lifecycle",
            strategy_id,
            overrides,
            {
                "kind": "strategy",
                "path": f"{strategy_id}.lifecycle",
                "old": current.value,
                "new": body.target,
            },
        )
        entry = StrategyCatalog.from_config(loaded.settings.strategies).get(strategy_id)
        ref = ReferenceRepository(repo().db)
        ref.ensure_strategy_version(
            StrategyVersionRecord(
                strategy_id=strategy_id,
                version=record.strategy_version,
                implementation=entry.version.implementation,
                family=entry.version.family.value,
                params=dict(entry.version.params),
                code_hash=entry.version.code_version,
                notes=entry.version.notes,
            )
        )
        ref.record_lifecycle_event(
            LifecycleEventRecord(
                strategy_id=strategy_id,
                version=record.strategy_version,
                from_state=record.from_state.value,
                to_state=record.to_state.value,
                actor_id=record.actor.id,
                actor_kind=record.actor.kind.value,
                reason=record.reason,
                at=record.at,
            )
        )
        audit(
            request,
            body.actor,
            "strategy.lifecycle",
            strategy_id,
            "accepted",
            {
                "from": current.value,
                "to": body.target,
                "reason": body.reason,
                "config_version": after.config_version,
            },
        )
        return MutationResult(
            ok=True,
            message=f"{strategy_id}: {current.value} -> {body.target} (recorded; config "
            f"{after.config_version}). If automation is running and this strategy can no "
            "longer trade, no further order is sent for it and automation stops; newly "
            "eligible strategies join after a stop/start.",
        )

    # ================================================================ settings
    @r.get("/settings", tags=["settings"])
    def settings_view() -> Row:
        loaded, row = effective()
        creds = ((worker_status() or {}).get("status") or {}).get("credentials_configured", {})
        return {
            "config_version": loaded.config_version,
            "reviewed_config_version": sv.loaded.config_version,
            "runtime": {k: row.get(k) for k in ("revision", "overlay", "updated_by", "updated_at")},
            "settings": settings_catalog(sv.loaded.settings, loaded.settings),
            "secrets": [
                {"name": n, "category": c.value, "label": label, "configured": creds.get(n)}
                for n, c, label in SECRET_SETTINGS
                if n != "AQ_API_TOKEN"
            ],
            "classes": {
                "runtime": "safe to change here; applies to the next job",
                "runtime_confirm": "risk limit: tighten-only here, with confirmation",
                "restart": "read at start-up: change the configuration and redeploy",
                "secret": "deployment secret: set in the platform, never here",
                "immutable": "safety-critical: reviewed repository change only",
            },
            "history": repo().runtime_changes(100),
            "risk_confirm": ctl.RISK_CONFIRM,
            "trading_mode": {
                "active": loaded.settings.trading.mode.value,
                "reviewed": sv.loaded.settings.trading.mode.value,
                "options": [
                    {"value": m, "label": m.capitalize(), "explanation": text}
                    for m, text in readiness.MODE_EXPLANATIONS.items()
                ],
                "live_trading": "LOCKED / NOT AVAILABLE",
                "note": "Changed with a guarded workflow (paper needs a verified Alpaca paper "
                "account). Changing the mode never starts automation, releases the kill switch "
                "or promotes a strategy.",
            },
            "worker_note": "Settings apply to the next job. A running scheduler keeps the "
            "configuration it started with until it is stopped and started again.",
        }

    @r.post("/settings", response_model=MutationResult, tags=["settings"])
    def settings_change(body: SettingsRequest, request: Request) -> MutationResult:
        limit(request, "POST", "/api/v1/settings")
        before, row = effective()
        if body.expected_revision != int(row["revision"]):
            raise refuse(
                request,
                body.actor,
                "settings.change",
                "settings",
                409,
                "settings changed since you loaded them; reload and try again",
            )
        editable = {s.path: s for s in EDITABLE}
        overlay = dict(row["overlay"])
        changes: list[Row] = []
        reviewed = sv.loaded.settings.model_dump(mode="json")
        for ch in body.changes:
            spec = editable.get(ch.path)
            if spec is None or ch.path == TRADING_MODE_PATH:
                raise refuse(
                    request,
                    body.actor,
                    "settings.change",
                    ch.path,
                    400,
                    f"{ch.path} cannot be changed here (see its classification)",
                )
            if spec.klass is SettingClass.RUNTIME_CONFIRM and body.confirm != ctl.RISK_CONFIRM:
                raise refuse(
                    request,
                    body.actor,
                    "settings.change",
                    ch.path,
                    400,
                    f"risk-limit changes need the confirmation {ctl.RISK_CONFIRM!r}",
                )
            old = get_path(before.settings.model_dump(mode="json"), ch.path)
            if ch.reset:
                overlay.pop(ch.path, None)
                new = get_path(reviewed, ch.path)
            else:
                if ch.value is None:
                    raise refuse(
                        request, body.actor, "settings.change", ch.path, 400, "a value is required"
                    )
                overlay[ch.path] = ch.value
                new = ch.value
            changes.append(
                {"kind": "setting", "path": ch.path, "old": old, "new": new, "reason": body.reason}
            )
        try:
            after = resolve(sv.loaded, overlay, row["strategy_overrides"], int(row["revision"]) + 1)
        except (AQError, ValidationError) as exc:
            msg = exc.message if isinstance(exc, AQError) else str(exc)
            raise refuse(request, body.actor, "settings.change", "settings", 400, msg) from exc
        try:
            revision = repo().save_runtime_config(
                expected_revision=int(row["revision"]),
                overlay=overlay,
                strategy_overrides=row["strategy_overrides"],
                actor=body.actor,
                now=sv.clock.now(),
                changes=changes,
                version_before=before.config_version,
                version_after=after.config_version,
            )
        except PersistenceError as exc:
            raise refuse(
                request, body.actor, "settings.change", "settings", 409, exc.message
            ) from exc
        audit(
            request,
            body.actor,
            "settings.change",
            "settings",
            "accepted",
            {"changes": changes, "revision": revision, "config_version": after.config_version},
        )
        return MutationResult(
            ok=True,
            message=f"saved as revision {revision} (config {after.config_version})",
            detail={"revision": revision, "config_version": after.config_version},
        )

    # ================================================================ trading

    def _scheduler_state() -> Row:
        st = repo().get_state(ctl.SCHEDULER_KEY)
        value = (st or {}).get("value") or {}
        return {
            "desired": "running" if ctl.desired_running(st) else "stopped",
            "desired_mode": value.get("mode") if isinstance(value, dict) else None,
            "updated_by": None if st is None else st["updated_by"],
            "updated_at": None if st is None else st["updated_at"],
        }

    def _verification() -> Row | None:
        v = repo().get_state(ctl.BROKER_KEY)
        return None if v is None else dict(v["value"])

    def _credentials(w: Row | None) -> bool | None:
        creds = ((w or {}).get("status") or {}).get("credentials_configured")
        if not isinstance(creds, dict):
            return None
        return bool(creds.get("ALPACA_API_KEY_ID")) and bool(creds.get("ALPACA_API_SECRET_KEY"))

    def _broker(loaded: LoadedConfig, w: Row | None) -> Row:
        from urllib.parse import urlparse

        host = urlparse(loaded.settings.broker.alpaca.paper_base_url).hostname
        verification = _verification()
        now = sv.clock.now()
        return {
            "provider": loaded.settings.broker.provider,
            "status": readiness.broker_label(_credentials(w), verification, now),
            "identity": "Alpaca PAPER (simulated funds)" if host == PAPER_HOST else "UNKNOWN",
            "endpoint_host": host,
            "paper_endpoint": host == PAPER_HOST,
            "live_adapter_available": False,
            "credentials_configured": _credentials(w),
            "verification": verification,
            "verification_problems": readiness.verification_problems(verification, now),
        }

    def _kill_switch() -> tuple[Row, bool]:
        """(status, known). Unreadable state counts as engaged and unknown."""
        try:
            ks = sv.kill_switch.status()
        except Exception:  # noqa: BLE001 - fail closed
            return {"engaged": True, "reason": "unreadable", "fail_safe": True}, False
        known = not (ks.fail_safe and "unreadable" in ks.reason)
        return ks.model_dump(mode="json"), known

    def _eligible(loaded: LoadedConfig, mode: TradingMode) -> list[str]:
        catalog = StrategyCatalog.from_config(loaded.settings.strategies)
        return [st.strategy_id for st in catalog.eligible(mode)]

    def _required_symbols(loaded: LoadedConfig, mode: TradingMode) -> list[str]:
        s = loaded.settings
        catalog = StrategyCatalog.from_config(s.strategies)
        strategies = catalog.eligible(mode)
        return sorted({*s.universe.tradeable_symbols, *(st.signal_symbol for st in strategies)})

    def _readiness(loaded: LoadedConfig, w: Row | None) -> list[readiness.Item]:
        mode = loaded.settings.trading.mode
        recon = AuditReads(repo().db).reconciliations(1)
        ks, _known = _kill_switch()
        return readiness.start_checklist(
            mode=mode.value,
            now=sv.clock.now(),
            worker=w,
            desired=repo().get_state(ctl.SCHEDULER_KEY),
            kill_switch=ks,
            eligible=_eligible(loaded, mode)
            if mode in (TradingMode.PAPER, TradingMode.SHADOW)
            else [],
            required_symbols=_required_symbols(loaded, mode),
            inventory=repo().inventory(),
            reconciliation=recon[0] if recon else None,
            verification=_verification(),
        )

    def _paper_switch(loaded: LoadedConfig, w: Row | None) -> Row:
        """What must hold before SHADOW -> PAPER is accepted."""
        verification = _verification()
        problems = readiness.verification_problems(verification, sv.clock.now())
        _ks, known = _kill_switch()
        if not known:
            problems.append("the kill-switch state cannot be read")
        sched = _scheduler_state()
        running = ((w or {}).get("status") or {}).get("scheduler") or {}
        if sched["desired"] == "running" or running.get("state") == "running":
            problems.append("stop automation before changing the trading mode")
        if loaded.settings.broker.provider != "alpaca":
            problems.append("the broker is not Alpaca")
        return {
            "allowed": not problems,
            "problems": problems,
            "checks": (verification or {}).get("checks", []),
            "verified_at": (verification or {}).get("verified_at"),
        }

    @r.get("/trading/control", tags=["trading"])
    def trading_control() -> Row:
        loaded, _row = effective()
        s = loaded.settings
        w = worker_status()
        status_ = ((w or {}).get("status") or {}).get("scheduler") or {}
        mode = s.trading.mode
        eligible = (
            _eligible(loaded, mode) if mode in (TradingMode.PAPER, TradingMode.SHADOW) else []
        )
        reads = AuditReads(repo().db)
        recon = reads.reconciliations(1)
        freshness = [
            {k: d.get(k) for k in ("source", "symbol", "adjustment", "last", "fresh", "freshness")}
            for d in repo().inventory()
            if d.get("symbol") in _required_symbols(loaded, mode)
            and d.get("adjustment") == "raw"
            and not d.get("is_synthetic")
        ]
        broker = _broker(loaded, w)
        ks, ks_known = _kill_switch()
        gate = bool(((w or {}).get("status") or {}).get("master_gate"))
        sched = _scheduler_state()
        state = str(status_.get("state", "unknown"))
        items = _readiness(loaded, w)
        return {
            "environment": s.app.environment.value,
            "mode": mode.value,
            "mode_label": "PAPER TRADING — SIMULATED FUNDS"
            if mode is TradingMode.PAPER
            else "SHADOW MODE — NO ORDERS SENT",
            "mode_explanations": readiness.MODE_EXPLANATIONS,
            "uses_real_money": mode.uses_real_money,
            "real_money_possible": False,
            "real_money_note": "This deployment cannot trade real money: live mode is refused "
            "by the configuration policy, the start-up check and the broker factory, and no "
            "live broker adapter exists in this version.",
            "live_trading": "LOCKED / NOT AVAILABLE",
            "worker": None
            if w is None
            else {
                "online": w.get("online"),
                "last_seen": w.get("last_seen_at"),
                "worker_id": w.get("worker_id"),
            },
            "automation": {
                "label": "RUNNING" if state == "running" else "STOPPED",
                "state": state,
                "desired": sched["desired"],
                "desired_mode": sched["desired_mode"],
                "detail": status_.get("detail", "no worker status yet"),
                "running_mode": status_.get("mode"),
                "restart_needed": status_.get("restart_needed", False),
                "next_wake": status_.get("next_wake"),
                "master_gate": gate,
                "master_gate_note": "AQ_SCHEDULER_ENABLED on the worker service (deployment "
                "setting; not changeable here)",
                "available": gate,
                "unavailable_reason": None
                if gate
                else "Automation unavailable — deployment scheduler master gate is OFF.",
            },
            # kept for older clients
            "scheduler": {
                "master_gate": gate,
                **sched,
                "state": state,
                "detail": status_.get("detail", "no worker status yet"),
                "restart_needed": status_.get("restart_needed", False),
                "next_wake": status_.get("next_wake"),
            },
            "kill_switch": {
                **ks,
                "label": "ENGAGED" if ks.get("engaged") else "RELEASED",
                "known": ks_known,
            },
            "broker": broker,
            "eligible_strategies": eligible,
            "eligible_count": len(eligible),
            "data_freshness": freshness,
            "reconciliation": recon[0] if recon else None,
            "latest_preflight": reads.latest_preflight(),
            "start_readiness": {
                "ready": not readiness.failed(items),
                "items": [i.to_json() for i in items],
            },
            "paper_switch": _paper_switch(loaded, w),
            "confirmations": {
                "start_shadow": ctl.START_SHADOW_CONFIRM,
                "start_paper": ctl.START_PAPER_CONFIRM,
                "paper_mode": ctl.PAPER_MODE_CONFIRM,
                "shadow_mode": ctl.SHADOW_MODE_CONFIRM,
            },
        }

    @r.post("/trading/scheduler/start", response_model=MutationResult, tags=["trading"])
    def scheduler_start(body: SchedulerStartRequest, request: Request) -> MutationResult:
        limit(request, "POST", "/api/v1/trading/scheduler/start")
        loaded, _row = effective()
        mode = loaded.settings.trading.mode
        phrase = ctl.START_PAPER_CONFIRM if mode is TradingMode.PAPER else ctl.START_SHADOW_CONFIRM
        if mode not in (TradingMode.PAPER, TradingMode.SHADOW):
            raise refuse(
                request,
                body.actor,
                "scheduler.start",
                "scheduler",
                400,
                f"automation runs in shadow or paper mode only (mode is {mode})",
            )
        if body.confirm != phrase:
            raise refuse(
                request,
                body.actor,
                "scheduler.start",
                "scheduler",
                400,
                f"type the confirmation phrase exactly: {phrase!r}",
            )
        missing = readiness.failed(_readiness(loaded, worker_status()))
        if missing:
            raise refuse(
                request,
                body.actor,
                "scheduler.start",
                "scheduler",
                409,
                f"not ready to start {mode.value} automation: "
                + "; ".join(f"{i.label}: {i.detail}" for i in missing),
            )
        repo().set_state(
            ctl.SCHEDULER_KEY,
            {"desired": "running", "mode": mode.value, "reason": body.reason},
            body.actor,
            sv.clock.now(),
        )
        audit(
            request,
            body.actor,
            "scheduler.start",
            "scheduler",
            "accepted",
            {"mode": mode.value, "reason": body.reason, "confirmation": "typed"},
        )
        return MutationResult(
            ok=True,
            message=f"{mode.value} automation requested. The worker starts it within ~15 s "
            "after its own checks; the kill switch, data, broker, reconciliation and "
            "pre-flight checks still apply to every cycle.",
        )

    def _stop_automation(request: Request, actor: str, reason: str) -> None:
        repo().set_state(
            ctl.SCHEDULER_KEY, {"desired": "stopped", "reason": reason}, actor, sv.clock.now()
        )
        audit(request, actor, "scheduler.stop", "scheduler", "accepted", {"reason": reason})

    @r.post("/trading/scheduler/stop", response_model=MutationResult, tags=["trading"])
    def scheduler_stop(body: ReasonedRequest, request: Request) -> MutationResult:
        limit(request, "POST", "/api/v1/trading/scheduler/stop")
        _stop_automation(request, body.actor, body.reason)
        return MutationResult(
            ok=True,
            message="automation stop requested: no new order is transmitted from now on, and "
            "the worker stops the scheduler within ~15 s. To block new risk immediately, also "
            "engage the kill switch.",
        )

    @r.post("/trading/mode", response_model=MutationResult, tags=["trading"])
    def trading_mode(body: ModeRequest, request: Request) -> MutationResult:
        limit(request, "POST", "/api/v1/trading/mode")
        before, row = effective()
        current = before.settings.trading.mode.value
        phrase = ctl.PAPER_MODE_CONFIRM if body.target == "paper" else ctl.SHADOW_MODE_CONFIRM
        if body.confirm != phrase:
            raise refuse(
                request,
                body.actor,
                "trading.mode",
                body.target,
                400,
                f"type the confirmation phrase exactly: {phrase!r}",
            )
        if body.target == current:
            raise refuse(
                request,
                body.actor,
                "trading.mode",
                body.target,
                409,
                f"the trading mode is already {current}",
            )
        w = worker_status()
        verification = _verification()
        automation_stopped = False
        if body.target == "paper":
            check = _paper_switch(before, w)
            if not check["allowed"]:
                raise refuse(
                    request,
                    body.actor,
                    "trading.mode",
                    body.target,
                    409,
                    "cannot switch to PAPER: " + "; ".join(check["problems"]),
                )
        elif ctl.desired_running(repo().get_state(ctl.SCHEDULER_KEY)):
            # PAPER -> SHADOW fails closed: stop automation *first*; from this moment the
            # worker's transmit guard refuses every paper order, even mid-step
            _stop_automation(request, body.actor, f"switching to shadow: {body.reason}")
            automation_stopped = True
        overlay = dict(row["overlay"])
        overlay[TRADING_MODE_PATH] = body.target
        try:
            after = resolve(sv.loaded, overlay, row["strategy_overrides"], int(row["revision"]) + 1)
        except AQError as exc:
            raise refuse(
                request, body.actor, "trading.mode", body.target, 400, exc.message
            ) from exc
        try:
            repo().save_runtime_config(
                expected_revision=int(row["revision"]),
                overlay=overlay,
                strategy_overrides=row["strategy_overrides"],
                actor=body.actor,
                now=sv.clock.now(),
                changes=[
                    {
                        "kind": "trading_mode",
                        "path": TRADING_MODE_PATH,
                        "old": current,
                        "new": body.target,
                        "reason": body.reason,
                    }
                ],
                version_before=before.config_version,
                version_after=after.config_version,
            )
        except PersistenceError as exc:
            raise refuse(
                request, body.actor, "trading.mode", body.target, 409, exc.message
            ) from exc
        effective_after, _ = effective()
        v = verification or {}
        audit(
            request,
            body.actor,
            "trading.mode",
            body.target,
            "accepted",
            {
                "previous_mode": current,
                "requested_mode": body.target,
                "resulting_mode": effective_after.settings.trading.mode.value,
                "confirmation": "typed",
                "reason": body.reason,
                "broker_verification": {
                    "ok": v.get("ok"),
                    "is_paper": v.get("is_paper"),
                    "endpoint_host": v.get("endpoint_host"),
                    "verified_at": v.get("verified_at"),
                },
                "automation_stopped": automation_stopped,
                "config_version": after.config_version,
            },
        )
        if body.target == "paper":
            msg = (
                "Trading mode is now PAPER — SIMULATED FUNDS. Automation is still STOPPED, "
                "the kill switch is unchanged and no strategy was promoted: start paper "
                "trading separately when the readiness checklist is complete."
            )
        else:
            msg = "Trading mode is now SHADOW — no orders are sent." + (
                " Automation was stopped first; no paper order can be transmitted any more."
                if automation_stopped
                else ""
            )
        return MutationResult(
            ok=True, message=msg, detail={"mode": effective_after.settings.trading.mode.value}
        )

    @r.get("/trading/live-readiness", tags=["trading"])
    def live_readiness() -> Row:
        loaded, _row = effective()
        s = loaded.settings
        catalog = StrategyCatalog.from_config(s.strategies)
        live_ok = [st.strategy_id for st in catalog.eligible(TradingMode.LIVE)]
        lt = s.trading.live_trading
        items = [
            (
                "Production environment",
                s.app.environment.value == "production",
                f"environment is {s.app.environment.value}",
                "deployment configuration",
            ),
            (
                "trading.mode: live",
                s.trading.mode is TradingMode.LIVE,
                f"mode is {s.trading.mode.value}",
                "reviewed change to production.yaml (never "
                "from the UI; the runtime overlay refuses it)",
            ),
            (
                "live_trading.enabled",
                lt.enabled,
                str(lt.enabled),
                "reviewed change to production.yaml",
            ),
            (
                "Exact written acknowledgement",
                bool(lt.acknowledgement),
                "set" if lt.acknowledgement else "not set",
                "reviewed change to production.yaml",
            ),
            (
                "AQ_LIVE_TRADING_CONFIRM in the deployment",
                False,
                "must stay unset in this deployment (containers refuse to start if it is set)",
                "deployment secret; never set or created by the software",
            ),
            (
                "A LIVE_APPROVED strategy with written approval",
                bool(live_ok),
                f"{len(live_ok)} live-approved",
                "reviewed change to strategies.yaml",
            ),
            (
                "A live broker adapter",
                False,
                "no live broker adapter exists in this version",
                "a separately reviewed implementation with live-account verification",
            ),
            (
                "Deployment start-up check allows live",
                False,
                "aq deploy check refuses live mode in this deployment",
                "separate reviewed change",
            ),
            (
                "Paper-trading track record reviewed",
                False,
                "not tracked by the software",
                "human review (docs/SAFETY.md checklist)",
            ),
        ]
        return {
            "live_possible": False,
            "summary": "Live trading is locked. Every prerequisite below must be met by "
            "separately reviewed changes; none can be satisfied from this dashboard.",
            "items": [
                {"requirement": a, "satisfied": b, "status": c, "how": d} for a, b, c, d in items
            ],
        }

    # ================================================================ audit
    @r.get("/audit/events", tags=["audit"])
    def audit_events(
        limit_: int = Query(200, ge=1, le=1000, alias="limit"),
        action: str | None = Query(None, pattern=r"^[a-z_.]{1,64}$"),
    ) -> Row:
        return {"events": repo().events(limit_, action)}

    return r
