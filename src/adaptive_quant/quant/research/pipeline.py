"""The research pipeline: trials -> robustness -> walk-forward -> statistics -> ranking.

For each candidate strategy:

1. run one trial per ``param_grid`` point (every trial goes into the registry);
2. robustness score over the grid; the *plateau centre* is the full-sample choice;
3. walk-forward selection with stitched out-of-sample (OOS) returns;
4. Deflated Sharpe (all registered trials on this data), PBO over its grid,
   bootstrap confidence intervals, p-value for "Sharpe > 0" on OOS returns;
5. regime/decade consistency of the OOS returns;
6. Monte Carlo (resampling for every candidate, engine re-runs for the top ones).

Across candidates: Benjamini-Hochberg FDR, White's Reality Check / Hansen SPA
of every trial against QQQ buy-and-hold, a global PBO over all trials, the
scorecard, and governance records. Nothing adopts parameters or changes
``strategies.yaml``; automated lifecycle changes stop at ``validated``.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

import numpy as np
import pandas as pd

from adaptive_quant.config.schema import BacktestConfig, CostConfig, ResearchConfig
from adaptive_quant.core.enums import StrategyFamily, StrategyLifecycle
from adaptive_quant.governance.lifecycle import LifecycleTransition
from adaptive_quant.governance.research import (
    GovernanceLedger,
    ResearchRecord,
    proposed_transition,
)
from adaptive_quant.quant.analytics.metrics import performance_summary
from adaptive_quant.quant.backtest.engine import EngineSettings
from adaptive_quant.quant.research import montecarlo as mc
from adaptive_quant.quant.research.regimes import RegimeReport, regime_labels, regime_report
from adaptive_quant.quant.research.robustness import Coord, ParamGrid, RobustnessResult, robustness
from adaptive_quant.quant.research.scorecard import (
    Evidence,
    GateSettings,
    ScoredCandidate,
    diversification,
    score,
)
from adaptive_quant.quant.research.stats import (
    ConfidenceInterval,
    DeflatedSharpe,
    PBOResult,
    RealityCheckResult,
    benjamini_hochberg,
    bootstrap_ci,
    deflated_sharpe_ratio,
    pbo_cscv,
    reality_check,
    sharpe_pvalue,
)
from adaptive_quant.quant.research.trials import (
    TrialContext,
    TrialOutcome,
    TrialRegistry,
    TrialSpec,
    run_trials,
)
from adaptive_quant.quant.research.walkforward import (
    WalkForwardResult,
    WalkForwardSettings,
    walk_forward,
)
from adaptive_quant.quant.strategies.catalog import StrategyVersion

P = 252
BENCHMARK_SYMBOL = "QQQ"
Progress = Callable[[str], None]


@dataclass
class StrategyResearch:
    version: StrategyVersion
    grid: ParamGrid
    outcomes: dict[Coord, TrialOutcome]
    invalid: list[tuple[str, str]]
    sharpe_grid: dict[Coord, float]
    robustness: RobustnessResult | None = None
    selected: Coord | None = None
    walk_forward: WalkForwardResult | None = None
    oos_equity: pd.Series | None = None
    oos_metrics: dict[str, float | int | None] = field(default_factory=dict)
    full_metrics: dict[str, float | int | None] = field(default_factory=dict)
    dsr: DeflatedSharpe | None = None
    pbo: PBOResult | None = None
    sharpe_ci: ConfidenceInterval | None = None
    cagr_ci: ConfidenceInterval | None = None
    max_dd_ci: ConfidenceInterval | None = None
    p_value: float = math.nan
    regimes: RegimeReport | None = None
    monte_carlo: dict[str, dict[str, mc.Distribution]] = field(default_factory=dict)
    scored: ScoredCandidate | None = None
    record: ResearchRecord | None = None
    transition: LifecycleTransition | None = None
    error: str | None = None

    @property
    def strategy_id(self) -> str:
        return self.version.strategy_id

    @property
    def selected_outcome(self) -> TrialOutcome | None:
        return None if self.selected is None else self.outcomes[self.selected]


@dataclass
class ResearchResult:
    run_id: str
    created_at: datetime
    config_version: str
    data_fingerprint: str
    start: str
    end: str
    sessions: int
    initial_capital: float
    strategies: list[StrategyResearch]
    ranked: list[ScoredCandidate]
    trials_this_run: int
    trials_registered: int  # distinct grid trials on this data, all runs (DSR N)
    var_trial_sharpe: float  # per-period, across this run's trials
    reality_check: RealityCheckResult | None
    global_pbo: PBOResult | None
    synthetic_sessions: int
    benchmark_equity: pd.Series | None
    notes: list[str] = field(default_factory=list)


def run_research(
    versions: Sequence[StrategyVersion],
    ctx: TrialContext,
    cfg: ResearchConfig,
    registry: TrialRegistry,
    ledger: GovernanceLedger | None,
    *,
    now: datetime,
    config_version: str,
    report_path: str = "",
    progress: Progress | None = None,
) -> ResearchResult:
    say = progress or (lambda _msg: None)
    run_id = _run_id(now, ctx, versions)
    notes: list[str] = []

    # 1. trials -------------------------------------------------------------
    studies: list[StrategyResearch] = []
    all_specs: list[TrialSpec] = []
    grids: list[ParamGrid] = []
    for v in versions:
        grid = ParamGrid.build(v.params, v.param_grid, cfg.max_grid_points)
        grids.append(grid)
        all_specs += [
            TrialSpec(v.strategy_id, v.implementation, grid.params(c), c, grid.label(c))
            for c in grid.coords()
        ]
    say(f"running {len(all_specs)} trials for {len(versions)} strategies")
    outcomes = run_trials(all_specs, ctx, cfg.workers)
    registry.record(outcomes, ctx, run_id, now, purpose="grid")
    by_sid: dict[str, list[TrialOutcome]] = {}
    for o in outcomes:
        by_sid.setdefault(o.spec.strategy_id, []).append(o)
    for v, grid in zip(versions, grids, strict=True):
        ok = {o.spec.coord: o for o in by_sid.get(v.strategy_id, []) if o.ok}
        invalid = [(o.spec.label, o.error or "") for o in by_sid.get(v.strategy_id, []) if not o.ok]
        study = StrategyResearch(
            version=v,
            grid=grid,
            outcomes=ok,
            invalid=invalid,
            sharpe_grid={c: _annual_sharpe(o.returns) for c, o in ok.items()},
        )
        if not ok:
            study.error = "no valid parameter set produced a backtest"
        studies.append(study)

    good = [o for o in outcomes if o.ok]
    if not good:
        raise ValueError("no trial produced a backtest; nothing to research")
    index = pd.DatetimeIndex(good[0].returns.index)
    for o in good:
        if not pd.DatetimeIndex(o.returns.index).equals(index):
            raise ValueError(
                f"trial {o.spec.label} of {o.spec.strategy_id} has a different session index"
            )
    per_period_sr = [x for o in good if math.isfinite(x := _per_period_sharpe(o.returns))]
    var_sr = float(np.var(per_period_sr, ddof=1)) if len(per_period_sr) > 1 else 0.0
    n_registered = max(registry.distinct_trials(ctx.data_fingerprint, purpose="grid"), len(good))
    synthetic_sessions = int(good[0].synthetic.iloc[1:].sum())
    if synthetic_sessions:
        notes.append(
            f"{synthetic_sessions} sessions use SYNTHETIC leveraged-ETF history; "
            "no strategy can be validated on evidence that includes them"
        )
    labels = regime_labels(ctx.frames[BENCHMARK_SYMBOL]["close"], index)
    wf = WalkForwardSettings(**cfg.walk_forward.model_dump())
    gate_cfg = GateSettings(**cfg.gates.model_dump())
    initial = ctx.settings.config.initial_capital

    # 2-5. per strategy -----------------------------------------------------
    for k, study in enumerate(studies):
        if study.error:
            continue
        say(f"analysing {study.strategy_id}")
        try:
            _analyse(
                study, cfg, wf, n_registered, var_sr, labels, initial, seed=cfg.seed + 7919 * k
            )
        except ValueError as exc:
            study.error = f"analysis failed: {exc}"
    candidates = [s for s in studies if s.error is None]
    for s in studies:
        if s.error:
            notes.append(f"{s.strategy_id}: {s.error}")
    if any(s.walk_forward is not None and not s.walk_forward.folds for s in candidates):
        notes.append("period too short for any walk-forward fold; OOS gates fail")

    # FDR across candidates
    adj = benjamini_hochberg([s.p_value if math.isfinite(s.p_value) else 1.0 for s in candidates])
    evidence = []
    bench_eq = _benchmark_equity(ctx, index, initial)
    bench_r = None if bench_eq is None else bench_eq.pct_change().iloc[1:]
    for s, a in zip(candidates, adj, strict=True):
        e = _evidence(s)
        e.bh_adjusted_p = a
        if bench_r is not None and len(e.oos_returns) >= 2:
            e.oos_benchmark_sharpe = _annual_sharpe(bench_r.reindex(e.oos_returns.index))
        evidence.append(e)
    for sid, dv in diversification(evidence).items():
        next(e for e in evidence if e.strategy_id == sid).diversification = dv
    ranked = score(evidence, cfg.scorecard_weights, gate_cfg) if evidence else []
    for rank, sc in enumerate(ranked, 1):
        study = next(s for s in candidates if s.strategy_id == sc.evidence.strategy_id)
        study.scored = sc
        study.record = _record(study, sc, rank, run_id, now, ctx, config_version, report_path)

    # 6. Monte Carlo -------------------------------------------------------
    top = {sc.evidence.strategy_id for sc in ranked[: cfg.monte_carlo.top_n]}
    for k, study in enumerate(candidates):
        say(f"Monte Carlo {study.strategy_id}")
        study.monte_carlo = _monte_carlo(
            study,
            ctx,
            cfg,
            registry,
            run_id,
            now,
            engine=study.strategy_id in top,
            seed=cfg.seed + 104729 * (k + 1),
        )

    # cross-candidate tests over every trial ---------------------------------
    rc = None
    trial_matrix = pd.DataFrame(
        {f"{o.spec.strategy_id}[{o.spec.label}]": o.returns.to_numpy() for o in good}, index=index
    )
    if bench_eq is not None and len(index) > 10:
        bench_r = bench_eq.pct_change().reindex(index).fillna(0.0)
        rc = reality_check(
            trial_matrix.sub(bench_r, axis=0),
            samples=cfg.bootstrap.samples,
            mean_block=cfg.bootstrap.mean_block_length,
            seed=cfg.seed,
        )
    global_pbo = None
    if trial_matrix.shape[1] >= 2 and len(index) >= cfg.pbo_partitions * 2:
        global_pbo = pbo_cscv(trial_matrix, cfg.pbo_partitions)

    # governance -------------------------------------------------------------
    if ledger is not None:
        validated = ledger.validated_versions()
        for study in candidates:
            if study.record is None:
                continue
            current = study.version.lifecycle
            if (
                current is StrategyLifecycle.RESEARCH
                and validated.get(study.strategy_id) == study.record.version_id
            ):
                current = StrategyLifecycle.VALIDATED
            study.transition = proposed_transition(study.record, current, now)
            ledger.append(study.record, study.transition)

    return ResearchResult(
        run_id=run_id,
        created_at=now,
        config_version=config_version,
        data_fingerprint=ctx.data_fingerprint,
        start=str(ctx.start),
        end=str(ctx.end),
        sessions=len(index) + 1,
        initial_capital=initial,
        strategies=studies,
        ranked=ranked,
        trials_this_run=len(good),
        trials_registered=n_registered,
        var_trial_sharpe=var_sr,
        reality_check=rc,
        global_pbo=global_pbo,
        synthetic_sessions=synthetic_sessions,
        benchmark_equity=bench_eq,
        notes=notes,
    )


# ------------------------------------------------------------------ per strategy
def _analyse(
    s: StrategyResearch,
    cfg: ResearchConfig,
    wf: WalkForwardSettings,
    n_trials: int,
    var_sr: float,
    labels: pd.DataFrame,
    initial: float,
    seed: int,
) -> None:
    s.robustness = robustness(s.grid, s.sharpe_grid, cfg.overfit_threshold)
    s.selected = s.robustness.plateau_centre
    chosen = s.outcomes[s.selected]
    s.full_metrics = performance_summary(
        chosen.equity, exposure=chosen.exposure, turnover=chosen.turnover
    )
    coords = sorted(s.outcomes)
    returns = pd.DataFrame(
        {i: s.outcomes[c].returns.to_numpy() for i, c in enumerate(coords)},
        index=chosen.returns.index,
    )
    s.walk_forward = walk_forward(s.grid, coords, returns, wf)
    oos = s.walk_forward.oos_returns
    s.dsr = deflated_sharpe_ratio(chosen.returns.to_numpy(), n_trials, var_sr)
    if len(coords) >= 2 and len(returns) >= cfg.pbo_partitions * 2:
        s.pbo = pbo_cscv(returns, cfg.pbo_partitions)
    if len(oos) >= 2:
        eq_index = pd.DatetimeIndex(chosen.equity.index)
        prev = eq_index[int(eq_index.searchsorted(oos.index[0])) - 1]
        eq = pd.concat([pd.Series([initial], index=[prev]), initial * (1 + oos).cumprod()])
        s.oos_equity = eq
        s.oos_metrics = performance_summary(eq)
        s.p_value = sharpe_pvalue(oos.to_numpy())
        b = cfg.bootstrap
        x = oos.to_numpy()

        def ci(stat: Callable[[np.ndarray], float]) -> ConfidenceInterval:
            return bootstrap_ci(
                x,
                stat,
                samples=b.samples,
                mean_block=b.mean_block_length,
                confidence=b.confidence,
                seed=seed,
            )

        s.sharpe_ci, s.cagr_ci, s.max_dd_ci = ci(_sharpe_stat), ci(_cagr_stat), ci(_mdd_stat)
        s.regimes = regime_report(oos, labels)


def _evidence(s: StrategyResearch) -> Evidence:
    if s.robustness is None or s.walk_forward is None or s.selected is None:
        raise ValueError(f"{s.strategy_id}: analysis incomplete")
    chosen = s.outcomes[s.selected]
    oos = s.walk_forward.oos_returns
    m = s.oos_metrics
    years = max(len(chosen.returns) / P, 1e-9)
    total_orders = chosen.fills + chosen.cancelled
    return Evidence(
        strategy_id=s.strategy_id,
        version_id=chosen.version_id,
        params_label=s.grid.label(s.selected),
        oos_sharpe=_f(m.get("sharpe")),
        oos_sortino=_f(m.get("sortino")),
        oos_max_drawdown=_f(m.get("max_drawdown")),
        oos_calmar=_f(m.get("calmar")),
        oos_sessions=len(oos),
        oos_synthetic_sessions=int(chosen.synthetic.reindex(oos.index, fill_value=False).sum()),
        robustness_score=s.robustness.score,
        potentially_overfit=s.robustness.potentially_overfit,
        regime_consistency=s.regimes.consistency if s.regimes else math.nan,
        annual_turnover=float(chosen.turnover.sum()) / years,
        execution_feasibility=1.0 - chosen.cancelled / total_orders if total_orders else math.nan,
        dsr=s.dsr.dsr if s.dsr else math.nan,
        pbo=s.pbo.pbo if s.pbo else math.nan,
        sharpe_p_value=s.p_value,
        oos_returns=oos,
    )


def _record(
    s: StrategyResearch,
    sc: ScoredCandidate,
    rank: int,
    run_id: str,
    now: datetime,
    ctx: TrialContext,
    config_version: str,
    report: str,
) -> ResearchRecord:
    e = sc.evidence
    return ResearchRecord(
        strategy_id=s.strategy_id,
        version_id=e.version_id,
        run_id=run_id,
        recorded_at=now.isoformat(),
        data_fingerprint=ctx.data_fingerprint,
        config_version=config_version,
        gates={g.name: g.passed for g in sc.gates},
        all_gates_passed=sc.passed,
        score=round(sc.score, 4),
        rank=rank,
        statistics={
            "oos_sharpe": _j(e.oos_sharpe),
            "oos_max_drawdown": _j(e.oos_max_drawdown),
            "dsr": _j(e.dsr),
            "pbo": _j(e.pbo),
            "robustness": _j(e.robustness_score),
            "bh_adjusted_p": _j(e.bh_adjusted_p),
            "oos_sessions": float(e.oos_sessions),
        },
        report=report,
    )


def _monte_carlo(
    s: StrategyResearch,
    ctx: TrialContext,
    cfg: ResearchConfig,
    registry: TrialRegistry,
    run_id: str,
    now: datetime,
    *,
    engine: bool,
    seed: int,
) -> dict[str, dict[str, mc.Distribution]]:
    if s.selected is None:
        raise ValueError(f"{s.strategy_id}: no selected parameters")
    m = cfg.monte_carlo
    chosen = s.outcomes[s.selected]
    initial = ctx.settings.config.initial_capital
    years = len(chosen.returns) / P
    out: dict[str, dict[str, mc.Distribution]] = {
        "trade_sequence": mc.table(
            mc.trade_sequence(chosen.trade_returns, years, initial, m.simulations, seed)
        ),
        "return_blocks": mc.table(
            mc.return_blocks(
                chosen.returns, initial, m.simulations, cfg.bootstrap.mean_block_length, seed + 1
            )
        ),
        "start_date": mc.table(
            mc.start_dates(
                chosen.returns, initial, m.simulations, m.start_skip_fraction, m.min_years, seed + 2
            )
        ),
        "parameters": mc.table(
            [
                mc.path_metrics(
                    s.outcomes[c].returns.to_numpy(),
                    initial,
                    pd.DatetimeIndex(s.outcomes[c].returns.index),
                )
                for c in [s.selected, *s.grid.neighbours(s.selected)]
                if c in s.outcomes
            ]
        ),
    }
    if not engine:
        return out
    spec = chosen.spec
    rng = np.random.default_rng(seed + 3)
    lo, hi = m.cost_multiplier_range
    runs: list[tuple[str, TrialContext]] = []
    for _ in range(m.cost_simulations):
        runs.append(("costs", ctx.with_settings(_scaled_costs(ctx, float(rng.uniform(lo, hi))))))
    base_delay = ctx.settings.config.execution_delay_bars
    for d in m.delays:
        cfg_d = ctx.settings.config.model_copy(update={"execution_delay_bars": base_delay + d})
        runs.append(("signal_delay", ctx.with_settings(_with_config(ctx, cfg_d))))
    paths: dict[str, list[mc.PathMetrics]] = {"costs": [], "signal_delay": []}
    for dim, c in runs:
        o = run_trials([spec], c, 1)[0]
        registry.record([o], c, run_id, now, purpose=f"monte_carlo:{dim}")
        if o.ok:
            paths[dim].append(
                mc.path_metrics(o.returns.to_numpy(), initial, pd.DatetimeIndex(o.returns.index))
            )
    for dim, p in paths.items():
        if p:
            out[dim] = mc.table(p)
    return out


def _scaled_costs(ctx: TrialContext, k: float) -> EngineSettings:
    c: CostConfig = ctx.settings.config.costs
    scaled = c.model_copy(
        update={
            "half_spread_bps": {sym: min(v * k, 500.0) for sym, v in c.half_spread_bps.items()},
            "slippage_bps": min(c.slippage_bps * k, 500.0),
            "impact_coefficient_bps": min(c.impact_coefficient_bps * k, 5000.0),
            "commission_per_share": c.commission_per_share * k,
            "commission_per_order": c.commission_per_order * k,
            "commission_minimum": c.commission_minimum * k,
        }
    )
    return _with_config(ctx, ctx.settings.config.model_copy(update={"costs": scaled}))


def _with_config(ctx: TrialContext, config: BacktestConfig) -> EngineSettings:
    return replace(ctx.settings, config=config)


def _benchmark_equity(
    ctx: TrialContext, index: pd.DatetimeIndex, initial: float
) -> pd.Series | None:
    """QQQ buy-and-hold (total return, no costs) from the session before the first return."""
    frame = ctx.frames.get(BENCHMARK_SYMBOL)
    if frame is None or len(index) == 0:
        return None
    close = frame["close"].astype(float)
    pos = int(pd.DatetimeIndex(close.index).searchsorted(index[0]))
    if pos <= 0 or pos >= len(close) or close.index[pos] != index[0]:
        return None
    full = pd.DatetimeIndex([close.index[pos - 1], *index])
    window = close.reindex(full)
    if window.isna().any():
        return None
    return initial * window / window.iloc[0]


# ------------------------------------------------------------------ helpers
def _per_period_sharpe(r: pd.Series) -> float:
    sd = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    return float(r.mean()) / sd if sd > 1e-15 else math.nan


def _annual_sharpe(r: pd.Series) -> float:
    v = _per_period_sharpe(r)
    return v * math.sqrt(P) if math.isfinite(v) else math.nan


def _sharpe_stat(x: np.ndarray) -> float:
    sd = float(np.std(x, ddof=1))
    return float(np.mean(x)) / sd * math.sqrt(P) if sd > 1e-15 else math.nan


def _cagr_stat(x: np.ndarray) -> float:
    g = float(np.prod(1 + x))
    return g ** (P / len(x)) - 1 if g > 0 else -1.0


def _mdd_stat(x: np.ndarray) -> float:
    growth = np.cumprod(1 + x)
    peak = np.maximum.accumulate(np.concatenate([[1.0], growth]))[1:]
    return float(min((growth / peak - 1).min(), 0.0))


def _f(v: object) -> float:
    return float(v) if isinstance(v, int | float) and math.isfinite(float(v)) else math.nan


def _j(v: float) -> float | None:
    return round(v, 6) if math.isfinite(v) else None


def _run_id(now: datetime, ctx: TrialContext, versions: Sequence[StrategyVersion]) -> str:
    key = "|".join([now.isoformat(), ctx.data_fingerprint, *sorted(v.version_id for v in versions)])
    return f"{now:%Y%m%dT%H%M%SZ}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def research_candidates(versions: Sequence[StrategyVersion]) -> list[StrategyVersion]:
    """Enabled, non-disabled, non-benchmark strategies (benchmarks are controls, not candidates)."""
    return [
        v
        for v in versions
        if v.enabled
        and v.lifecycle is not StrategyLifecycle.DISABLED
        and v.family is not StrategyFamily.BENCHMARK
    ]
