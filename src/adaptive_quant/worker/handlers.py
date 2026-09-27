"""Job handlers: validated parameters -> existing services -> JSON result.

Data, backtest and research handlers only call ``adaptive_quant.services``;
they cannot reach a broker or the order manager. ``broker.verify`` builds the
configured (paper-only) broker adapter and calls ``get_account`` - a read; it
never submits, cancels or modifies an order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.control import jobs as job_catalog
from adaptive_quant.control.state import BROKER_KEY
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import DataProviderError
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database
from adaptive_quant.quant.data.bars import Frequency
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.services import backtests, research
from adaptive_quant.services import data as data_service
from adaptive_quant.worker.context import JobContext

Result = dict[str, Any]


@dataclass
class WorkerEnv:
    base: LoadedConfig
    secrets: Secrets
    clock: Clock
    db: Database
    calendar: TradingCalendar

    @property
    def repo(self) -> ControlRepository:
        return ControlRepository(self.db)


Handler = Callable[[JobContext, WorkerEnv, LoadedConfig, Any], Result]


def refresh_inventory(env: WorkerEnv, loaded: LoadedConfig) -> int:
    rows = data_service.inventory(loaded, env.clock, env.calendar)
    env.repo.replace_inventory([vars(r) for r in rows], env.clock.now())
    return len(rows)


def _data_download(ctx: JobContext, env: WorkerEnv, cfg: LoadedConfig, p: Any) -> Result:
    res = data_service.download(
        cfg,
        env.secrets,
        env.clock,
        env.calendar,
        provider=p.provider,
        symbols=p.symbols,
        frequency=Frequency(p.frequency),
        start=p.start,
        end=p.end,
        progress=ctx.progress,
    )
    ctx.progress("updating the dataset list", 0.95)
    refresh_inventory(env, cfg)
    return res.to_json()


def _data_validate(ctx: JobContext, env: WorkerEnv, cfg: LoadedConfig, p: Any) -> Result:
    res = data_service.validate(
        cfg,
        env.clock,
        env.calendar,
        source=p.source,
        symbols=p.symbols,
        frequency=Frequency(p.frequency),
        require_fresh=p.require_fresh,
        progress=ctx.progress,
    )
    refresh_inventory(env, cfg)
    return res.to_json()


def _data_synthesize(ctx: JobContext, env: WorkerEnv, cfg: LoadedConfig, p: Any) -> Result:
    res = data_service.synthesize(
        cfg,
        env.clock,
        source=p.source,
        symbols=list(p.symbols) if p.symbols else None,
        progress=ctx.progress,
    )
    refresh_inventory(env, cfg)
    return {**res.to_json(), "warning": data_service.SYNTHETIC_WARNING}


def _data_inventory(ctx: JobContext, env: WorkerEnv, cfg: LoadedConfig, _p: Any) -> Result:
    ctx.progress("scanning the market-data store", 0.1)
    return {"datasets": refresh_inventory(env, cfg)}


def _data_import(ctx: JobContext, env: WorkerEnv, cfg: LoadedConfig, p: Any) -> Result:
    repo = env.repo
    upload = repo.get_upload(p.upload_id)
    if upload is None:
        raise DataProviderError(f"unknown upload {p.upload_id}")
    if upload["status"] != "queued":
        raise DataProviderError(f"upload {p.upload_id} is {upload['status']}, not queued")
    content = repo.upload_content(p.upload_id)
    try:
        res = data_service.import_upload(
            cfg,
            env.secrets,
            env.clock,
            env.calendar,
            content,
            symbol=upload["symbol"],
            kind=upload["kind"],
            frequency=Frequency(upload["frequency"]),
            progress=ctx.progress,
        )
    except Exception:
        repo.set_upload_status(p.upload_id, "failed", env.clock.now())
        raise
    repo.set_upload_status(
        p.upload_id, "imported" if res.ok else "failed", env.clock.now(), job_id=ctx.job_id
    )
    refresh_inventory(env, cfg)
    return res.to_json()


def _backtest(ctx: JobContext, env: WorkerEnv, cfg: LoadedConfig, p: Any) -> Result:
    req = backtests.BacktestRequest(
        strategies=list(p.strategies),
        source=p.source,
        start=p.start,
        end=p.end,
        initial_capital=p.initial_capital,
        execution=p.execution,
        execution_delay_bars=p.execution_delay_bars,
        use_synthetic_history=p.use_synthetic_history,
        costs=dict(p.costs),
    )
    outcome = backtests.run(cfg, env.clock, req, calendar=env.calendar, progress=ctx.progress)
    summary = backtests.summarize(outcome)
    env.repo.save_backtest(
        ctx.job_id,
        strategies=req.strategies,
        source=outcome.source,
        start=outcome.start,
        end=outcome.end,
        initial_capital=float(summary["initial_capital"] or 0.0),
        summary=summary,
        report_path=str(outcome.report),
        config_version=outcome.config_version,
    )
    return {
        "disclaimer": summary["disclaimer"],
        "period": summary["period"],
        "ending_equity": summary["ending_equity"],
        "headline": summary["headline"],
        "has_synthetic": summary["has_synthetic"],
    }


def _research(ctx: JobContext, env: WorkerEnv, cfg: LoadedConfig, p: Any) -> Result:
    outcome = research.run(
        cfg,
        env.clock,
        research.ResearchRequest(
            strategies=list(p.strategies) if p.strategies else None,
            source=p.source,
            start=p.start,
            end=p.end,
            use_synthetic_history=p.use_synthetic_history,
            simulations=p.simulations,
            scheme=p.scheme,
        ),
        progress=ctx.progress,
    )
    return research.summarize(outcome)


def _required_symbols(cfg: LoadedConfig) -> list[str]:
    """Tradeable instruments plus the signal symbols of PAPER-eligible strategies."""
    from adaptive_quant.core.enums import TradingMode
    from adaptive_quant.quant.strategies.catalog import StrategyCatalog

    s = cfg.settings
    eligible = StrategyCatalog.from_config(s.strategies).eligible(TradingMode.PAPER)
    return sorted({*s.universe.tradeable_symbols, *(st.signal_symbol for st in eligible)})


def _broker_verify(ctx: JobContext, env: WorkerEnv, cfg: LoadedConfig, _p: Any) -> Result:
    """Read-only paper-account verification and pre-flight (never submits anything).

    Builds the configured adapter through the normal factory, which only knows the
    Alpaca *paper* adapter (and that adapter refuses any host but the paper host),
    reads the account, positions and open orders, and checks the database, the
    stored market data, the last reconciliation and the kill-switch state.
    """
    from adaptive_quant.persistence.repositories import CycleRepository
    from adaptive_quant.trading.brokers.alpaca import PAPER_HOST
    from adaptive_quant.trading.brokers.factory import build_broker
    from adaptive_quant.trading.safety.broker_checks import BrokerStateCheck, ReconciliationCheck
    from adaptive_quant.trading.safety.db_checks import DatabaseCheck
    from adaptive_quant.trading.safety.kill_switch_store import build_kill_switch
    from adaptive_quant.worker.context import safe_error

    s = cfg.settings
    host = urlparse(s.broker.alpaca.paper_base_url).hostname
    checks: list[dict[str, Any]] = []

    def check(name: str, label: str, passed: bool, detail: str) -> bool:
        checks.append({"name": name, "label": label, "passed": bool(passed), "detail": detail})
        return bool(passed)

    ctx.progress("checking the broker configuration", 0.05)
    check("provider", "Broker is Alpaca", s.broker.provider == "alpaca", str(s.broker.provider))
    check(
        "paper_endpoint", "Endpoint is the Alpaca paper environment", host == PAPER_HOST, str(host)
    )
    present = env.secrets.present()
    creds = bool(present.get("ALPACA_API_KEY_ID")) and bool(present.get("ALPACA_API_SECRET_KEY"))
    check(
        "credentials",
        "Credentials configured on the worker",
        creds,
        "present" if creds else "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY missing",
    )
    account = None
    broker = None
    if creds and host == PAPER_HOST and s.broker.provider == "alpaca":
        ctx.progress("authenticating with the Alpaca paper account (read-only)", 0.2)
        try:
            broker = build_broker(cfg, env.secrets, env.clock)
            account = broker.get_account()  # read only
            check("authentication", "Credentials authenticate", True, "account read")
        except Exception as exc:  # noqa: BLE001 - reported, never re-raised raw
            check("authentication", "Credentials authenticate", False, safe_error(exc, env.secrets))
    else:
        check("authentication", "Credentials authenticate", False, "not attempted")
    is_paper = bool(account is not None and account.is_paper and host == PAPER_HOST)
    check(
        "paper_account",
        "Account is a paper (simulated-funds) account",
        is_paper,
        "paper" if is_paper else "not confirmed as a paper account",
    )
    if broker is not None and account is not None:
        ctx.progress("reading positions and open orders", 0.4)
        r = BrokerStateCheck(broker).run()
        check(
            "broker_connectivity",
            "Broker account, positions and orders readable",
            r.passed,
            r.detail,
        )
    else:
        check(
            "broker_connectivity",
            "Broker account, positions and orders readable",
            False,
            "broker not reachable",
        )
    r = DatabaseCheck(env.db).run()
    check("database", "Database reachable", r.passed, r.detail)
    ctx.progress("validating stored market data", 0.6)
    symbols = _required_symbols(cfg)
    data = data_service.validate(cfg, env.clock, env.calendar, symbols=symbols, require_fresh=True)
    check(
        "market_data",
        "Required market data valid and fresh",
        data.ok,
        "; ".join(o.summary for o in data.outcomes if not o.ok) or ", ".join(symbols),
    )
    r = ReconciliationCheck(CycleRepository(env.db).latest_reconciliation).run()
    check("reconciliation", "Reconciliation healthy", r.passed, r.detail)
    engaged = None
    try:
        ks = build_kill_switch(cfg, env.clock, env.db).status()
        engaged = ks.engaged
        check(
            "kill_switch_known",
            "Kill-switch state readable",
            True,
            "ENGAGED" if ks.engaged else "released",
        )
    except Exception as exc:  # noqa: BLE001
        check(
            "kill_switch_known", "Kill-switch state readable", False, safe_error(exc, env.secrets)
        )

    from adaptive_quant.control.readiness import PREFLIGHT_CHECKS, SWITCH_CHECKS

    by = {c["name"]: c["passed"] for c in checks}
    result: Result = {
        "provider": s.broker.provider,
        "endpoint_host": host,
        "paper_endpoint": host == PAPER_HOST,
        "is_paper": is_paper,
        "ok": all(by.get(n) for n in SWITCH_CHECKS),
        "preflight_ok": all(by.get(n) for n in PREFLIGHT_CHECKS),
        "kill_switch_engaged": engaged,
        "required_symbols": symbols,
        "checks": checks,
        "verified_at": env.clock.now().isoformat(),
    }
    env.repo.set_state(BROKER_KEY, result, f"worker:{ctx.worker_id}", env.clock.now())
    if not result["ok"]:
        bad = [c["label"] + ": " + c["detail"] for c in checks if not c["passed"]]
        raise DataProviderError("paper account verification failed: " + "; ".join(bad))
    return result


HANDLERS: dict[str, Handler] = {
    "data.download": _data_download,
    "data.validate": _data_validate,
    "data.synthesize": _data_synthesize,
    "data.inventory": _data_inventory,
    "data.import_upload": _data_import,
    "backtest.run": _backtest,
    "research.run": _research,
    "broker.verify": _broker_verify,
}

if set(HANDLERS) != set(job_catalog.JOB_TYPES):  # pragma: no cover - import-time guard
    raise RuntimeError("every job type needs exactly one handler")
