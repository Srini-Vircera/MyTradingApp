"""``aq backtest run`` - hypothetical backtests of configured strategies.

Research only: no broker, no orders, no parameter optimisation.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
from datetime import date
from pathlib import Path

import pandas as pd

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.core.clock import MARKET_TZ, Clock
from adaptive_quant.core.enums import TradingMode
from adaptive_quant.core.errors import ConfigurationError, DataQualityError
from adaptive_quant.quant.analytics.report import write_report
from adaptive_quant.quant.backtest.data import BacktestData, load_backtest_data
from adaptive_quant.quant.backtest.execution import ExecutionTiming
from adaptive_quant.quant.backtest.runner import risk_warmup_bars, run_backtest
from adaptive_quant.quant.data.calendar import TradingCalendar, nyse_calendar
from adaptive_quant.quant.data.factory import build_store
from adaptive_quant.quant.strategies.base import Strategy
from adaptive_quant.quant.strategies.catalog import StrategyCatalog

EXIT_OK = 0
EXIT_REFUSED = 2
TRADEABLE = ("QQQ", "TQQQ", "SQQQ")


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
    loaded, overrides = _apply_overrides(loaded, args)
    settings = loaded.settings
    catalog = StrategyCatalog.from_config(settings.strategies)
    eligible = {s.strategy_id: s for s in catalog.eligible(TradingMode.BACKTEST)}
    unknown = [sid for sid in args.strategies if sid not in eligible]
    if unknown:
        raise ConfigurationError(
            f"not available for backtesting: {unknown}",
            hint="use ids from `aq strategies list` (disabled strategies cannot run)",
        )
    strategies = [eligible[sid] for sid in args.strategies]
    source = args.source or settings.data.primary_provider
    required = sorted({*TRADEABLE, *(s.signal_symbol for s in strategies)})
    optional = sorted(
        {*settings.universe.benchmarks, *(o for s in strategies for o in s.optional_symbols)}
    )
    data = load_backtest_data(
        build_store(loaded, clock),
        source,
        required,
        optional,
        use_synthetic=settings.backtest.use_synthetic_history,
        synthetic_symbols=settings.data.synthetic.products.keys(),
    )
    calendar = nyse_calendar()
    start, end = default_range(
        data, strategies, calendar, args.start, args.end, risk_warmup_bars(settings)
    )
    analysed = run_backtest(loaded, strategies, data, calendar, start, end)
    analysed.notes[:0] = overrides
    stamp = f"{clock.now().astimezone(MARKET_TZ):%Y%m%d-%H%M%S}"
    name = re.sub(r"[^a-z0-9_+-]", "", "+".join(args.strategies))[:80]
    if args.out:
        out = loaded.resolve_path(Path(args.out))
    else:
        out = loaded.resolve_path(settings.backtest.report_dir) / f"{stamp}-{name}"
    title = f"Backtest: {' + '.join(args.strategies)}"
    report = write_report(analysed, out, title)

    m = analysed.metrics["all"]
    print("HYPOTHETICAL BACKTEST - simulated fills and costs; no performance claim is made.")
    if "synthetic" in analysed.segments:
        print(
            "Includes SYNTHETIC price history; real and synthetic metrics are reported separately."
        )
    print(
        f"period {start} .. {end}, execution {settings.backtest.execution} "
        f"(+{settings.backtest.execution_delay_bars} bars), source {source}"
    )
    print(f"{'':<22} {'CAGR':>8} {'MaxDD':>8} {'Sharpe':>7} {'Vol':>7}")
    for series_name, vals in m.items():
        print(
            f"{series_name[:22]:<22} {_p(vals.get('cagr')):>8} {_p(vals.get('max_drawdown')):>8} "
            f"{_r(vals.get('sharpe')):>7} {_p(vals.get('volatility')):>7}"
        )
    for note in analysed.notes:
        print(f"note: {note}")
    print(f"report: {report}")
    return EXIT_OK


def _apply_overrides(
    loaded: LoadedConfig, args: argparse.Namespace
) -> tuple[LoadedConfig, list[str]]:
    bt = loaded.settings.backtest
    update: dict[str, object] = {}
    if args.execution is not None:
        update["execution"] = args.execution
    if args.delay is not None:
        if args.delay < 0:
            raise ConfigurationError("--delay must be >= 0")
        update["execution_delay_bars"] = args.delay
    if args.synthetic is not None:
        update["use_synthetic_history"] = args.synthetic
    if not update:
        return loaded, []
    new_bt = type(bt).model_validate({**bt.model_dump(), **update})
    settings = loaded.settings.model_copy(update={"backtest": new_bt})
    note = (
        "command-line overrides of config "
        + loaded.config_version
        + ": "
        + ", ".join(f"{k}={v}" for k, v in update.items())
    )
    return dataclasses.replace(loaded, settings=settings), [note]


def default_range(
    data: BacktestData,
    strategies: list[Strategy],
    calendar: TradingCalendar,
    start: date | None,
    end: date | None,
    min_history_bars: int = 0,
) -> tuple[date, date]:
    tradeable = [pd.DatetimeIndex(data.frames[s].index) for s in TRADEABLE]
    # the first decision needs a prior known close of every instrument (sizing prices)
    common_first = max(ix[1] for ix in tradeable)
    common_last = min(ix[-1] for ix in tradeable)
    first_ready = common_first
    for s in strategies:
        idx = pd.DatetimeIndex(data.frames[s.signal_symbol].index)
        if len(idx) <= s.warmup_bars:
            raise DataQualityError(
                f"{s.strategy_id} needs {s.warmup_bars} bars; only {len(idx)} stored"
            )
        first_ready = max(first_ready, idx[s.warmup_bars])  # warm-up complete *before* this session
    if min_history_bars:
        underlying = pd.DatetimeIndex(data.frames["QQQ"].index)
        if len(underlying) <= min_history_bars:
            raise DataQualityError(
                f"the risk engine needs {min_history_bars} QQQ bars; only {len(underlying)} stored"
            )
        first_ready = max(first_ready, underlying[min_history_bars])
    auto_start = first_ready.tz_convert(MARKET_TZ).date()
    auto_end = common_last.tz_convert(MARKET_TZ).date()
    s_, e_ = start or auto_start, end or auto_end
    if s_ >= e_:
        raise DataQualityError(f"empty backtest range {s_} .. {e_}")
    if not calendar.is_session(s_):
        s_ = calendar.next_session(s_).date
    return s_, e_


def _p(v: float | int | None) -> str:
    return "-" if v is None else f"{v:+.1%}"


def _r(v: float | int | None) -> str:
    return "-" if v is None else f"{v:.2f}"
