"""Market-data operations shared by ``aq data`` and control-plane jobs.

* :func:`download`, :func:`validate`, :func:`synthesize` orchestrate the existing
  pipeline, validator and synthetic-history builder (no new data logic).
* :func:`inventory` summarises every stored series (for the database-backed
  dataset list the API serves without access to the worker's volume).
* :func:`preview_upload` parses and validates an uploaded CSV **in a private
  temporary directory** with the file provider and bar validator - the same
  code a file import uses - and never trusts a client-supplied path or name.
* :func:`import_upload` (worker only) writes an accepted upload into the
  configured import directory under a server-chosen name, archives the file
  it replaces, and runs the normal file-provider pipeline.
"""

from __future__ import annotations

import csv
import io
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import Clock, to_market_time
from adaptive_quant.core.enums import AssetClass
from adaptive_quant.core.errors import AQError, DataProviderError, MissingDataError
from adaptive_quant.quant.data.bars import Adjustment, Frequency, SeriesKey
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.quant.data.factory import build_provider, build_store, build_validator
from adaptive_quant.quant.data.freshness import (
    FreshnessResult,
    check_daily_freshness,
    check_intraday_freshness,
)
from adaptive_quant.quant.data.history import SYNTHETIC_SOURCE, build_synthetic
from adaptive_quant.quant.data.pipeline import DataPipeline
from adaptive_quant.quant.data.providers.base import BarRequest
from adaptive_quant.quant.data.providers.files import FileDataProvider
from adaptive_quant.quant.data.validation import ValidationReport

#: progress(message, fraction 0..1 or None)
Progress = Callable[[str, float | None], None]

BASIS_ORDER = (Adjustment.RAW, Adjustment.ALL, Adjustment.SPLIT)
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
UPLOAD_KINDS = ("bars", "actions")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_ROWS = 2_000_000
SYNTHETIC_WARNING = (
    "SYNTHETIC: model-generated history, not real prices. Do not treat it as calibrated "
    "real history until it has been checked against real overlapping data."
)
NO_ACTIONS_WARNING = (
    "no corporate-actions data for this symbol: split/total-return series equal the raw "
    "prices, so dividends and any splits are NOT reflected (total return may be understated)"
)


def _noop(_msg: str, _frac: float | None) -> None:
    return None


def _iso(ts: datetime | None) -> str | None:
    return None if ts is None else f"{to_market_time(ts):%Y-%m-%d}"


def default_symbols(loaded: LoadedConfig, provider: str) -> list[str]:
    """Every configured instrument; index data only from providers that carry it."""
    return [
        i.symbol
        for i in loaded.settings.universe.instruments
        if i.asset_class is not AssetClass.INDEX or provider == "polygon"
    ]


def check_symbol(symbol: str) -> str:
    if not SYMBOL_RE.fullmatch(symbol):
        raise DataProviderError(
            f"invalid symbol {symbol!r}", hint="use 1-10 upper-case letters/digits, e.g. QQQ"
        )
    return symbol


# ================================================================== results
@dataclass
class SymbolOutcome:
    symbol: str
    ok: bool
    summary: str
    rows: int = 0
    first: str | None = None
    last: str | None = None
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fresh: bool | None = None
    freshness: str = ""


@dataclass
class DataResult:
    action: str
    params: dict[str, Any]
    outcomes: list[SymbolOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.outcomes) and all(o.ok for o in self.outcomes)

    @property
    def failures(self) -> int:
        return sum(1 for o in self.outcomes if not o.ok)

    def to_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "params": self.params,
            "ok": self.ok,
            "failures": self.failures,
            "outcomes": [asdict(o) for o in self.outcomes],
        }


def _report_outcome(symbol: str, report: ValidationReport, ok: bool, summary: str) -> SymbolOutcome:
    return SymbolOutcome(
        symbol=symbol,
        ok=ok,
        summary=summary,
        rows=report.rows,
        first=_iso(report.first),
        last=_iso(report.last),
        issues=[str(i) for i in report.issues],
    )


