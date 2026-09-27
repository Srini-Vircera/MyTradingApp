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


def _broker_verify(ctx: JobContext, env: WorkerEnv, cfg: LoadedConfig, _p: Any) -> Result:
    from adaptive_quant.trading.brokers.alpaca import PAPER_HOST
    from adaptive_quant.trading.brokers.factory import build_broker

    ctx.progress("connecting to the broker (read-only account check)", 0.2)
    host = urlparse(cfg.settings.broker.alpaca.paper_base_url).hostname
    result: Result = {
        "provider": cfg.settings.broker.provider,
        "endpoint_host": host,
        "paper_endpoint": host == PAPER_HOST,
        "verified_at": env.clock.now().isoformat(),
    }
    try:
        broker = build_broker(cfg, env.secrets, env.clock)
        account = broker.get_account()  # read only
        result.update(
            ok=True,
            is_paper=bool(account.is_paper) and host == PAPER_HOST,
            trading_blocked=account.trading_blocked,
            currency=account.currency,
        )
    except Exception as exc:  # noqa: BLE001 - any failure is reported, never re-raised raw
        from adaptive_quant.worker.context import safe_error

        result.update(ok=False, is_paper=False, error=safe_error(exc, env.secrets))
    env.repo.set_state(BROKER_KEY, result, f"worker:{ctx.worker_id}", env.clock.now())
    if not result["ok"]:
        raise DataProviderError(f"broker verification failed: {result.get('error')}")
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
