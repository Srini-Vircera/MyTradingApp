"""``aq strategies ...`` commands: list, validate, signals.

Research tooling only: nothing here creates, sizes or submits orders.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, time

import pandas as pd

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.core.clock import MARKET_TZ, Clock, ensure_utc
from adaptive_quant.core.enums import TradingMode
from adaptive_quant.core.errors import MissingDataError
from adaptive_quant.quant.data.bars import Adjustment, Frequency, SeriesKey
from adaptive_quant.quant.data.calendar import nyse_calendar
from adaptive_quant.quant.data.factory import build_store
from adaptive_quant.quant.data.store import ParquetBarStore
from adaptive_quant.quant.data.view import MarketDataView
from adaptive_quant.quant.strategies.catalog import StrategyCatalog
from adaptive_quant.quant.strategies.runner import run_strategies

EXIT_OK = 0
EXIT_REFUSED = 2
SIGNAL_ADJUSTMENT = Adjustment.ALL  # signals use total-return adjusted history
_RESEARCH_MODES = [m.value for m in TradingMode if not m.uses_real_money]


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    st = sub.add_parser("strategies", help="inspect strategies and compute research signals")
    s = st.add_subparsers(dest="action", required=True)
    s.add_parser("list", help="configured strategies, versions, lifecycle and warm-up")
    s.add_parser("validate", help="validate config/strategies.yaml against the registry")
    sig = s.add_parser("signals", help="compute signals from stored data (research only)")
    sig.add_argument("--source", help="stored data source (default: data.primary_provider)")
    sig.add_argument(
        "--as-of",
        help="YYYY-MM-DD (after that session's close) or ISO datetime with offset; default: now",
    )
    sig.add_argument(
        "--mode",
        choices=_RESEARCH_MODES,
        default=TradingMode.BACKTEST.value,
        help="which lifecycle states to include (governance eligibility); never submits",
    )


def run(args: argparse.Namespace, loaded: LoadedConfig, clock: Clock) -> int:
    catalog = StrategyCatalog.from_config(loaded.settings.strategies)
    if args.action == "validate":
        print(f"OK  {len(catalog)} strategies valid (config {loaded.config_version})")
        for mode in TradingMode:
            print(f"    eligible in {mode.value:<8}: {len(catalog.eligible(mode))}")
        return EXIT_OK
    if args.action == "list":
        return _list(catalog)
    if args.action == "signals":
        return _signals(args, loaded, catalog, clock)
    raise AssertionError(f"unhandled action {args.action}")  # pragma: no cover


def _list(catalog: StrategyCatalog) -> int:
    print(f"{'id':<26} {'family':<25} {'lifecycle':<13} {'warm-up':>7}  version")
    for e in catalog.entries:
        v = e.version
        flag = "" if v.enabled else "  (disabled)"
        print(
            f"{v.strategy_id:<26} {v.family.value:<25} {v.lifecycle.value:<13} "
            f"{v.warmup_bars:>7}  {v.version_id}{flag}"
        )
    return EXIT_OK


def _signals(
    args: argparse.Namespace, loaded: LoadedConfig, catalog: StrategyCatalog, clock: Clock
) -> int:
    source = args.source or loaded.settings.data.primary_provider
    as_of = resolve_as_of(args.as_of, clock)
    strategies = catalog.eligible(TradingMode(args.mode))
    if not strategies:
        print(
            f"no strategies are eligible in mode {args.mode!r} (see lifecycle in strategies.yaml)"
        )
        return EXIT_REFUSED
    required, optional = catalog.symbols
    frames = load_frames(build_store(loaded, clock), source, required, optional)
    batch = run_strategies(strategies, MarketDataView(frames, as_of))

    print(
        f"signals as of {as_of.astimezone(MARKET_TZ):%Y-%m-%d %H:%M %Z} "
        f"(source={source}, mode={args.mode}; RESEARCH OUTPUT - no orders are placed)"
    )
    missing_optional = [s for s in optional if s not in frames]
    if missing_optional:
        print(f"optional data not available: {', '.join(missing_optional)}")
    print(f"{'strategy':<26} {'dir':<8} {'score':>6} {'conf':>5} {'expo':>6}  reason")
    for sig in batch.signals:
        print(
            f"{sig.strategy_name:<26} {sig.direction.value:<8} {sig.normalized_score:>+6.2f} "
            f"{sig.confidence:>5.2f} {sig.suggested_exposure:>+6.2f}  {sig.reason}"
        )
    for sid, msg in {**batch.not_ready, **batch.failures}.items():
        print(f"{sid:<26} NO SIGNAL  {msg}")
    return EXIT_OK if batch.ok else EXIT_REFUSED


def resolve_as_of(text: str | None, clock: Clock) -> datetime:
    if text is None:
        return clock.now()
    if len(text) == 10:
        d = date.fromisoformat(text)
        session = nyse_calendar().session(d)
        if session is not None:
            return session.close
        return datetime.combine(d, time(23, 59), tzinfo=MARKET_TZ).astimezone(UTC)
    return ensure_utc(datetime.fromisoformat(text))


def load_frames(
    store: ParquetBarStore, source: str, required: list[str], optional: list[str]
) -> dict[str, pd.DataFrame]:
    """Validated total-return daily bars; required symbols must exist, optional may not."""
    frames: dict[str, pd.DataFrame] = {}
    for symbol in required + optional:
        key = SeriesKey(source, symbol, Frequency.DAILY, SIGNAL_ADJUSTMENT)
        try:
            frames[symbol] = store.read(key)
        except MissingDataError:
            if symbol in required:
                raise
    return frames
