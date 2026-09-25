"""Versioned, content-addressed Parquet storage for bar series.

Layout::

    <root>/bars/<source>/<frequency>/<adjustment>/<SYMBOL>/<hash16>.parquet
    <root>/bars/<source>/<frequency>/<adjustment>/<SYMBOL>/manifest.json
    <root>/corporate_actions/<source>/<SYMBOL>.json

* **Immutable snapshots.** A snapshot file is named by the SHA-256 of its
  content and never modified. Writing identical data again is a no-op
  (idempotent), so re-running a download is always safe.
* **Manifest = history.** Each series' manifest lists every snapshot ever
  written with provenance (provider, time, rows, range, validation outcome,
  synthetic flag, notes). The latest entry is the current version; older ones
  stay available for audits ("which data did the backtest of March use?").
* **Integrity.** Reads recompute the content hash and refuse corrupted files.
* **Fail closed.** By default only snapshots that passed validation are served.
* Synthetic series are labelled both in the manifest and inside the Parquet
  file's schema metadata, so the label travels with the file.

PostgreSQL will hold a mirror of this metadata in Milestone 8; Parquet remains
the research store.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import DataQualityError, MissingDataError
from adaptive_quant.core.models import UtcDatetime
from adaptive_quant.quant.data.bars import (
    Adjustment,
    Frequency,
    SeriesKey,
    content_hash,
    require_canonical_sorted,
    to_canonical,
)
from adaptive_quant.quant.data.corporate_actions import CorporateAction, dedupe_actions
from adaptive_quant.quant.data.validation import ValidationReport

_META_KEY = b"adaptive_quant"


class SnapshotInfo(BaseModel):
    """Provenance of one stored snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    symbol: str
    frequency: Frequency
    adjustment: Adjustment
    content_hash: str
    file: str
    rows: int
    first: UtcDatetime | None
    last: UtcDatetime | None
    created_at: UtcDatetime
    provider: str
    is_synthetic: bool = False
    validation_passed: bool
    issues: list[str] = Field(default_factory=list)
    notes: dict[str, str] = Field(default_factory=dict)

    @property
    def key(self) -> SeriesKey:
        return SeriesKey(self.source, self.symbol, self.frequency, self.adjustment)


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshots: list[SnapshotInfo] = Field(default_factory=list)


