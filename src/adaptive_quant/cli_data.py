"""``aq data ...`` commands: download, validate, list, synthesize."""

from __future__ import annotations

import argparse
from datetime import date

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import Clock, to_market_time
from adaptive_quant.quant.data.bars import Frequency
from adaptive_quant.quant.data.calendar import TradingCalendar, nyse_calendar
from adaptive_quant.quant.data.factory import PROVIDERS, build_store
from adaptive_quant.services import data as data_service

EXIT_OK = 0
EXIT_REFUSED = 2


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    data = sub.add_parser("data", help="download, validate and inspect market data")
    d = data.add_subparsers(dest="action", required=True)

    dl = d.add_parser("download", help="fetch bars from a provider into the local store")
    dl.add_argument("--provider", choices=PROVIDERS, help="default: data.primary_provider")
    dl.add_argument("--symbols", nargs="+", help="default: every configured instrument")
    dl.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD (default: history_start)")
    dl.add_argument("--end", type=date.fromisoformat, help="default: last completed session")
    _freq_arg(dl)

    val = d.add_parser("validate", help="re-validate stored data and check freshness")
    val.add_argument("--source", help="default: data.primary_provider")
    val.add_argument("--symbols", nargs="+")
    _freq_arg(val)
    val.add_argument(
        "--require-fresh",
        action="store_true",
        help="fail if data is stale (what the trading pre-flight check enforces)",
    )

    d.add_parser("list", help="list stored series")

    syn = d.add_parser("synthesize", help="build synthetic leveraged-ETF history")
    syn.add_argument("--source", help="store source holding the underlying (default: primary)")
    syn.add_argument("--symbols", nargs="+", help="default: all configured products")


def run(args: argparse.Namespace, loaded: LoadedConfig, secrets: Secrets, clock: Clock) -> int:
    calendar = nyse_calendar()
    if args.action == "download":
        return _download(args, loaded, secrets, clock, calendar)
    if args.action == "validate":
        return _validate(args, loaded, clock, calendar)
    if args.action == "list":
        return _list(loaded, clock)
    if args.action == "synthesize":
        return _synthesize(args, loaded, clock)
    raise AssertionError(f"unhandled data action {args.action}")  # pragma: no cover


def default_symbols(loaded: LoadedConfig, provider: str) -> list[str]:
    """Every configured instrument; index data only from providers that carry it."""
    return data_service.default_symbols(loaded, provider)


# ------------------------------------------------------------------ commands
def _download(
    args: argparse.Namespace,
    loaded: LoadedConfig,
    secrets: Secrets,
    clock: Clock,
    calendar: TradingCalendar,
) -> int:
    result = data_service.download(
        loaded,
        secrets,
        clock,
        calendar,
        provider=args.provider,
        symbols=args.symbols,
        frequency=Frequency(args.frequency),
        start=args.start,
        end=args.end,
    )
    for o in result.outcomes:
        print(
            o.summary
            if o.summary.startswith("FAIL ")
            else ("OK   " if o.ok else "FAIL ") + o.summary
        )
    p = result.params
    print(
        f"\n{len(result.outcomes) - result.failures}/{len(result.outcomes)} symbol(s) stored and "
        f"valid (provider={p['provider']}, {p['start']}..{p['end']}, {p['frequency']})"
    )
    return EXIT_OK if result.failures == 0 else EXIT_REFUSED


def _validate(
    args: argparse.Namespace, loaded: LoadedConfig, clock: Clock, calendar: TradingCalendar
) -> int:
    result = data_service.validate(
        loaded,
        clock,
        calendar,
        source=args.source,
        symbols=args.symbols,
        frequency=Frequency(args.frequency),
        require_fresh=args.require_fresh,
    )
    for o in result.outcomes:
        print(o.summary)
        if o.fresh is not None:
            print(f"        freshness: {'fresh' if o.fresh else 'STALE'} - {o.freshness}")
    return EXIT_OK if result.failures == 0 else EXIT_REFUSED


def _list(loaded: LoadedConfig, clock: Clock) -> int:
    store = build_store(loaded, clock)
    keys = store.list_series()
    if not keys:
        print(f"no data stored under {store.root} - run `aq data download`")
        return EXIT_OK
    print(f"{'series':<34} {'rows':>7}  {'first':<10}  {'last':<10}  {'valid':<5}  flags")
    for key in keys:
        info = store.latest(key, require_valid=False)
        if info is None:  # pragma: no cover - list_series only yields populated series
            continue
        first = f"{to_market_time(info.first):%Y-%m-%d}" if info.first else "-"
        last = f"{to_market_time(info.last):%Y-%m-%d}" if info.last else "-"
        flags = "SYNTHETIC" if info.is_synthetic else ""
        print(
            f"{key!s:<34} {info.rows:>7}  {first:<10}  {last:<10}  "
            f"{'yes' if info.validation_passed else 'NO':<5}  {flags}"
        )
    return EXIT_OK


def _synthesize(args: argparse.Namespace, loaded: LoadedConfig, clock: Clock) -> int:
    result = data_service.synthesize(loaded, clock, source=args.source, symbols=args.symbols)
    for o in result.outcomes:
        span = f" {o.first}..{o.last}" if o.first and o.last else ""
        print(o.summary + span)
        for note in o.notes:
            print(f"     {note}")
    return EXIT_OK if result.failures == 0 else EXIT_REFUSED


def _freq_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--frequency",
        default=Frequency.DAILY.value,
        choices=[f.value for f in Frequency],
        help="bar frequency (default: 1d)",
    )
