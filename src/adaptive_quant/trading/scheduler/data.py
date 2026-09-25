"""Market data for the live cycle: refresh from the provider, read point-in-time frames.

``CycleData`` is the interface the trading cycle depends on, so tests and drills
can supply generated data while production uses the M2 store and pipeline.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

import pandas as pd

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import DataQualityError
from adaptive_quant.quant.data.bars import Adjustment, Frequency, SeriesKey
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.quant.data.factory import build_provider, build_store, build_validator
from adaptive_quant.quant.data.pipeline import DataPipeline


class CycleData(Protocol):
    def refresh(self) -> list[str]:
        """Fetch the latest completed bars; raise on failure. Returns notes."""

    def frames(self) -> dict[str, pd.DataFrame]:
        """Total-return adjusted daily bars (the same basis as backtests)."""

    def bars(self, symbol: str) -> pd.DataFrame:
        """Bars used by the freshness/validity check (raises MissingDataError)."""


class StoreCycleData:
    def __init__(
        self,
        loaded: LoadedConfig,
        secrets: Secrets,
        clock: Clock,
        calendar: TradingCalendar,
        symbols: Sequence[str],
        refresh_days: int = 30,
    ) -> None:
        self.loaded, self.secrets, self.clock, self.calendar = loaded, secrets, clock, calendar
        self.symbols = list(symbols)
        self.source = loaded.settings.data.primary_provider
        self.store = build_store(loaded, clock)
        self.refresh_days = refresh_days

    def refresh(self) -> list[str]:
        last = self.calendar.last_completed_session(self.clock.now())
        if last is None:
            raise DataQualityError("no completed session to update")
        provider = build_provider(self.source, self.loaded, self.secrets, self.calendar)
        pipeline = DataPipeline(
            provider, self.store, build_validator(self.loaded, self.calendar), self.clock
        )
        notes: list[str] = []
        failed: list[str] = []
        try:
            for sym in self.symbols:
                r = pipeline.update(
                    sym, Frequency.DAILY, last.date - timedelta(days=self.refresh_days), last.date
                )
                (notes if r.ok else failed).append(r.summary())
        finally:
            provider.close()
        if failed:
            raise DataQualityError("market data update failed: " + "; ".join(failed))
        return notes

    def frames(self) -> dict[str, pd.DataFrame]:
        return {s: self.bars(s) for s in self.symbols}

    def bars(self, symbol: str) -> pd.DataFrame:
        return self.store.read(SeriesKey(self.source, symbol, Frequency.DAILY, Adjustment.ALL))
