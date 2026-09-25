"""Provider contract.

Adapters return **canonical, unvalidated** bar frames (see ``bars.py``) in the
order the vendor delivered them; validation is the pipeline's job so that
every provider is judged by the same rules. Strategies never touch providers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from adaptive_quant.core.errors import CorporateActionsUnavailable
from adaptive_quant.quant.data.bars import Adjustment, Frequency
from adaptive_quant.quant.data.corporate_actions import CorporateAction


@dataclass(frozen=True)
class BarRequest:
    symbol: str
    frequency: Frequency
    start: date  # inclusive, exchange-local date
    end: date  # inclusive, exchange-local date
    adjustment: Adjustment = Adjustment.RAW

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"request end {self.end} is before start {self.start}")
        if not self.symbol:
            raise ValueError("symbol must not be empty")


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    frequencies: frozenset[Frequency]
    adjustments: frozenset[Adjustment]
    corporate_actions: bool
    symbols: frozenset[str] | None = None  # None = any symbol
    notes: tuple[str, ...] = field(default=())


class MarketDataProvider(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    def fetch_bars(self, request: BarRequest) -> pd.DataFrame:
        """Canonical bars for the request (possibly empty); raises ``DataProviderError``."""

    def fetch_corporate_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        """Splits and cash distributions with ex-dates in ``[start, end]``.

        Raises :class:`CorporateActionsUnavailable` when the provider cannot say -
        an empty list always means "confirmed: none".
        """
        raise CorporateActionsUnavailable(
            f"{self.capabilities.name} does not provide corporate actions"
        )

    def supports(self, request: BarRequest) -> bool:
        caps = self.capabilities
        return (
            request.frequency in caps.frequencies
            and request.adjustment in caps.adjustments
            and (caps.symbols is None or request.symbol in caps.symbols)
        )

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""
