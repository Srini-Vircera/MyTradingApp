"""``aq research`` - robustness research over the configured strategies (Milestone 6).

Research only: no broker, no orders. Parameters are never adopted and
``strategies.yaml`` is never edited; the only automated lifecycle change is
RESEARCH -> VALIDATED (or back), recorded in the governance ledger.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
from collections import Counter
from datetime import date
from pathlib import Path

from adaptive_quant.cli_backtest import TRADEABLE, default_range
from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.core.clock import MARKET_TZ, Clock
from adaptive_quant.core.enums import TradingMode
from adaptive_quant.core.errors import ConfigurationError, StrategyError
from adaptive_quant.governance.research import GovernanceLedger
from adaptive_quant.quant.backtest.data import load_backtest_data
from adaptive_quant.quant.backtest.runner import engine_settings
from adaptive_quant.quant.data.calendar import nyse_calendar
from adaptive_quant.quant.data.factory import build_store
from adaptive_quant.quant.research.pipeline import research_candidates, run_research
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

EXIT_OK = 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    rs = sub.add_parser("research", help="robustness research: sweeps, walk-forward, Monte Carlo")
    r = rs.add_subparsers(dest="action", required=True)
    run_p = r.add_parser("run", help="research configured strategies (default: every candidate)")
    run_p.add_argument(
        "--strategy",
        action="append",
        dest="strategies",
        help="strategy id (repeatable; default: all enabled non-benchmark strategies)",
    )
    run_p.add_argument("--source", help="stored data source (default: data.primary_provider)")
    run_p.add_argument(
        "--start", type=date.fromisoformat, help="default: first date every trial is warm"
    )
    run_p.add_argument("--end", type=date.fromisoformat, help="default: last common date")
    syn = run_p.add_mutually_exclusive_group()
    syn.add_argument("--synthetic", dest="synthetic", action="store_true", default=None)
    syn.add_argument("--no-synthetic", dest="synthetic", action="store_false")
    run_p.add_argument("--workers", type=int, help="parallel trial processes")
    run_p.add_argument("--simulations", type=int, help="Monte Carlo resamples per dimension")
    run_p.add_argument("--scheme", choices=["rolling", "anchored"], help="walk-forward scheme")
    run_p.add_argument("--out", help="output directory (default: research.output_dir/<run>)")
    r.add_parser("trials", help="summarise the trial registry")
    r.add_parser("status", help="latest research evidence and automated lifecycle changes")


def run(args: argparse.Namespace, loaded: LoadedConfig, clock: Clock) -> int:
    if args.action == "trials":
        return _trials(loaded)
    if args.action == "status":
        return _status(loaded)
    return _run(args, loaded, clock)


def _run(args: argparse.Namespace, loaded: LoadedConfig, clock: Clock) -> int:
    loaded, overrides = _apply_overrides(loaded, args)
    s = loaded.settings
    cfg = s.research
    catalog = StrategyCatalog.from_config(s.strategies)
    eligible = {st.strategy_id for st in catalog.eligible(TradingMode.BACKTEST)}
    versions = [e.version for e in catalog.entries if e.version.strategy_id in eligible]
    if args.strategies:
        unknown = [sid for sid in args.strategies if sid not in eligible]
        if unknown:
            raise ConfigurationError(
                f"not available for research: {unknown}",
                hint="use ids from `aq strategies list` (disabled strategies cannot run)",
            )
        versions = [v for v in versions if v.strategy_id in args.strategies]
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
    source = args.source or s.data.primary_provider
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
        data,
        grid_strategies,
        calendar,
        args.start,
        args.end,
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
    name = re.sub(r"[^a-z0-9_+-]", "", "+".join(args.strategies or ["all"]))[:60]
    out = (
        loaded.resolve_path(Path(args.out))
        if args.out
        else loaded.resolve_path(cfg.output_dir) / f"{stamp}-{name}"
    )
    print(
        "HYPOTHETICAL RESEARCH - simulated backtests; "
        "no parameter is adopted, no performance claim is made."
    )
    result = run_research(
        versions,
        ctx,
        cfg,
        TrialRegistry(loaded.resolve_path(cfg.registry_path)),
        GovernanceLedger(loaded.resolve_path(cfg.governance_ledger)),
        now=now,
        config_version=loaded.config_version,
        report_path=str(out / "report.html"),
        progress=lambda msg: print(f"  {msg}"),
    )
    result.notes[:0] = [*overrides, *(f"data {k}: {v}" for k, v in data.provenance.items())]
    if data.missing_optional:
        result.notes.append(f"optional data unavailable: {', '.join(data.missing_optional)}")
    report = write_report(
        result, out, f"Research: {' + '.join(args.strategies or ['all candidates'])}"
    )

    print(
        f"period {start} .. {end}; {result.trials_this_run} trials this run, "
        f"{result.trials_registered} distinct trials on this data"
    )
    if result.synthetic_sessions:
        print("Includes SYNTHETIC history: the real-data gate fails for affected OOS periods.")
    print(
        f"{'rank':<5}{'strategy':<26}{'score':>6} {'OOS Sharpe':>10} {'DSR':>6} {'PBO':>6}  gates"
    )
    for k, sc in enumerate(result.ranked, 1):
        e = sc.evidence
        failed = [g.name for g in sc.gates if not g.passed]
        print(
            f"{k:<5}{e.strategy_id[:25]:<26}{sc.score:>6.2f} {_r(e.oos_sharpe):>10} {_r(e.dsr):>6} "
            f"{_r(e.pbo):>6}  {'pass' if sc.passed else 'FAIL: ' + ', '.join(failed)}"
        )
    for st in result.strategies:
        if st.transition is not None:
            t = st.transition
            print(
                f"governance: {t.strategy_id} {t.from_state} -> {t.to_state} "
                "(automated; recorded in the ledger)"
            )
    if result.reality_check:
        print(
            f"Reality Check p={result.reality_check.reality_check_p:.3f}, "
            f"SPA p={result.reality_check.spa_p:.3f} (all trials vs QQQ buy-and-hold)"
        )
    for note in result.notes:
        print(f"note: {note}")
    print(f"report: {report}")
    return EXIT_OK


def _apply_overrides(
    loaded: LoadedConfig, args: argparse.Namespace
) -> tuple[LoadedConfig, list[str]]:
    s = loaded.settings
    notes: list[str] = []
    research = s.research
    backtest = s.backtest
    if args.workers is not None:
        research = research.model_copy(update={"workers": args.workers})
    if args.simulations is not None:
        mc = type(research.monte_carlo).model_validate(
            {**research.monte_carlo.model_dump(), "simulations": args.simulations}
        )
        research = research.model_copy(update={"monte_carlo": mc})
        notes.append(f"monte_carlo.simulations={args.simulations}")
    if args.scheme is not None:
        wf = research.walk_forward.model_copy(update={"scheme": args.scheme})
        research = research.model_copy(update={"walk_forward": wf})
        notes.append(f"walk_forward.scheme={args.scheme}")
    if args.synthetic is not None:
        backtest = backtest.model_copy(update={"use_synthetic_history": args.synthetic})
        notes.append(f"use_synthetic_history={args.synthetic}")
    if research.workers < 1:
        raise ConfigurationError("--workers must be >= 1")
    settings = s.model_copy(update={"research": research, "backtest": backtest})
    out = (
        [f"command-line overrides of config {loaded.config_version}: {', '.join(notes)}"]
        if notes
        else []
    )
    return dataclasses.replace(loaded, settings=settings), out


def _trials(loaded: LoadedConfig) -> int:
    reg = TrialRegistry(loaded.resolve_path(loaded.settings.research.registry_path))
    records = reg.records()
    if not records:
        print("trial registry is empty")
        return EXIT_OK
    by_data = Counter(r.data_fingerprint for r in records)
    print(f"{len(records)} records, {reg.distinct_trials()} distinct successful trials")
    for fp, n in by_data.most_common():
        print(
            f"data {fp}: {n} records, {reg.distinct_trials(fp, purpose='grid')} "
            "distinct grid trials (the Deflated Sharpe N for this data)"
        )
    per_strategy = Counter(r.strategy_id for r in records if r.purpose == "grid")
    for sid, n in sorted(per_strategy.items()):
        print(f"  {sid:<28} {n} grid records")
    return EXIT_OK


def _status(loaded: LoadedConfig) -> int:
    ledger = GovernanceLedger(loaded.resolve_path(loaded.settings.research.governance_ledger))
    entries = ledger.entries()
    if not entries:
        print("no research evidence recorded yet")
        return EXIT_OK
    latest: dict[str, dict[str, object]] = {}
    for e in entries:
        rec = e["record"]
        if isinstance(rec, dict):
            latest[str(rec["strategy_id"])] = rec
    validated = ledger.validated_versions()
    for sid, rec in sorted(latest.items()):
        state = "VALIDATED (automated)" if sid in validated else "research"
        print(
            f"{sid:<28} run {rec['run_id']}  "
            f"gates {'pass' if rec['all_gates_passed'] else 'FAIL'}  rank {rec['rank']}  -> {state}"
        )
    print(
        "Lifecycle in strategies.yaml is unchanged; "
        "paper/shadow/live promotion needs a human approval."
    )
    return EXIT_OK


def _r(v: float) -> str:
    return "-" if v != v else f"{v:.2f}"
