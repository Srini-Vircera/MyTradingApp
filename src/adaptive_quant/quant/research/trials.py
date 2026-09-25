"""Trials: one backtest of one strategy with one parameter set - and their registry.

Every trial run by the research pipeline is appended to an append-only JSONL
**trial registry** so multiple-testing corrections (Deflated Sharpe) count
*every* configuration ever tried on the same data, not just the ones shown.
A trial is identified by its strategy version (implementation, code version,
parameters), the data fingerprint, the backtest settings and the period, so
re-running an identical trial does not inflate the count.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from multiprocessing import get_context
from pathlib import Path

import numpy as np
import pandas as pd

from adaptive_quant.core.clock import MARKET_TZ, ensure_utc
from adaptive_quant.core.errors import AQError, StrategyError
from adaptive_quant.core.models import Instrument
from adaptive_quant.quant.backtest.engine import BacktestEngine, EngineSettings
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.quant.research.robustness import Coord
from adaptive_quant.quant.strategies.base import Strategy
from adaptive_quant.quant.strategies.params import ParamValue
from adaptive_quant.quant.strategies.registry import registry


@dataclass(frozen=True)
class TrialSpec:
    strategy_id: str
    implementation: str
    params: Mapping[str, ParamValue]
    coord: Coord = ()
    label: str = "configured"

    def build(self) -> Strategy:
        return registry()[self.implementation](self.strategy_id, dict(self.params))


@dataclass
class TrialOutcome:
    spec: TrialSpec
    version_id: str
    trial_id: str
    equity: pd.Series[float]
    returns: pd.Series[float]  # daily simple returns, net of all costs; index = sessions
    exposure: pd.Series[float]
    turnover: pd.Series[float]
    synthetic: pd.Series[bool]
    trade_returns: list[float]  # round-trip P&L as a fraction of equity at entry
    fills: int
    cancelled: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class TrialContext:
    """Everything a trial needs; picklable so trials can run in worker processes."""

    frames: Mapping[str, pd.DataFrame]
    synthetic: Mapping[str, pd.Series[bool]]
    instruments: Mapping[str, Instrument]
    calendar: TradingCalendar
    settings: EngineSettings
    start: date
    end: date
    data_fingerprint: str

    def with_settings(self, settings: EngineSettings) -> TrialContext:
        return replace(self, settings=settings)

    @property
    def settings_fingerprint(self) -> str:
        s = self.settings
        payload = json.dumps(
            {
                "backtest": s.config.model_dump(mode="json", exclude={"report_dir"}),
                "threshold": s.rebalance_threshold,
                "fractional": s.allow_fractional,
                "risk": None if s.risk_limits is None else s.risk_limits.model_dump(mode="json"),
                "ensemble": None if s.ensemble is None else s.ensemble.model_dump(mode="json"),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def data_fingerprint(frames: Mapping[str, pd.DataFrame]) -> str:
    """Content hash of every price history used (order-independent)."""
    h = hashlib.sha256()
    for sym in sorted(frames):
        h.update(sym.encode())
        h.update(pd.util.hash_pandas_object(frames[sym], index=True).to_numpy().tobytes())
    return h.hexdigest()[:16]


def trial_id(version_id: str, ctx: TrialContext) -> str:
    key = f"{version_id}|{ctx.data_fingerprint}|{ctx.settings_fingerprint}|{ctx.start}|{ctx.end}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def run_trial(spec: TrialSpec, ctx: TrialContext) -> TrialOutcome:
    """One deterministic backtest. Invalid parameter combinations become failed outcomes."""
    try:
        strategy = spec.build()
    except StrategyError as exc:
        return _failed(spec, "", ctx, f"invalid parameters: {exc}")
    tid = trial_id(strategy.version_id, ctx)
    try:
        result = BacktestEngine(
            frames=ctx.frames,
            strategies=[strategy],
            instruments=ctx.instruments,
            calendar=ctx.calendar,
            settings=ctx.settings,
            synthetic=ctx.synthetic,
        ).run(ctx.start, ctx.end)
    except AQError as exc:
        return _failed(spec, strategy.version_id, ctx, f"{type(exc).__name__}: {exc}", tid)
    daily = result.daily
    equity = daily["equity"].astype(float)
    returns = equity.pct_change().iloc[1:]
    trade_returns = []
    for rt in result.portfolio.round_trips:
        pos = int(pd.DatetimeIndex(equity.index).searchsorted(pd.Timestamp(rt.entry_time), "right"))
        base = float(equity.iloc[max(pos - 1, 0)])
        trade_returns.append(float(rt.pnl) / base)
    return TrialOutcome(
        spec=spec,
        version_id=strategy.version_id,
        trial_id=tid,
        equity=equity,
        returns=returns,
        exposure=daily["gross_exposure"].astype(float),
        turnover=daily["turnover"].astype(float),
        synthetic=daily["synthetic"].astype(bool),
        trade_returns=trade_returns,
        fills=len(result.fills),
        cancelled=len(result.cancelled),
    )


def _failed(
    spec: TrialSpec, version_id: str, ctx: TrialContext, error: str, tid: str = ""
) -> TrialOutcome:
    empty = pd.Series(dtype=float)
    return TrialOutcome(
        spec=spec,
        version_id=version_id,
        trial_id=tid or trial_id(f"invalid:{spec.implementation}:{spec.label}", ctx),
        equity=empty,
        returns=empty,
        exposure=empty,
        turnover=empty,
        synthetic=pd.Series(dtype=bool),
        trade_returns=[],
        fills=0,
        cancelled=0,
        error=error,
    )


# ------------------------------------------------------------------ parallel execution
_WORKER_CTX: TrialContext | None = None


def _init_worker(ctx: TrialContext) -> None:
    global _WORKER_CTX
    _WORKER_CTX = ctx


def _run_in_worker(spec: TrialSpec) -> TrialOutcome:
    if _WORKER_CTX is None:  # pragma: no cover - initializer always runs first
        raise RuntimeError("worker context missing")
    return run_trial(spec, _WORKER_CTX)


def run_trials(
    specs: Sequence[TrialSpec], ctx: TrialContext, workers: int = 1
) -> list[TrialOutcome]:
    """Run trials in order (``workers`` > 1 uses separate processes; results are identical)."""
    if workers <= 1 or len(specs) <= 1:
        return [run_trial(s, ctx) for s in specs]
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=get_context("spawn"),
        initializer=_init_worker,
        initargs=(ctx,),
    ) as pool:
        return list(pool.map(_run_in_worker, specs))


# ------------------------------------------------------------------ registry
@dataclass(frozen=True)
class TrialRecord:
    trial_id: str
    run_id: str
    recorded_at: str
    strategy_id: str
    version_id: str
    params: dict[str, ParamValue]
    data_fingerprint: str
    settings_fingerprint: str
    start: str
    end: str
    sessions: int
    sharpe_annual: float | None
    synthetic_sessions: int
    purpose: str
    error: str | None = None


@dataclass
class TrialRegistry:
    """Append-only JSONL log of every trial. Never rewritten, never pruned."""

    path: Path
    _cache: list[TrialRecord] | None = field(default=None, repr=False)

    def records(self) -> list[TrialRecord]:
        if self._cache is None:
            out = []
            if self.path.exists():
                for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        out.append(TrialRecord(**json.loads(line)))
                    except (TypeError, ValueError) as exc:
                        raise AQError(
                            f"trial registry {self.path} line {n} is corrupt: {exc}",
                            hint="the registry is append-only evidence; restore it, do not edit it",
                        ) from exc
            self._cache = out
        return self._cache

    def __iter__(self) -> Iterator[TrialRecord]:
        return iter(self.records())

    def record(
        self,
        outcomes: Sequence[TrialOutcome],
        ctx: TrialContext,
        run_id: str,
        at: datetime,
        purpose: str,
    ) -> int:
        """Append outcomes; returns how many were *new* distinct trials."""
        known = {r.trial_id for r in self.records()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = 0
        lines = []
        for o in outcomes:
            rec = TrialRecord(
                trial_id=o.trial_id,
                run_id=run_id,
                recorded_at=ensure_utc(at).isoformat(),
                strategy_id=o.spec.strategy_id,
                version_id=o.version_id,
                params=dict(o.spec.params),
                data_fingerprint=ctx.data_fingerprint,
                settings_fingerprint=ctx.settings_fingerprint,
                start=ctx.start.isoformat(),
                end=ctx.end.isoformat(),
                sessions=len(o.equity),
                sharpe_annual=_annual_sharpe(o.returns) if o.ok else None,
                synthetic_sessions=int(o.synthetic.sum()) if o.ok else 0,
                purpose=purpose,
                error=o.error,
            )
            lines.append(json.dumps(rec.__dict__, sort_keys=True))
            self.records().append(rec)
            if o.trial_id not in known:
                known.add(o.trial_id)
                new += 1
        with self.path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
        return new

    def distinct_trials(
        self, data_fingerprint: str | None = None, purpose: str | None = None
    ) -> int:
        """Distinct successful trials, optionally only those on the given data / of a purpose."""
        return len(
            {
                r.trial_id
                for r in self.records()
                if r.error is None
                and (data_fingerprint is None or r.data_fingerprint == data_fingerprint)
                and (purpose is None or r.purpose == purpose)
            }
        )


def _annual_sharpe(r: pd.Series[float]) -> float | None:
    if len(r) < 2:
        return None
    sd = float(r.std(ddof=1))
    return float(r.mean() / sd * np.sqrt(252)) if sd > 1e-15 else None


def session_date(ts: pd.Timestamp) -> date:
    return pd.Timestamp(ts).tz_convert(MARKET_TZ).date()
