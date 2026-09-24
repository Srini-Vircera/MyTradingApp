"""Historical data pipeline: provider -> validation -> versioned store.

For each symbol/frequency:

1. Fetch bars from the provider (raw when the provider can supply raw).
2. Merge with the stored raw history (fetched rows win) and count *revisions*:
   previously stored bars whose values changed at the vendor.
3. Fetch corporate actions (daily raw data only). If the provider cannot supply
   them, that is recorded; adjusted series then equal raw and split jumps will
   be flagged by validation.
4. Validate the merged series; store it with its validation outcome. Invalid
   snapshots are kept for inspection but never served by default.
5. For valid daily raw data, derive and store split-adjusted and total-return
   (``all``) series.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import CorporateActionsUnavailable
from adaptive_quant.observability.logging import get_logger
from adaptive_quant.quant.data.bars import (
    Adjustment,
    Frequency,
    SeriesKey,
    is_sorted_unique,
    to_canonical,
)
from adaptive_quant.quant.data.corporate_actions import (
    CorporateAction,
    apply_adjustment,
    dedupe_actions,
)
from adaptive_quant.quant.data.normalize import market_date
from adaptive_quant.quant.data.providers.base import BarRequest, MarketDataProvider
from adaptive_quant.quant.data.store import ParquetBarStore, SnapshotInfo
from adaptive_quant.quant.data.validation import BarValidator, ValidationReport

_log = get_logger(__name__)


@dataclass
class UpdateResult:
    symbol: str
    frequency: Frequency
    fetched_rows: int
    report: ValidationReport
    snapshots: dict[Adjustment, SnapshotInfo] = field(default_factory=dict)
    revisions: int = 0
    corporate_actions: int | None = None  # None = unavailable
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.report.ok and all(s.validation_passed for s in self.snapshots.values())

    def summary(self) -> str:
        stored = ", ".join(f"{a}={s.content_hash[:10]}" for a, s in self.snapshots.items())
        lines = [
            f"{self.symbol} {self.frequency}: fetched {self.fetched_rows} rows, "
            f"{'OK' if self.ok else 'FAILED VALIDATION'}; stored [{stored}]",
            *(f"  {line}" for line in self.report.summary().splitlines()[1:]),
            *(f"  note: {n}" for n in self.notes),
        ]
        return "\n".join(lines)


class DataPipeline:
    def __init__(
        self,
        provider: MarketDataProvider,
        store: ParquetBarStore,
        validator: BarValidator,
        clock: Clock,
    ) -> None:
        self.provider = provider
        self.store = store
        self.validator = validator
        self.clock = clock
        self.source = provider.capabilities.name

    def update(self, symbol: str, frequency: Frequency, start: date, end: date) -> UpdateResult:
        caps = self.provider.capabilities
        basis = Adjustment.RAW if Adjustment.RAW in caps.adjustments else min(caps.adjustments)
        request = BarRequest(symbol, frequency, start, end, basis)
        fetched = to_canonical(self.provider.fetch_bars(request))
        notes: list[str] = []
        if basis is not Adjustment.RAW:
            notes.append(f"provider supplies {basis} data only; no local adjustment possible")

        key = SeriesKey(self.source, symbol, frequency, basis)
        merged, revisions = self._merge_with_stored(key, fetched)
        if revisions:
            notes.append(f"{revisions} previously stored bar(s) were revised by the vendor")

        actions: list[CorporateAction] | None = None
        if frequency is Frequency.DAILY and basis is Adjustment.RAW and not merged.empty:
            actions = self._corporate_actions(symbol, merged, end, notes)

        report = self.validator.validate(
            merged,
            frequency=frequency,
            subject=str(key),
            corporate_action_dates={a.ex_date for a in actions or []},
            as_of=self.clock.now(),
        )
        result = UpdateResult(
            symbol=symbol,
            frequency=frequency,
            fetched_rows=len(fetched),
            report=report,
            revisions=revisions,
            corporate_actions=None if actions is None else len(actions),
            notes=notes,
        )
        if merged.empty:
            return result  # nothing to store; report says EMPTY

        if not is_sorted_unique(merged):
            notes.append("series not stored: fix ordering/duplicates at the source first")
            return result
        provenance = {"request": f"{start}..{end}", "provider_notes": "; ".join(caps.notes)}
        result.snapshots[basis] = self.store.write(
            key, merged, provider=self.source, validation=report, notes=provenance
        )
        if actions is not None:
            self.store.write_corporate_actions(self.source, symbol, actions)
        if report.ok and basis is Adjustment.RAW and frequency is Frequency.DAILY:
            for mode in (Adjustment.SPLIT, Adjustment.ALL):
                result.snapshots[mode] = self._store_adjusted(symbol, merged, actions or [], mode)
        _log.info(
            "data_updated",
            symbol=symbol,
            frequency=frequency.value,
            rows=len(merged),
            ok=result.ok,
            revisions=revisions,
        )
        return result

    # ------------------------------------------------------------ steps
    def _merge_with_stored(self, key: SeriesKey, fetched: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        stored_info = self.store.latest(key)
        if stored_info is None or not is_sorted_unique(fetched):
            return fetched, 0
        stored = self.store.read_snapshot(stored_info)
        overlap = stored.index.intersection(fetched.index)
        revisions = 0
        if len(overlap):
            cols = [c for c in stored.columns if c in fetched.columns]
            a = stored.loc[overlap, cols].to_numpy(dtype=float)
            b = fetched.loc[overlap, cols].to_numpy(dtype=float)
            same = np.isclose(a, b, rtol=1e-9, atol=0.0, equal_nan=True).all(axis=1)
            revisions = int((~same).sum())
        kept = stored[~stored.index.isin(fetched.index)]
        merged = pd.concat([kept, fetched]).sort_index()
        return to_canonical(merged), revisions

    def _corporate_actions(
        self, symbol: str, bars: pd.DataFrame, end: date, notes: list[str]
    ) -> list[CorporateAction] | None:
        first = market_date(bars.index[0].to_pydatetime())
        try:
            actions = self.provider.fetch_corporate_actions(symbol, first, end)
        except CorporateActionsUnavailable as exc:
            notes.append(
                f"corporate actions unavailable ({exc.message}); adjusted series not derived"
            )
            return None
        return dedupe_actions(actions)

    def _store_adjusted(
        self,
        symbol: str,
        raw: pd.DataFrame,
        actions: list[CorporateAction],
        mode: Adjustment,
    ) -> SnapshotInfo:
        adjusted = apply_adjustment(raw, actions, mode)
        key = SeriesKey(self.source, symbol, Frequency.DAILY, mode)
        report = self.validator.validate(
            adjusted,
            frequency=Frequency.DAILY,
            subject=str(key),
            as_of=self.clock.now(),
        )
        return self.store.write(
            key,
            adjusted,
            provider=self.source,
            validation=report,
            notes={"derived_from": "raw", "corporate_actions": str(len(actions))},
        )
