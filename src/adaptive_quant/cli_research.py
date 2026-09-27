"""``aq research`` - robustness research over the configured strategies (Milestone 6).

Research only: no broker, no orders. Parameters are never adopted and
``strategies.yaml`` is never edited; the only automated lifecycle change is
RESEARCH -> VALIDATED (or back), recorded in the governance ledger.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from pathlib import Path

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.core.clock import Clock
from adaptive_quant.governance.research import GovernanceLedger
from adaptive_quant.quant.research.trials import TrialRegistry
from adaptive_quant.services import research

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
    print(
        "HYPOTHETICAL RESEARCH - simulated backtests; "
        "no parameter is adopted, no performance claim is made."
    )
    outcome = research.run(
        loaded,
        clock,
        research.ResearchRequest(
            strategies=args.strategies,
            source=args.source,
            start=args.start,
            end=args.end,
            use_synthetic_history=args.synthetic,
            workers=args.workers,
            simulations=args.simulations,
            scheme=args.scheme,
            out_dir=loaded.resolve_path(Path(args.out)) if args.out else None,
        ),
        progress=lambda msg, _f: print(f"  {msg}"),
    )
    result = outcome.result
    print(
        f"period {outcome.start} .. {outcome.end}; {result.trials_this_run} trials this run, "
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
    print(f"report: {outcome.report}")
    return EXIT_OK


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
