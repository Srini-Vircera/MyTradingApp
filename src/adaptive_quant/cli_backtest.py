"""``aq backtest run`` - hypothetical backtests of configured strategies.

Research only: no broker, no orders, no parameter optimisation.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import ConfigurationError
from adaptive_quant.quant.backtest.execution import ExecutionTiming
from adaptive_quant.services import backtests
from adaptive_quant.services.backtests import TRADEABLE, default_range

__all__ = ["TRADEABLE", "default_range", "register", "run"]

EXIT_OK = 0
EXIT_REFUSED = 2


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    bt = sub.add_parser("backtest", help="hypothetical backtests (research only)")
    b = bt.add_subparsers(dest="action", required=True)
    run = b.add_parser("run", help="backtest one or more configured strategies")
    run.add_argument(
        "--strategy",
        action="append",
        required=True,
        dest="strategies",
        help="strategy id from strategies.yaml (repeatable; several = naive equal weight)",
    )
    run.add_argument("--source", help="stored data source (default: data.primary_provider)")
    run.add_argument(
        "--start", type=date.fromisoformat, help="default: first date with enough history"
    )
    run.add_argument("--end", type=date.fromisoformat, help="default: last common date")
    run.add_argument("--execution", choices=[e.value for e in ExecutionTiming])
    run.add_argument("--delay", type=int, help="extra sessions between decision and fill")
    syn = run.add_mutually_exclusive_group()
    syn.add_argument(
        "--synthetic",
        dest="synthetic",
        action="store_true",
        default=None,
        help="extend TQQQ/SQQQ with SYNTHETIC pre-inception history",
    )
    syn.add_argument("--no-synthetic", dest="synthetic", action="store_false")
    run.add_argument(
        "--out", help="output directory (default: backtest.report_dir/<timestamp>-<ids>)"
    )


def run(args: argparse.Namespace, loaded: LoadedConfig, clock: Clock) -> int:
    if args.delay is not None and args.delay < 0:
        raise ConfigurationError("--delay must be >= 0")
    req = backtests.BacktestRequest(
        strategies=list(args.strategies),
        source=args.source,
        start=args.start,
        end=args.end,
        execution=args.execution,
        execution_delay_bars=args.delay,
        use_synthetic_history=args.synthetic,
        out_dir=loaded.resolve_path(Path(args.out)) if args.out else None,
    )
    outcome = backtests.run(loaded, clock, req)
    analysed = outcome.analysed
    bt = analysed.metadata
    m = analysed.metrics["all"]
    print("HYPOTHETICAL BACKTEST - simulated fills and costs; no performance claim is made.")
    if "synthetic" in analysed.segments:
        print(
            "Includes SYNTHETIC price history; real and synthetic metrics are reported separately."
        )
    print(
        f"period {outcome.start} .. {outcome.end}, execution {bt['execution']} "
        f"(+{bt['execution_delay_bars']} bars), source {outcome.source}"
    )
    print(f"{'':<22} {'CAGR':>8} {'MaxDD':>8} {'Sharpe':>7} {'Vol':>7}")
    for series_name, vals in m.items():
        print(
            f"{series_name[:22]:<22} {_p(vals.get('cagr')):>8} {_p(vals.get('max_drawdown')):>8} "
            f"{_r(vals.get('sharpe')):>7} {_p(vals.get('volatility')):>7}"
        )
    for note in analysed.notes:
        print(f"note: {note}")
    print(f"report: {outcome.report}")
    return EXIT_OK


def _p(v: float | int | None) -> str:
    return "-" if v is None else f"{v:+.1%}"


def _r(v: float | int | None) -> str:
    return "-" if v is None else f"{v:.2f}"
