"""Run many strategies against one point-in-time view, failing closed.

A strategy that raises - for any reason - is recorded as a failure with its
error; the batch is then not ``ok`` and the pre-trade gate refuses to trade
(``SIGNAL_FAILURE``). Nothing is silently skipped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from adaptive_quant.core.errors import InsufficientHistoryError
from adaptive_quant.core.models import StrategySignal
from adaptive_quant.observability.logging import get_logger
from adaptive_quant.quant.data.view import MarketDataView
from adaptive_quant.quant.strategies.base import Strategy

_log = get_logger(__name__)


@dataclass(frozen=True)
class SignalBatch:
    as_of: datetime
    signals: tuple[StrategySignal, ...]
    failures: dict[str, str] = field(default_factory=dict)
    not_ready: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only if every strategy produced a signal and there was at least one."""
        return bool(self.signals) and not self.failures and not self.not_ready

    def problems(self) -> list[str]:
        return [f"{k}: {v}" for k, v in {**self.not_ready, **self.failures}.items()]


def run_strategies(strategies: Sequence[Strategy], view: MarketDataView) -> SignalBatch:
    signals: list[StrategySignal] = []
    failures: dict[str, str] = {}
    not_ready: dict[str, str] = {}
    for strategy in strategies:
        try:
            signals.append(strategy.generate_signal(view))
        except InsufficientHistoryError as exc:
            not_ready[strategy.strategy_id] = exc.message
        except Exception as exc:  # noqa: BLE001 - fail closed: record, never crash or skip
            _log.exception("strategy_failed", strategy=strategy.strategy_id)
            failures[strategy.strategy_id] = f"{type(exc).__name__}: {exc}"
    return SignalBatch(view.as_of, tuple(signals), failures, not_ready)
