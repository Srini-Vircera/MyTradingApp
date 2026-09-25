"""Pre-trade checks that market data exists, is valid, and is fresh.

``quant.data`` knows nothing about trading; this module adapts its freshness
and validation results into :class:`~adaptive_quant.trading.safety.preflight.CheckResult`
objects for the pre-trade gate.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pandas as pd

from adaptive_quant.config.schema import StalenessConfig
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import MissingDataError
from adaptive_quant.quant.data.bars import Frequency
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.quant.data.freshness import check_daily_freshness, check_intraday_freshness
from adaptive_quant.quant.data.validation import BarValidator
from adaptive_quant.trading.safety.preflight import CheckResult, RefusalReason

#: Returns the bars to trade on for a symbol; raises MissingDataError if none.
BarLoader = Callable[[str], pd.DataFrame]


class MarketDataCheck:
    """Refuses to trade unless every required symbol has valid, fresh bars.

    Order of evaluation per symbol: missing -> invalid -> stale. All symbols are
    evaluated and every problem is reported.
    """

    def __init__(
        self,
        *,
        symbols: Sequence[str],
        frequency: Frequency,
        loader: BarLoader,
        validator: BarValidator,
        calendar: TradingCalendar,
        staleness: StalenessConfig,
        clock: Clock,
    ) -> None:
        self.name = f"market_data_{frequency.value}"
        self._symbols = tuple(symbols)
        self._frequency = frequency
        self._loader = loader
        self._validator = validator
        self._calendar = calendar
        self._staleness = staleness
        self._clock = clock

    def run(self) -> CheckResult:
        if not self._symbols:
            return CheckResult.fail(
                self.name, RefusalReason.MARKET_DATA_MISSING, "no symbols configured"
            )
        now = self._clock.now()
        missing, invalid, stale = [], [], []
        for symbol in self._symbols:
            try:
                bars = self._loader(symbol)
            except MissingDataError as exc:
                missing.append(f"{symbol} ({exc.message})")
                continue
            report = self._validator.validate(
                bars, frequency=self._frequency, subject=symbol, as_of=now
            )
            if not report.ok:
                kinds = ", ".join(sorted(i.kind.value for i in report.errors))
                invalid.append(f"{symbol} ({kinds})")
                continue
            if self._frequency is Frequency.DAILY:
                fresh = check_daily_freshness(
                    bars,
                    subject=symbol,
                    as_of=now,
                    calendar=self._calendar,
                    max_age_sessions=self._staleness.max_daily_bar_age_sessions,
                )
            else:
                fresh = check_intraday_freshness(
                    bars,
                    subject=symbol,
                    as_of=now,
                    calendar=self._calendar,
                    max_age_seconds=self._staleness.max_intraday_bar_age_seconds,
                )
            if not fresh.fresh:
                stale.append(f"{symbol} ({fresh.detail})")

        if missing:
            return CheckResult.fail(
                self.name, RefusalReason.MARKET_DATA_MISSING, _join(missing, invalid, stale)
            )
        if invalid:
            return CheckResult.fail(
                self.name, RefusalReason.MARKET_DATA_INVALID, _join(missing, invalid, stale)
            )
        if stale:
            return CheckResult.fail(
                self.name, RefusalReason.MARKET_DATA_STALE, _join(missing, invalid, stale)
            )
        return CheckResult.ok(self.name, f"{len(self._symbols)} symbol(s) valid and fresh")


def _join(missing: list[str], invalid: list[str], stale: list[str]) -> str:
    parts = []
    if missing:
        parts.append("missing: " + "; ".join(missing))
    if invalid:
        parts.append("invalid: " + "; ".join(invalid))
    if stale:
        parts.append("stale: " + "; ".join(stale))
    return " | ".join(parts)
