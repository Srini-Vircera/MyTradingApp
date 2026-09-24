"""``aq data ...`` commands: download, validate, list, synthesize."""

from __future__ import annotations

import argparse
from datetime import date

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import Clock, to_market_time
from adaptive_quant.core.enums import AssetClass
from adaptive_quant.core.errors import AQError, MissingDataError
from adaptive_quant.quant.data.bars import Adjustment, Frequency, SeriesKey
from adaptive_quant.quant.data.calendar import TradingCalendar, nyse_calendar
from adaptive_quant.quant.data.factory import (
    PROVIDERS,
    build_provider,
    build_store,
    build_validator,
)
from adaptive_quant.quant.data.freshness import check_daily_freshness, check_intraday_freshness
from adaptive_quant.quant.data.history import build_synthetic
from adaptive_quant.quant.data.pipeline import DataPipeline

EXIT_OK = 0
EXIT_REFUSED = 2
_BASIS_ORDER = (Adjustment.RAW, Adjustment.ALL, Adjustment.SPLIT)


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
    return [
        i.symbol
        for i in loaded.settings.universe.instruments
        if i.asset_class is not AssetClass.INDEX or provider == "polygon"
    ]


# ------------------------------------------------------------------ commands
def _download(
    args: argparse.Namespace,
    loaded: LoadedConfig,
    secrets: Secrets,
    clock: Clock,
    calendar: TradingCalendar,
) -> int:
    settings = loaded.settings
    provider_name = args.provider or settings.data.primary_provider
    frequency = Frequency(args.frequency)
    last = calendar.last_completed_session(clock.now())
    end = args.end or (last.date if last else clock.now().date())
    start = args.start or settings.data.history_start
    symbols = args.symbols or default_symbols(loaded, provider_name)

    provider = build_provider(provider_name, loaded, secrets, calendar)
    pipeline = DataPipeline(
        provider, build_store(loaded, clock), build_validator(loaded, calendar), clock
    )
    failures = 0
    try:
        for symbol in symbols:
            try:
                result = pipeline.update(symbol, frequency, start, end)
            except AQError as exc:
                failures += 1
                print(f"FAIL {symbol}: {exc}")
                continue
            failures += 0 if result.ok else 1
            print(("OK   " if result.ok else "FAIL ") + result.summary())
    finally:
        provider.close()
    print(
        f"\n{len(symbols) - failures}/{len(symbols)} symbol(s) stored and valid "
        f"(provider={provider_name}, {start}..{end}, {frequency})"
    )
    return EXIT_OK if failures == 0 else EXIT_REFUSED


def _validate(
    args: argparse.Namespace, loaded: LoadedConfig, clock: Clock, calendar: TradingCalendar
) -> int:
    settings = loaded.settings
    source = args.source or settings.data.primary_provider
    frequency = Frequency(args.frequency)
    store = build_store(loaded, clock)
    validator = build_validator(loaded, calendar)
    symbols = args.symbols or default_symbols(loaded, source)
    now = clock.now()
    failures = 0
    for symbol in symbols:
        # validate the basis series as stored: raw when available, else what the source holds
        candidates = [SeriesKey(source, symbol, frequency, adj) for adj in _BASIS_ORDER]
        found = [(k, store.latest(k, require_valid=False)) for k in candidates]
        key, info = next(((k, i) for k, i in found if i is not None), (candidates[0], None))
        if info is None:
            failures += 1
            print(f"MISSING {key}")
            continue
        try:
            bars = store.read_snapshot(info)
        except AQError as exc:
            failures += 1
            print(f"FAIL    {key}: {exc}")
            continue
        actions = store.read_corporate_actions(source, symbol) or []
        report = validator.validate(
            bars,
            frequency=frequency,
            subject=str(key),
            corporate_action_dates={a.ex_date for a in actions},
            as_of=now,
        )
        if frequency is Frequency.DAILY:
            fresh = check_daily_freshness(
                bars,
                subject=symbol,
                as_of=now,
                calendar=calendar,
                max_age_sessions=settings.data.staleness.max_daily_bar_age_sessions,
            )
        else:
            fresh = check_intraday_freshness(
                bars,
                subject=symbol,
                as_of=now,
                calendar=calendar,
                max_age_seconds=settings.data.staleness.max_intraday_bar_age_seconds,
            )
        stale_fail = args.require_fresh and not fresh.fresh
        if not report.ok or stale_fail:
            failures += 1
        status = "OK     " if report.ok else "INVALID"
        print(f"{status} {report.summary()}")
        print(f"        freshness: {'fresh' if fresh.fresh else 'STALE'} - {fresh.detail}")
    return EXIT_OK if failures == 0 else EXIT_REFUSED


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
    settings = loaded.settings
    source = args.source or settings.data.primary_provider
    store = build_store(loaded, clock)
    symbols = args.symbols or sorted(settings.data.synthetic.products)
    failures = 0
    for symbol in symbols:
        try:
            build = build_synthetic(
                store, settings, symbol, source=source, resolve_path=loaded.resolve_path
            )
        except MissingDataError as exc:
            failures += 1
            print(f"FAIL {symbol}: {exc}")
            continue
        snap = build.snapshot
        span = (
            f"{to_market_time(snap.first):%Y-%m-%d}..{to_market_time(snap.last):%Y-%m-%d}"
            if snap.first and snap.last
            else "-"
        )
        print(f"{'OK  ' if build.ok else 'FAIL'} {symbol} SYNTHETIC {snap.rows} rows {span}")
        for name, value in build.assumptions.items():
            print(f"     {name}: {value}")
        failures += 0 if build.ok else 1
    return EXIT_OK if failures == 0 else EXIT_REFUSED


def _freq_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--frequency",
        default=Frequency.DAILY.value,
        choices=[f.value for f in Frequency],
        help="bar frequency (default: 1d)",
    )