class ParquetBarStore:
    def __init__(self, root: Path, clock: Clock) -> None:
        self.root = root
        self._clock = clock

    # ------------------------------------------------------------ bars
    def write(
        self,
        key: SeriesKey,
        bars: pd.DataFrame,
        *,
        provider: str,
        validation: ValidationReport,
        is_synthetic: bool = False,
        notes: dict[str, str] | None = None,
    ) -> SnapshotInfo:
        """Store ``bars`` as the new current version of ``key`` (no-op if unchanged)."""
        require_canonical_sorted(bars, str(key))
        bars = to_canonical(bars)
        digest = content_hash(bars)
        manifest = self._load_manifest(key)
        latest = manifest.snapshots[-1] if manifest.snapshots else None
        if (
            latest is not None
            and latest.content_hash == digest
            and latest.validation_passed == validation.ok
            and latest.is_synthetic == is_synthetic
        ):
            return latest

        info = SnapshotInfo(
            source=key.source,
            symbol=key.symbol,
            frequency=key.frequency,
            adjustment=key.adjustment,
            content_hash=digest,
            file=f"{digest[:16]}.parquet",
            rows=len(bars),
            first=bars.index[0].to_pydatetime() if len(bars) else None,
            last=bars.index[-1].to_pydatetime() if len(bars) else None,
            created_at=self._clock.now(),
            provider=provider,
            is_synthetic=is_synthetic,
            validation_passed=validation.ok,
            issues=[str(i) for i in validation.issues],
            notes=dict(notes or {}),
        )
        path = self._series_dir(key) / info.file
        if not path.exists():
            self._write_parquet(path, bars, info)
        manifest.snapshots.append(info)
        self._save_manifest(key, manifest)
        return info

    def latest(self, key: SeriesKey, *, require_valid: bool = True) -> SnapshotInfo | None:
        for info in reversed(self._load_manifest(key).snapshots):
            if info.validation_passed or not require_valid:
                return info
        return None

    def history(self, key: SeriesKey) -> list[SnapshotInfo]:
        return list(self._load_manifest(key).snapshots)

    def read(self, key: SeriesKey, *, require_valid: bool = True) -> pd.DataFrame:
        info = self.latest(key, require_valid=require_valid)
        if info is None:
            qualifier = "validated " if require_valid else ""
            raise MissingDataError(
                f"no {qualifier}data stored for {key}",
                hint="run `aq data download` (and check `aq data validate`)",
            )
        return self.read_snapshot(info)

    def read_snapshot(self, info: SnapshotInfo) -> pd.DataFrame:
        path = self._series_dir(info.key) / info.file
        if not path.exists():
            raise MissingDataError(f"snapshot file missing: {path}")
        df = pq.read_table(path).to_pandas()
        df = to_canonical(df)
        if content_hash(df) != info.content_hash:
            raise DataQualityError(
                f"snapshot {path} failed its integrity check (content hash mismatch)",
                hint="the file was modified or corrupted; re-download the series",
            )
        return df

    def list_series(self) -> list[SeriesKey]:
        keys = []
        for manifest in sorted((self.root / "bars").glob("*/*/*/*/manifest.json")):
            source, freq, adj, symbol = manifest.parent.parts[-4:]
            keys.append(SeriesKey(source, symbol, Frequency(freq), Adjustment(adj)))
        return keys

    # ------------------------------------------------------------ corporate actions
    def write_corporate_actions(
        self, source: str, symbol: str, actions: list[CorporateAction]
    ) -> Path:
        path = self.root / "corporate_actions" / source / f"{symbol}.json"
        payload = [a.model_dump(mode="json") for a in dedupe_actions(actions)]
        _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))
        return path

    def read_corporate_actions(self, source: str, symbol: str) -> list[CorporateAction] | None:
        """Stored actions, or ``None`` if none were ever recorded (unknown != empty)."""
        path = self.root / "corporate_actions" / source / f"{symbol}.json"
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [CorporateAction.model_validate(item) for item in raw]

    # ------------------------------------------------------------ internals
    def _series_dir(self, key: SeriesKey) -> Path:
        return (
            self.root
            / "bars"
            / _safe(key.source)
            / key.frequency.value
            / key.adjustment.value
            / _safe(key.symbol)
        )

    def _load_manifest(self, key: SeriesKey) -> _Manifest:
        path = self._series_dir(key) / "manifest.json"
        if not path.exists():
            return _Manifest()
        return _Manifest.model_validate_json(path.read_text(encoding="utf-8"))

    def _save_manifest(self, key: SeriesKey, manifest: _Manifest) -> None:
        _atomic_write_text(
            self._series_dir(key) / "manifest.json", manifest.model_dump_json(indent=2)
        )

    @staticmethod
    def _write_parquet(path: Path, bars: pd.DataFrame, info: SnapshotInfo) -> None:
        table = pa.Table.from_pandas(bars, preserve_index=True)
        meta = dict(table.schema.metadata or {})
        meta[_META_KEY] = info.model_dump_json().encode()
        table = table.replace_schema_metadata(meta)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".parquet")
        os.close(fd)
        try:
            pq.write_table(table, tmp)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def parquet_provenance(path: Path) -> SnapshotInfo:
    """Read the provenance embedded in a snapshot file (e.g. its synthetic label)."""
    meta = pq.read_schema(path).metadata or {}
    if _META_KEY not in meta:
        raise DataQualityError(f"{path} has no adaptive_quant provenance metadata")
    return SnapshotInfo.model_validate_json(meta[_META_KEY])


def _safe(part: str) -> str:
    if not part or "/" in part or "\\" in part or part in {".", ".."}:
        raise ValueError(f"unsafe path component {part!r}")
    return part


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
