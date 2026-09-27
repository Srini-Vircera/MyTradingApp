"""FastAPI application factory.

* Every endpoint except the ``GET /api/v1/health/live`` and ``/health/ready``
  probes requires the operator bearer token (``AQ_API_TOKEN``; constant-time
  comparison; failures logged without the supplied value).
* State-changing endpoints are an explicit allowlist (``MUTATING_ROUTES``): the
  kill switch plus the control-plane mutations in ``api/control.py`` (queue
  worker jobs, CSV uploads, strategy parameters/enabled/lifecycle with governance,
  runtime settings, scheduler start/stop, shadow<->paper mode). Every mutation is
  authenticated, strictly validated, rate limited and audited. No endpoint can
  enable live trading, place/cancel orders, grant LIVE_APPROVED or edit secrets
  or the scheduler master gate (enforced by tests).
* Database problems return 503 with a readable message, never internals.
* Interactive docs are disabled (no third-party assets); the OpenAPI schema is
  at ``/api/v1/openapi.json`` and committed as ``apps/api/openapi.json``.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from adaptive_quant import __version__
from adaptive_quant.api.control import CONTROL_MUTATIONS, control_router
from adaptive_quant.api.security import RateLimiter, bearer, check_token, client_of, harden
from adaptive_quant.api.services import ApiServices
from adaptive_quant.config.schema import redact
from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.core.errors import (
    AQError,
    DatabaseUnavailableError,
    PersistenceError,
    SafetyViolation,
)
from adaptive_quant.persistence import migrate
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.explain import explain_decision
from adaptive_quant.persistence.reads import AuditReads
from adaptive_quant.persistence.repositories import MonitoringRepository
from adaptive_quant.quant.strategies.catalog import ELIGIBLE_LIFECYCLES, StrategyCatalog
from adaptive_quant.services.runtime import effective_config
from adaptive_quant.trading.safety.kill_switch import RELEASE_CONFIRMATION
from adaptive_quant.trading.scheduler.schedule import plan_at

API_PREFIX = "/api/v1"
ENGAGE_CONFIRMATION = "STOP AUTOMATED TRADING"
HYPOTHETICAL = (
    "Paper/simulated results and backtests are hypothetical "
    "and are not a prediction of future returns."
)

KILL_SWITCH_MUTATIONS = frozenset(
    {("POST", f"{API_PREFIX}/kill-switch/engage"), ("POST", f"{API_PREFIX}/kill-switch/release")}
)
#: The complete, explicit allowlist of state-changing routes (the kill switch plus the
#: control-plane mutations of PR #4). Tests assert that nothing else exists.
MUTATING_ROUTES = KILL_SWITCH_MUTATIONS | frozenset(CONTROL_MUTATIONS)

Rows = list[dict[str, Any]]


# ================================================================== response models
class Banner(BaseModel):
    environment: str
    mode: str
    uses_real_money: bool
    live_trading_enabled: bool
    config_version: str
    notice: str = HYPOTHETICAL


class KillSwitchView(BaseModel):
    engaged: bool
    reason: str
    actor: str | None
    changed_at: datetime | None
    fail_safe: bool


class Page(BaseModel):
    """A list page: the banner plus rows."""

    banner: Banner
    items: Rows
    count: int


class Overview(BaseModel):
    banner: Banner
    kill_switch: KillSwitchView
    session: dict[str, Any] | None
    latest_cycle: dict[str, Any] | None
    latest_performance: dict[str, Any] | None
    target: dict[str, Any] | None
    open_orders: int
    last_reconciliation: dict[str, Any] | None


class Portfolio(BaseModel):
    banner: Banner
    account: dict[str, Any] | None
    positions: Rows
    expected_positions: Rows
    target: dict[str, Any] | None


class Risk(BaseModel):
    banner: Banner
    latest_decision: dict[str, Any] | None
    history: Rows
    limits: dict[str, Any]


class Strategies(BaseModel):
    banner: Banner
    strategies: Rows
    lifecycle_events: Rows


class SystemHealth(BaseModel):
    banner: Banner
    version: str
    database: dict[str, Any]
    kill_switch: KillSwitchView
    latest_cycle: dict[str, Any] | None
    recent_errors: Rows
    recent_notifications: Rows


class Configuration(BaseModel):
    banner: Banner
    config_version: str
    warnings: list[str]
    live_trading_lock: dict[str, Any]
    settings: dict[str, Any]


class Backtests(BaseModel):
    banner: Banner
    backtests: Rows
    research: Rows


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=2, max_length=128, description="the operator's name")
    reason: str = Field(min_length=5, max_length=1000)
    confirm: str = Field(
        description=f"engage: {ENGAGE_CONFIRMATION!r}; release: {RELEASE_CONFIRMATION!r}"
    )


# ================================================================== factory
def create_app(services: ApiServices) -> FastAPI:
    app = FastAPI(
        title="Adaptive Quant operator API",
        version=__version__,
        description="Operator views and an allowlisted control plane for the paper/shadow "
        "trading platform: the kill switch, queued worker jobs (data, backtests, research, "
        "read-only broker verification), CSV uploads, strategy governance up to shadow, "
        "runtime settings and the shadow/paper scheduler switch. It cannot place orders, "
        "enable live trading, grant LIVE_APPROVED or change deployment secrets. " + HYPOTHETICAL,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.services = services
    app.middleware("http")(harden)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=services.loaded.settings.api.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(DatabaseUnavailableError)
    async def _db_down(_r: Request, exc: DatabaseUnavailableError) -> JSONResponse:
        return JSONResponse(
            {"detail": "audit database unavailable", "hint": exc.hint}, status_code=503
        )

    @app.exception_handler(PersistenceError)
    async def _db_error(_r: Request, _exc: PersistenceError) -> JSONResponse:
        return JSONResponse({"detail": "audit database error"}, status_code=503)

    @app.get(f"{API_PREFIX}/health/live", tags=["system"])
    def live() -> dict[str, str]:
        """Liveness probe (no authentication, no internals)."""
        return {"status": "ok"}

    @app.get(
        f"{API_PREFIX}/health/ready",
        tags=["system"],
        responses={503: {"description": "not ready"}},
    )
    def ready() -> JSONResponse:
        """Readiness probe for the deployment platform (no authentication, no internals).

        Ready only when the audit database is configured, reachable and migrated
        to the head revision."""
        ok = False
        if services.db is not None:
            try:
                services.db.ping()
                ok = migrate.current_revision(services.db) == migrate.head_revision()
            except (DatabaseUnavailableError, PersistenceError):
                ok = False
        return JSONResponse(
            {"status": "ready" if ok else "not ready"}, status_code=200 if ok else 503
        )

    limiter = RateLimiter()
    app.state.limiter = limiter
    app.include_router(_read_router(services))
    app.include_router(_kill_switch_router(services, limiter))
    app.include_router(control_router(services, _auth(services), limiter))
    return app


def _auth(services: ApiServices) -> Any:
    def dep(
        request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> None:
        check_token(services.token, creds, request.client.host if request.client else "?")

    return Depends(dep)


def _limit(services: ApiServices) -> Any:
    top = services.loaded.settings.api.max_page_size
    return Query(100, ge=1, le=top)


# ================================================================== read endpoints
def _read_router(sv: ApiServices) -> APIRouter:
    r = APIRouter(prefix=API_PREFIX, dependencies=[_auth(sv)])
    s = sv.loaded.settings
    env = s.app.environment.value

    def banner() -> Banner:
        """The *effective* mode (YAML + runtime overlay); unknown if it cannot be read."""
        mode, version = s.trading.mode, sv.loaded.config_version
        if sv.db is not None:
            try:
                eff, _row = effective_config(sv.loaded, sv.db)
                mode, version = eff.settings.trading.mode, eff.config_version
            except AQError:
                return Banner(
                    environment=env,
                    mode="unknown",
                    uses_real_money=False,
                    live_trading_enabled=s.trading.live_trading.enabled,
                    config_version=version,
                )
        return Banner(
            environment=env,
            mode=mode.value,
            uses_real_money=mode.uses_real_money,
            live_trading_enabled=s.trading.live_trading.enabled,
            config_version=version,
        )

    def reads() -> AuditReads:
        if sv.reads is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "audit database not configured (DATABASE_URL)"
            )
        return sv.reads

    def ks() -> KillSwitchView:
        st = sv.kill_switch.status()
        return KillSwitchView(**st.model_dump())

    def page(items: Rows) -> Page:
        return Page(banner=banner(), items=items, count=len(items))

    @r.get("/overview", response_model=Overview, tags=["pages"])
    def overview() -> Overview:
        db = reads()
        now = sv.clock.now()
        plan = plan_at(now, sv.calendar, s.schedule)
        session = None
        if plan is not None:
            session = {
                "date": plan.date.isoformat(),
                "close": plan.session.close.astimezone(MARKET_TZ).isoformat(),
                "early_close": plan.session.early_close,
                "order_cutoff": plan.order_cutoff.astimezone(MARKET_TZ).isoformat(),
                "steps": [
                    {"name": x.name, "at": x.at.astimezone(MARKET_TZ).isoformat()}
                    for x in plan.steps
                ],
            }
        perf = db.performance(env, 1)
        recon = db.reconciliations(1)
        open_states = (
            "created",
            "validated",
            "submitted",
            "acknowledged",
            "partially_filled",
            "unknown",
        )
        open_orders = sum(
            1 for o in db.orders(sv.loaded.settings.api.max_page_size) if o["state"] in open_states
        )
        return Overview(
            banner=banner(),
            kill_switch=ks(),
            session=session,
            latest_cycle=db.latest_cycle(),
            latest_performance=perf[-1] if perf else None,
            target=db.latest_target(),
            open_orders=open_orders,
            last_reconciliation=recon[0] if recon else None,
        )

    @r.get("/portfolio", response_model=Portfolio, tags=["pages"])
    def portfolio() -> Portfolio:
        db = reads()
        return Portfolio(
            banner=banner(),
            account=db.latest_account(),
            positions=db.latest_positions("broker"),
            expected_positions=db.latest_positions("expected"),
            target=db.latest_target(),
        )

    @r.get("/strategies", response_model=Strategies, tags=["pages"])
    def strategies(limit: int = _limit(sv)) -> Strategies:
        cat = StrategyCatalog.from_config(s.strategies)
        rows = [
            {
                "strategy_id": e.version.strategy_id,
                "family": e.version.family.value,
                "implementation": e.version.implementation,
                "version_id": e.version.version_id,
                "lifecycle": e.version.lifecycle.value,
                "enabled": e.version.enabled,
                "eligible_modes": sorted(
                    m.value for m, ok in ELIGIBLE_LIFECYCLES.items() if e.version.lifecycle in ok
                ),
                "params": e.version.params,
                "warmup_bars": e.version.warmup_bars,
                "approval": None
                if e.version.approval is None
                else e.version.approval.model_dump(mode="json"),
            }
            for e in cat.entries
        ]
        events = reads().lifecycle_events(limit) if sv.db is not None else []
        return Strategies(banner=banner(), strategies=rows, lifecycle_events=events)

    @r.get("/signals", response_model=Page, tags=["pages"])
    def signals(limit: int = _limit(sv), cycle_id: str | None = None) -> Page:
        return page(reads().signals(limit, cycle_id))

    @r.get("/risk", response_model=Risk, tags=["pages"])
    def risk(limit: int = _limit(sv)) -> Risk:
        history = reads().risk_decisions(limit)
        return Risk(
            banner=banner(),
            latest_decision=history[0] if history else None,
            history=history,
            limits=s.risk.model_dump(mode="json"),
        )

    @r.get("/performance", response_model=Page, tags=["pages"])
    def performance(limit: int = _limit(sv)) -> Page:
        return page(reads().performance(env, limit))

    @r.get("/backtests", response_model=Backtests, tags=["pages"])
    def backtests(limit: int = _limit(sv)) -> Backtests:
        bt_dir = sv.loaded.resolve_path(s.backtest.report_dir)
        rs_dir = sv.loaded.resolve_path(s.research.output_dir)
        return Backtests(
            banner=banner(),
            backtests=_reports(bt_dir, "metrics.json", limit),
            research=_reports(rs_dir, "research.json", limit),
        )

    @r.get("/orders", response_model=Page, tags=["pages"])
    def orders(
        limit: int = _limit(sv), state: str | None = None, cycle_id: str | None = None
    ) -> Page:
        return page(reads().orders(limit, state, cycle_id))

    @r.get("/shadow-orders", response_model=Page, tags=["pages"])
    def shadow_orders(limit: int = _limit(sv)) -> Page:
        return page(reads().shadow_orders(limit))

    @r.get("/executions", response_model=Page, tags=["pages"])
    def executions(limit: int = _limit(sv)) -> Page:
        return page(reads().executions(limit))

    @r.get("/reconciliation", response_model=Page, tags=["pages"])
    def reconciliation(limit: int = _limit(sv)) -> Page:
        return page(reads().reconciliations(limit))

    @r.get("/cycles", response_model=Page, tags=["pages"])
    def cycles(limit: int = _limit(sv)) -> Page:
        return page(reads().cycles(limit))

    @r.get("/cycles/{cycle_id}/explain", tags=["pages"])
    def explain(cycle_id: str) -> dict[str, Any]:
        """The full stored decision chain of one trading cycle."""
        if sv.db is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "audit database not configured"
            )
        try:
            return {"banner": banner().model_dump(mode="json"), **explain_decision(sv.db, cycle_id)}
        except PersistenceError as exc:
            if "unknown trading cycle" in str(exc):
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, f"unknown trading cycle {cycle_id}"
                ) from exc
            raise

    @r.get("/system/health", response_model=SystemHealth, tags=["pages"])
    def system_health(limit: int = Query(20, ge=1, le=200)) -> SystemHealth:
        database: dict[str, Any] = {"configured": sv.db is not None}
        latest = None
        errors: Rows = []
        notes: Rows = []
        if sv.db is not None:
            try:
                sv.db.ping()
                current, head = migrate.current_revision(sv.db), migrate.head_revision()
                database.update(
                    reachable=True, revision=current, head=head, at_head=current == head
                )
                db = AuditReads(sv.db)
                latest, errors, notes = db.latest_cycle(), db.errors(limit), db.notifications(limit)
            except (DatabaseUnavailableError, PersistenceError):
                database.update(reachable=False)
        return SystemHealth(
            banner=banner(),
            version=__version__,
            database=database,
            kill_switch=ks(),
            latest_cycle=latest,
            recent_errors=errors,
            recent_notifications=notes,
        )

    @r.get("/configuration", response_model=Configuration, tags=["pages"])
    def configuration() -> Configuration:
        """The reviewed YAML configuration, redacted (runtime changes: ``/settings``)."""
        lt = s.trading.live_trading
        return Configuration(
            banner=banner(),
            config_version=sv.loaded.config_version,
            warnings=list(sv.loaded.warnings),
            live_trading_lock={
                "mode": s.trading.mode.value,
                "uses_real_money": s.trading.mode.uses_real_money,
                "live_trading_enabled_in_config": lt.enabled,
                "changeable_via_api": False,
                "how_to_change": "live: never from the API (reviewed YAML change plus a deployment "
                "secret; see docs/SAFETY.md). Shadow <-> paper: the Trading Control page.",
            },
            settings=redact(s.model_dump(mode="json")),
        )

    @r.get("/kill-switch", response_model=KillSwitchView, tags=["kill switch"])
    def kill_switch_status() -> KillSwitchView:
        return ks()

    return r


def _reports(root: Path, filename: str, limit: int) -> Rows:
    """Summaries of stored report directories (newest first); never serves arbitrary files."""
    if not root.is_dir():
        return []
    out: Rows = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)[:limit]:
        f = d / filename
        summary: Any = None
        if f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                summary = (
                    data
                    if filename == "metrics.json"
                    else {
                        k: data.get(k)
                        for k in (
                            "run_id",
                            "period",
                            "trials_this_run",
                            "trials_registered",
                            "synthetic_sessions",
                        )
                    }
                )
            except (OSError, ValueError):
                summary = {"error": "unreadable"}
        out.append(
            {"name": d.name, "has_report": (d / "report.html").is_file(), "summary": summary}
        )
    return out


# ================================================================== kill switch (only mutations)
def _kill_switch_router(sv: ApiServices, limiter: RateLimiter) -> APIRouter:
    r = APIRouter(
        prefix=f"{API_PREFIX}/kill-switch", dependencies=[_auth(sv)], tags=["kill switch"]
    )

    def audit(request: Request, body: KillSwitchRequest, action: str, outcome: str) -> None:
        if sv.db is None:
            return
        # best effort: the kill switch itself (and its own audit trail) is authoritative
        with contextlib.suppress(DatabaseUnavailableError, PersistenceError):
            ControlRepository(sv.db).record_event(
                actor=body.actor,
                action=action,
                target="kill-switch",
                outcome=outcome,
                detail={"reason": body.reason},
                client=client_of(request),
                now=sv.clock.now(),
            )

    def record(engaged: bool, body: KillSwitchRequest) -> None:
        if sv.db is None or sv.loaded.settings.trading.kill_switch.store == "database":
            return  # the database store already appends to kill_switch_events
        # the file-based kill switch (with its own audit log) is authoritative
        with contextlib.suppress(DatabaseUnavailableError, PersistenceError):
            MonitoringRepository(sv.db).record_kill_switch_event(
                engaged, f"api:{body.actor}", body.reason, sv.clock.now()
            )

    @r.post("/engage", response_model=KillSwitchView)
    def engage(body: KillSwitchRequest, request: Request) -> KillSwitchView:
        """Stop new risk-increasing orders. Requires ``confirm`` = "STOP AUTOMATED TRADING".

        Never rate limited: stopping must always be possible."""
        if body.confirm != ENGAGE_CONFIRMATION:
            audit(request, body, "kill_switch.engage", "refused")
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"type the confirmation phrase exactly: {ENGAGE_CONFIRMATION!r}",
            )
        sv.kill_switch.engage(f"operator:{body.actor}", body.reason)
        record(True, body)
        audit(request, body, "kill_switch.engage", "accepted")
        return KillSwitchView(**sv.kill_switch.status().model_dump())

    @r.post("/release", response_model=KillSwitchView)
    def release(body: KillSwitchRequest, request: Request) -> KillSwitchView:
        """Re-enable automated trading.

        Requires ``confirm`` = "RE-ENABLE TRADING" and a named operator."""
        limiter.check("control", client_of(request))
        try:
            sv.kill_switch.release(f"operator:{body.actor}", body.reason, body.confirm)
        except SafetyViolation as exc:
            audit(request, body, "kill_switch.release", "refused")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        record(False, body)
        audit(request, body, "kill_switch.release", "accepted")
        return KillSwitchView(**sv.kill_switch.status().model_dump())

    return r
