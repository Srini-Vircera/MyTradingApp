"""Compute sets of indicators, and point-in-time snapshots for strategies.

Two ways to evaluate, with the same results (proved by the look-ahead tests):

* :meth:`IndicatorEngine.compute` - the whole history at once (fast; used by
  backtests). Valid because every indicator is causal.
* :meth:`IndicatorEngine.snapshot` - the values *as of* a moment. The bars are
  truncated to ``timestamp <= as_of`` **before** anything is computed, so the
  snapshot is point-in-time by construction, independent of indicator code.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from adaptive_quant.core.clock import ensure_utc
from adaptive_quant.core.errors import MissingDataError
from adaptive_quant.quant.data.bars import require_canonical_sorted
from adaptive_quant.quant.data.view import MarketDataView
from adaptive_quant.quant.indicators.specs import IndicatorSpec


@dataclass(frozen=True)
class IndicatorSnapshot:
    """Indicator values known at ``as_of``. ``None`` = not yet warmed up."""

    as_of: datetime
    data_timestamp: datetime
    values: dict[str, float | None]

    @property
    def ready(self) -> bool:
        return all(v is not None for v in self.values.values())

    def require(self, *names: str) -> dict[str, float]:
        """Values for ``names``; raises if any is missing or still warming up."""
        missing = [n for n in names if self.values.get(n) is None]
        if missing:
            raise MissingDataError(
                f"indicators not available as of {self.as_of:%Y-%m-%d %H:%M}Z: {missing}",
                hint="load more history (see the strategy's warm-up requirement)",
            )
        return {n: float(self.values[n]) for n in names}  # type: ignore[arg-type]


class IndicatorEngine:
    def __init__(self, specs: Sequence[IndicatorSpec]) -> None:
        names = [s.name for s in specs]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate indicator names: {dupes}")
        self.specs = tuple(specs)

    @property
    def warmup(self) -> int:
        """Rows of history needed before every indicator has a value."""
        return max((s.warmup for s in self.specs), default=0)

    def compute(self, bars: pd.DataFrame) -> pd.DataFrame:
        require_canonical_sorted(bars)
        columns = {s.name: s.compute(bars) for s in self.specs}
        return pd.DataFrame(columns, index=bars.index)

    def snapshot(self, bars: pd.DataFrame, as_of: datetime) -> IndicatorSnapshot:
        cut = ensure_utc(as_of)
        require_canonical_sorted(bars)
        visible = bars[bars.index <= pd.Timestamp(cut)]
        if visible.empty:
            raise MissingDataError(f"no bars known as of {cut:%Y-%m-%d %H:%M}Z")
        last = self.compute(visible).iloc[-1]
        values = {
            str(name): (None if not math.isfinite(float(v)) else float(v))
            for name, v in last.items()
        }
        data_ts: datetime = visible.index[-1].to_pydatetime()
        return IndicatorSnapshot(as_of=cut, data_timestamp=data_ts, values=values)

    def snapshot_view(self, view: MarketDataView, symbol: str) -> IndicatorSnapshot:
        """Snapshot from a point-in-time view (the strategy-facing entry point)."""
        return self.snapshot(view.bars(symbol), view.as_of)