# ================================================================== operations
def download(
    loaded: LoadedConfig,
    secrets: Secrets,
    clock: Clock,
    calendar: TradingCalendar,
    *,
    provider: str | None = None,
    symbols: list[str] | None = None,
    frequency: Frequency = Frequency.DAILY,
    start: date | None = None,
    end: date | None = None,
    progress: Progress = _noop,
) -> DataResult:
    settings = loaded.settings
    provider_name = provider or settings.data.primary_provider
    last = calendar.last_completed_session(clock.now())
    end = end or (last.date if last else clock.now().date())
    start = start or settings.data.history_start
    symbols = symbols or default_symbols(loaded, provider_name)
    result = DataResult(
        "download",
        {
            "provider": provider_name,
            "symbols": symbols,
            "frequency": frequency.value,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )
    source = build_provider(provider_name, loaded, secrets, calendar)
    pipeline = DataPipeline(
        source, build_store(loaded, clock), build_validator(loaded, calendar), clock
    )
    try:
        for n, symbol in enumerate(symbols):
            progress(f"downloading {symbol} from {provider_name}", n / max(len(symbols), 1))
            try:
                upd = pipeline.update(check_symbol(symbol), frequency, start, end)
            except AQError as exc:
                result.outcomes.append(SymbolOutcome(symbol, False, f"FAIL {symbol}: {exc}"))
                continue
            out = _report_outcome(symbol, upd.report, upd.ok, upd.summary())
            out.notes = list(upd.notes)
            result.outcomes.append(out)
    finally:
        source.close()
    progress("download finished", 1.0)
    return result


def validate(
    loaded: LoadedConfig,
    clock: Clock,
    calendar: TradingCalendar,
    *,
    source: str | None = None,
    symbols: list[str] | None = None,
    frequency: Frequency = Frequency.DAILY,
    require_fresh: bool = False,
    progress: Progress = _noop,
) -> DataResult:
    settings = loaded.settings
    source = source or settings.data.primary_provider
    store = build_store(loaded, clock)
    validator = build_validator(loaded, calendar)
    symbols = symbols or default_symbols(loaded, source)
    now = clock.now()
    result = DataResult(
        "validate",
        {
            "source": source,
            "symbols": symbols,
            "frequency": frequency.value,
            "require_fresh": require_fresh,
        },
    )
    for n, symbol in enumerate(symbols):
        progress(f"validating {symbol}", n / max(len(symbols), 1))
        # validate the basis series as stored: raw when available, else what the source holds
        candidates = [SeriesKey(source, symbol, frequency, adj) for adj in BASIS_ORDER]
        found = [(k, store.latest(k, require_valid=False)) for k in candidates]
        key, info = next(((k, i) for k, i in found if i is not None), (candidates[0], None))
        if info is None:
            result.outcomes.append(SymbolOutcome(symbol, False, f"MISSING {key}"))
            continue
        try:
            bars = store.read_snapshot(info)
        except AQError as exc:
            result.outcomes.append(SymbolOutcome(symbol, False, f"FAIL    {key}: {exc}"))
            continue
        actions = store.read_corporate_actions(source, symbol) or []
        report = validator.validate(
            bars,
            frequency=frequency,
            subject=str(key),
            corporate_action_dates={a.ex_date for a in actions},
            as_of=now,
        )
        fresh = freshness(loaded, calendar, bars, symbol, frequency, now)
        ok = report.ok and (fresh.fresh or not require_fresh)
        status = "OK     " if report.ok else "INVALID"
        out = _report_outcome(symbol, report, ok, f"{status} {report.summary()}")
        out.fresh, out.freshness = fresh.fresh, fresh.detail
        result.outcomes.append(out)
    progress("validation finished", 1.0)
    return result


def freshness(
    loaded: LoadedConfig,
    calendar: TradingCalendar,
    bars: pd.DataFrame,
    subject: str,
    frequency: Frequency,
    now: datetime,
) -> FreshnessResult:
    staleness = loaded.settings.data.staleness
    if frequency is Frequency.DAILY:
        return check_daily_freshness(
            bars,
            subject=subject,
            as_of=now,
            calendar=calendar,
            max_age_sessions=staleness.max_daily_bar_age_sessions,
        )
    return check_intraday_freshness(
        bars,
        subject=subject,
        as_of=now,
        calendar=calendar,
        max_age_seconds=staleness.max_intraday_bar_age_seconds,
    )


def synthesize(
    loaded: LoadedConfig,
    clock: Clock,
    *,
    source: str | None = None,
    symbols: list[str] | None = None,
    progress: Progress = _noop,
) -> DataResult:
    settings = loaded.settings
    source = source or settings.data.primary_provider
    store = build_store(loaded, clock)
    symbols = symbols or sorted(settings.data.synthetic.products)
    result = DataResult("synthesize", {"source": source, "symbols": symbols})
    for n, symbol in enumerate(symbols):
        progress(f"building SYNTHETIC {symbol}", n / max(len(symbols), 1))
        try:
            build = build_synthetic(
                store, settings, symbol, source=source, resolve_path=loaded.resolve_path
            )
        except (MissingDataError, KeyError) as exc:
            result.outcomes.append(SymbolOutcome(symbol, False, f"FAIL {symbol}: {exc}"))
            continue
        snap = build.snapshot
        out = SymbolOutcome(
            symbol=symbol,
            ok=build.ok,
            summary=f"{'OK  ' if build.ok else 'FAIL'} {symbol} SYNTHETIC {snap.rows} rows",
            rows=snap.rows,
            first=_iso(snap.first),
            last=_iso(snap.last),
            issues=list(snap.issues),
            notes=[f"{k}: {v}" for k, v in build.assumptions.items()] + [SYNTHETIC_WARNING],
        )
        result.outcomes.append(out)
    progress("synthetic build finished", 1.0)
    return result


# ================================================================== inventory
@dataclass
class DatasetRow:
    source: str
    symbol: str
    frequency: str
    adjustment: str
    rows: int
    first: str | None
    last: str | None
    validation_passed: bool
    is_synthetic: bool
    provider: str
    content_hash: str
    snapshot_created_at: str
    issues: list[str]
    warnings: list[str]
    notes: dict[str, str]
    fresh: bool | None
    freshness: str
    corporate_actions: int | None  # None = unavailable


def inventory(
    loaded: LoadedConfig, clock: Clock, calendar: TradingCalendar, progress: Progress = _noop
) -> list[DatasetRow]:
    """Every stored series with validation, freshness and caveats."""
    store = build_store(loaded, clock)
    now = clock.now()
    rows: list[DatasetRow] = []
    for key in store.list_series():
        info = store.latest(key, require_valid=False)
        if info is None:  # pragma: no cover - list_series only yields populated series
            continue
        progress(f"inspecting {key}", None)
        warnings: list[str] = []
        fresh: bool | None = None
        detail = ""
        try:
            bars = store.read_snapshot(info)
            f = freshness(loaded, calendar, bars, key.symbol, key.frequency, now)
            fresh, detail = f.fresh, f.detail
        except AQError as exc:
            warnings.append(f"cannot read snapshot: {exc}")
        actions = (
            None if info.is_synthetic else store.read_corporate_actions(key.source, key.symbol)
        )
        if info.is_synthetic or key.source == SYNTHETIC_SOURCE:
            warnings.append(SYNTHETIC_WARNING)
            tracking = info.notes.get("tracking", "")
            if tracking:
                warnings.append(f"tracking check: {tracking}")
        elif key.frequency is Frequency.DAILY and actions is None:
            warnings.append(NO_ACTIONS_WARNING)
        if not info.validation_passed:
            warnings.append("failed validation: never served to backtests or trading")
        rows.append(
            DatasetRow(
                source=key.source,
                symbol=key.symbol,
                frequency=key.frequency.value,
                adjustment=key.adjustment.value,
                rows=info.rows,
                first=_iso(info.first),
                last=_iso(info.last),
                validation_passed=info.validation_passed,
                is_synthetic=info.is_synthetic,
                provider=info.provider,
                content_hash=info.content_hash,
                snapshot_created_at=info.created_at.isoformat(),
                issues=list(info.issues),
                warnings=warnings,
                notes={k: v for k, v in info.notes.items() if k != "provider_notes"},
                fresh=fresh,
                freshness=detail,
                corporate_actions=None if actions is None else len(actions),
            )
        )
    return rows


# ================================================================== uploads
@dataclass
class UploadPreview:
    symbol: str
    kind: str
    frequency: str
    ok: bool
    rows: int
    first: str | None
    last: str | None
    columns: list[str]
    head: list[dict[str, Any]]
    tail: list[dict[str, Any]]
    issues: list[str]
    warnings: list[str]
    errors: list[str]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def upload_filename(symbol: str, kind: str, frequency: Frequency) -> str:
    """The server-chosen file name inside the import directory (never client-supplied)."""
    check_symbol(symbol)
    if kind == "actions":
        return f"{symbol}_actions.csv"
    if kind != "bars":
        raise DataProviderError(f"unknown upload kind {kind!r}")
    return f"{symbol}.csv" if frequency is Frequency.DAILY else f"{symbol}_{frequency.value}.csv"


def decode_csv(content: bytes) -> str:
    if len(content) > MAX_UPLOAD_BYTES:
        raise DataProviderError(f"upload larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if not content.strip():
        raise DataProviderError("upload is empty")
    if b"\x00" in content:
        raise DataProviderError("upload is not a text CSV file (binary content)")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataProviderError("upload is not UTF-8 text") from exc
    return text


def preview_upload(
    loaded: LoadedConfig,
    calendar: TradingCalendar,
    clock: Clock,
    content: bytes,
    *,
    symbol: str,
    kind: str,
    frequency: Frequency = Frequency.DAILY,
) -> UploadPreview:
    """Parse + validate an uploaded CSV exactly as a file import would, without storing it."""
    errors: list[str] = []
    warnings: list[str] = []
    name = upload_filename(symbol, kind, frequency)
    text = decode_csv(content)
    header = next(csv.reader(io.StringIO(text)), [])
    columns = [c.strip() for c in header]
    line_count = text.count("\n")
    if line_count > MAX_UPLOAD_ROWS:
        raise DataProviderError(f"upload has more than {MAX_UPLOAD_ROWS} rows")
    empty = UploadPreview(
        symbol, kind, frequency.value, False, 0, None, None, columns, [], [], [], [], errors
    )
    adjustment = Adjustment(loaded.settings.data.file_import.adjustment)
    with tempfile.TemporaryDirectory(prefix="aq-upload-") as tmp:
        path = Path(tmp) / name
        path.write_text(text, encoding="utf-8")
        provider = FileDataProvider(Path(tmp), adjustment, calendar)
        if kind == "actions":
            try:
                actions = provider.fetch_corporate_actions(symbol, date.min, date.max)
            except AQError as exc:
                errors.append(str(exc))
                return empty
            sample = [
                {
                    "ex_date": a.ex_date.isoformat(),
                    "type": a.type.value,
                    "ratio": a.ratio,
                    "amount": a.amount,
                }
                for a in actions
            ]
            dates = sorted(a.ex_date for a in actions)
            dupes = len(dates) - len({(a.ex_date, a.type) for a in actions})
            if dupes:
                errors.append(f"{dupes} duplicate corporate action(s)")
            return UploadPreview(
                symbol=symbol,
                kind=kind,
                frequency=frequency.value,
                ok=not errors and bool(actions),
                rows=len(actions),
                first=dates[0].isoformat() if dates else None,
                last=dates[-1].isoformat() if dates else None,
                columns=columns,
                head=sample[:5],
                tail=sample[-5:],
                issues=[],
                warnings=warnings,
                errors=errors or ([] if actions else ["no corporate actions in the file"]),
            )
        known = {"date", "timestamp", "datetime", "time", "t", "open", "o", "high", "h", "low"}
        known |= {"l", "close", "c", "volume", "vol", "v", "vwap", "vw"}
        ignored = [c for c in columns if c.lower() not in known]
        if ignored:
            warnings.append(
                f"columns ignored: {', '.join(ignored)}. Splits and dividends are read only from "
                f"a separate {symbol}_actions.csv (ex_date,type,ratio,amount) upload."
            )
        try:
            bars = provider.fetch_bars(
                BarRequest(symbol, frequency, date.min, date.max, adjustment)
            )
        except AQError as exc:
            errors.append(str(exc))
            return empty
        except (ValueError, KeyError, pd.errors.ParserError) as exc:
            errors.append(f"cannot parse the file: {exc}")
            return empty
    report = build_validator(loaded, calendar).validate(
        bars, frequency=frequency, subject=f"upload:{symbol}:{frequency}", as_of=clock.now()
    )
    errors.extend(str(i) for i in report.errors)
    if adjustment is Adjustment.RAW and kind == "bars" and frequency is Frequency.DAILY:
        warnings.append(
            "prices are treated as RAW (data.file_import.adjustment); without a corporate-actions "
            "file, adjusted series will equal raw prices"
        )
    records = [
        {"time": f"{to_market_time(ts.to_pydatetime()):%Y-%m-%d %H:%M}", **row}
        for ts, row in zip(bars.index, bars.to_dict("records"), strict=True)
    ]
    return UploadPreview(
        symbol=symbol,
        kind=kind,
        frequency=frequency.value,
        ok=report.ok and not errors,
        rows=report.rows,
        first=_iso(report.first),
        last=_iso(report.last),
        columns=columns,
        head=[_plain(r) for r in records[:5]],
        tail=[_plain(r) for r in records[-5:]],
        issues=[str(i) for i in report.issues],
        warnings=warnings,
        errors=errors,
    )


def _plain(record: dict[Any, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in record.items():
        out[str(k)] = v if isinstance(v, str | int) else (None if pd.isna(v) else float(v))
    return out


def import_upload(
    loaded: LoadedConfig,
    secrets: Secrets,
    clock: Clock,
    calendar: TradingCalendar,
    content: bytes,
    *,
    symbol: str,
    kind: str,
    frequency: Frequency = Frequency.DAILY,
    progress: Progress = _noop,
) -> DataResult:
    """Write an accepted upload into the import directory and run the file pipeline."""
    preview = preview_upload(
        loaded, calendar, clock, content, symbol=symbol, kind=kind, frequency=frequency
    )
    if not preview.ok:
        raise DataProviderError(
            "upload failed validation; nothing was imported",
            hint="; ".join(preview.errors[:3]) or "see the upload preview",
        )
    directory = loaded.resolve_path(loaded.settings.data.file_import.directory)
    name = upload_filename(symbol, kind, frequency)
    progress(f"writing {name} to the import directory", 0.1)
    _replace_file(directory, name, decode_csv(content), clock)
    result = DataResult(
        "import_upload", {"symbol": symbol, "kind": kind, "frequency": frequency.value}
    )
    bars_file = upload_filename(symbol, "bars", frequency)
    if kind == "actions" and not (directory / bars_file).exists():
        result.outcomes.append(
            SymbolOutcome(
                symbol,
                True,
                f"stored {name}; upload {bars_file} to import prices with these actions",
            )
        )
        return result
    progress(f"importing {symbol} with the file provider", 0.4)
    last = calendar.last_completed_session(clock.now())
    end = last.date if last else clock.now().date()
    start = loaded.settings.data.history_start
    if kind == "bars" and preview.first:
        start = min(start, date.fromisoformat(preview.first[:10]))
    imported = download(
        loaded,
        secrets,
        clock,
        calendar,
        provider="file",
        symbols=[symbol],
        frequency=frequency,
        start=start,
        end=max(end, date.fromisoformat(preview.last[:10])) if preview.last else end,
    )
    result.outcomes.extend(imported.outcomes)
    progress("import finished", 1.0)
    return result


def _replace_file(directory: Path, name: str, text: str, clock: Clock) -> None:
    """Atomic write; the replaced file is archived, never silently lost."""
    directory.mkdir(parents=True, exist_ok=True)
    target = (directory / name).resolve()
    if target.parent != directory.resolve():  # defence in depth: names are server-built
        raise DataProviderError("refusing to write outside the import directory")
    if target.exists():
        archive = directory / "archive"
        archive.mkdir(exist_ok=True)
        stamp = f"{clock.now():%Y%m%dT%H%M%SZ}"
        target.replace(archive / f"{name}.{stamp}")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".upload-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
