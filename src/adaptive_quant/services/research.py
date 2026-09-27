"""Research orchestration shared by ``aq research run`` and control-plane jobs.

Runs the existing M6 pipeline unchanged: parameter sweeps and robustness,
walk-forward, Monte Carlo and stress tests, bootstrap confidence intervals,
Deflated Sharpe, PBO, White's Reality Check / Hansen's SPA and
Benjamini-Hochberg FDR control, then the scorecard gates. The only lifecycle
change it can cause is the governance ledger's automated RESEARCH <-> VALIDATED
step; nothing here promotes a strategy to paper, shadow or live. Hypothetical.
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.core.clock import MARKET_TZ, Clock
from adaptive_quant.core.enums import TradingMode
from adaptive_quant.core.errors import ConfigurationError, StrategyError
from adaptive_quant.governance.research import GovernanceLedger
from adaptive_quant.quant.backtest.data import load_backtest_data
from adaptive_quant.quant.backtest.runner import engine_settings, risk_warmup_bars
from adaptive_quant.quant.data.calendar import nyse_calendar
from adaptive_quant.quant.data.factory import build_store
from adaptive_quant.quant.research.pipeline import (
    ResearchResult,
    research_candidates,
    run_research,
)
from adaptive_quant.quant.research.report import write_report
from adaptive_quant.quant.research.robustness import ParamGrid
from adaptive_quant.quant.research.trials import (
    TrialContext,
    TrialRegistry,
    TrialSpec,
    data_fingerprint,
)
from adaptive_quant.quant.strategies.base import Strategy
from adaptive_quant.quant.strategies.catalog import StrategyCatalog
from adaptive_quant.services.backtests import TRADEABLE, default_range

Progress = Callable[[str, float | None], None]
HYPOTHETICAL = (
    "HYPOTHETICAL RESEARCH: simulated backtests on historical data. No parameter is "
    "adopted automatically, and no result predicts future returns."
)


def _noop(_msg: str, _frac: float | None) -> None:
    return None


@dataclass
class ResearchRequest:
    strategies: list[str] | None = None  # None = every enabled non-benchmark candidate
    source: str | None = None
    start: date | None = None
    end: date | None = None
    use_synthetic_history: bool | None = None
    workers: int | None = None
    simulations: int | None = None
    scheme: str | None = None
    out_dir: Path | None = None


@dataclass
class ResearchOutcome:
    request: ResearchRequest
    result: ResearchResult
    report: Path
    start: date
    end: date
    source: str


def apply_overrides(loaded: LoadedConfig, req: ResearchRequest) -> tuple[LoadedConfig, list[str]]:
    s = loaded.settings
    notes: list[str] = []
    research = s.research
    backtest = s.backtest
    if req.workers is not None:
        if req.workers < 1:
            raise ConfigurationError("--workers must be >= 1")
        research = research.model_copy(update={"workers": req.workers})
    if req.simulations is not None:
        mc = type(research.monte_carlo).model_validate(
            {**research.monte_carlo.model_dump(), "simulations": req.simulations}
        )
        research = research.model_copy(update={"monte_carlo": mc})
        notes.append(f"monte_carlo.simulations={req.simulations}")
    if req.scheme is not None:
        wf = type(research.walk_forward).model_validate(
            {**research.walk_forward.model_dump(), "scheme": req.scheme}
        )
        research = research.model_copy(update={"walk_forward": wf})
        notes.append(f"walk_forward.scheme={req.scheme}")
    if req.use_synthetic_history is not None:
        backtest = backtest.model_copy(update={"use_synthetic_history": req.use_synthetic_history})
        notes.append(f"use_synthetic_history={req.use_synthetic_history}")
    settings = s.model_copy(update={"research": research, "backtest": backtest})
    out = (
        [f"command-line overrides of config {loaded.config_version}: {', '.join(notes)}"]
        if notes
        else []
    )
    return dataclasses.replace(loaded, settings=settings), out


def run(
    loaded: LoadedConfig,
    clock: Clock,
    req: ResearchRequest,
    *,
    progress: Progress = _noop,
) -> ResearchOutcome:
    loaded, overrides = apply_overrides(loaded, req)
    s = loaded.settings
    cfg = s.research
    catalog = StrategyCatalog.from_config(s.strategies)
    eligible = {st.strategy_id for st in catalog.eligible(TradingMode.BACKTEST)}
    versions = [e.version for e in catalog.entries if e.version.strategy_id in eligible]
    if req.strategies:
        unknown = [sid for sid in req.strategies if sid not in eligible]
        if unknown:
            raise ConfigurationError(
                f"not available for research: {unknown}",
                hint="use ids from the strategy catalogue (disabled strategies cannot run)",
            )
        versions = [v for v in versions if v.strategy_id in req.strategies]
    else:
        versions = research_candidates(versions)
    if not versions:
        raise ConfigurationError("no strategies to research")

    # every valid grid point decides the common warm-up and data needs
    grid_strategies: list[Strategy] = []
    for v in versions:
        grid = ParamGrid.build(v.params, v.param_grid, cfg.max_grid_points)
        for c in grid.coords():
            try:
                spec = TrialSpec(v.strategy_id, v.implementation, grid.params(c), c)
                grid_strategies.append(spec.build())
            except StrategyError:
                continue  # invalid combination: reported by the pipeline
    source = req.source or s.data.primary_provider
    progress(f"loading {source} price history", 0.02)
    required = sorted({*TRADEABLE, *(st.signal_symbol for st in grid_strategies)})
    optional = sorted(
        {*s.universe.benchmarks, *(o for st in grid_strategies for o in st.optional_symbols)}
    )
    data = load_backtest_data(
        build_store(loaded, clock),
        source,
        required,
        optional,
        use_synthetic=s.backtest.use_synthetic_history,
        synthetic_symbols=s.data.synthetic.products.keys(),
    )
    calendar = nyse_calendar()
    start, end = default_range(
        data, grid_strategies, calendar, req.start, req.end, risk_warmup_bars(s)
    )
    ctx = TrialContext(
        frames=data.frames,
        synthetic=data.synthetic,
        instruments=s.universe.by_symbol,
        calendar=calendar,
        settings=engine_settings(s),
        start=start,
        end=end,
        data_fingerprint=data_fingerprint(data.frames),
    )
    now = clock.now()
    stamp = f"{now.astimezone(MARKET_TZ):%Y%m%d-%H%M%S}"
    name = re.sub(r"[^a-z0-9_+-]", "", "+".join(req.strategies or ["all"]))[:60]
    out = req.out_dir or loaded.resolve_path(cfg.output_dir) / f"{stamp}-{name}"
    result = run_research(
        versions,
        ctx,
        cfg,
        TrialRegistry(loaded.resolve_path(cfg.registry_path)),
        GovernanceLedger(loaded.resolve_path(cfg.governance_ledger)),
        now=now,
        config_version=loaded.config_version,
        report_path=str(out / "report.html"),
        progress=lambda msg: progress(msg, None),
    )
    result.notes[:0] = [*overrides, *(f"data {k}: {v}" for k, v in data.provenance.items())]
    if data.missing_optional:
        result.notes.append(f"optional data unavailable: {', '.join(data.missing_optional)}")
    report = write_report(
        result, out, f"Research: {' + '.join(req.strategies or ['all candidates'])}"
    )
    progress("research finished", 1.0)
    return ResearchOutcome(req, result, report, start, end, source)


def _f(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def summarize(outcome: ResearchOutcome) -> dict[str, Any]:
    """Bounded JSON scorecards for PostgreSQL."""
    r = outcome.result
    ranked = []
    for k, sc in enumerate(r.ranked, 1):
        e = sc.evidence
        ranked.append(
            {
                "rank": k,
                "strategy_id": e.strategy_id,
                "version_id": e.version_id,
                "params": e.params_label,
                "score": _f(sc.score),
                "passed": sc.passed,
                "oos_sharpe": _f(e.oos_sharpe),
                "oos_sortino": _f(e.oos_sortino),
                "oos_max_drawdown": _f(e.oos_max_drawdown),
                "oos_calmar": _f(e.oos_calmar),
                "oos_sessions": e.oos_sessions,
                "oos_synthetic_sessions": e.oos_synthetic_sessions,
                "robustness": _f(e.robustness_score),
                "potentially_overfit": e.potentially_overfit,
                "dsr": _f(e.dsr),
                "pbo": _f(e.pbo),
                "p_value": _f(e.sharpe_p_value),
                "bh_adjusted_p": _f(e.bh_adjusted_p),
                "oos_benchmark_sharpe": _f(e.oos_benchmark_sharpe),
                "gates": [
                    {
                        "name": g.name,
                        "passed": g.passed,
                        "value": g.value if isinstance(g.value, bool) else _f(g.value),
                        "threshold": g.threshold
                        if isinstance(g.threshold, bool)
                        else _f(g.threshold),
                        "rule": g.rule,
                    }
                    for g in sc.gates
                ],
            }
        )
    transitions = [
        {
            "strategy_id": st.transition.strategy_id,
            "from": st.transition.from_state.value,
            "to": st.transition.to_state.value,
            "reason": st.transition.reason,
            "kind": "automated (governance ledger); strategies.yaml is unchanged",
        }
        for st in r.strategies
        if st.transition is not None
    ]
    rc = r.reality_check
    return {
        "disclaimer": HYPOTHETICAL,
        "run_id": r.run_id,
        "config_version": r.config_version,
        "data_fingerprint": r.data_fingerprint,
        "source": outcome.source,
        "period": {"start": outcome.start.isoformat(), "end": outcome.end.isoformat()},
        "sessions": r.sessions,
        "synthetic_sessions": r.synthetic_sessions,
        "trials_this_run": r.trials_this_run,
        "trials_registered": r.trials_registered,
        "ranked": ranked,
        "errors": {st.strategy_id: st.error for st in r.strategies if st.error},
        "governance_transitions": transitions,
        "reality_check": None
        if rc is None
        else {
            "best": rc.best,
            "reality_check_p": _f(rc.reality_check_p),
            "spa_p": _f(rc.spa_p),
            "models": rc.n_models,
        },
        "global_pbo": None if r.global_pbo is None else _f(getattr(r.global_pbo, "pbo", None)),
        "notes": list(r.notes),
        "promotion": "No strategy is promoted to paper, shadow or live by a research run.",
    }


def research_versions(loaded: LoadedConfig) -> Sequence[str]:
    """Default research candidates (enabled, non-benchmark)."""
    catalog = StrategyCatalog.from_config(loaded.settings.strategies)
    eligible = {st.strategy_id for st in catalog.eligible(TradingMode.BACKTEST)}
    versions = [e.version for e in catalog.entries if e.version.strategy_id in eligible]
    return [v.strategy_id for v in research_candidates(versions)]
