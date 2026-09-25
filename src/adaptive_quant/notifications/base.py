"""Notification contract.

Channels (email now; SMS/push later) implement :class:`Notifier`. A
:class:`NotificationRouter` fans an event out to every channel whose minimum
severity it meets, and never lets a failing channel crash the trading loop -
failures are logged and reported back to the caller.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from adaptive_quant.core.enums import Severity
from adaptive_quant.observability.logging import get_logger

_log = get_logger(__name__)


class EventType(StrEnum):
    ORDER_PLACED = "order_placed"
    ORDER_REJECTED = "order_rejected"
    PARTIAL_FILL = "partial_fill"
    RECONCILIATION_FAILURE = "reconciliation_failure"
    BROKER_DISCONNECTED = "broker_disconnected"
    MARKET_DATA_STALE = "market_data_stale"
    STRATEGY_FAILURE = "strategy_failure"
    DAILY_LOSS_THRESHOLD = "daily_loss_threshold"
    DRAWDOWN_THRESHOLD = "drawdown_threshold"
    KILL_SWITCH_ACTIVATED = "kill_switch_activated"
    SYSTEM_STARTUP = "system_startup"
    SYSTEM_SHUTDOWN = "system_shutdown"
    END_OF_DAY_SUMMARY = "end_of_day_summary"
    TRADING_REFUSED = "trading_refused"


#: Default severity per event type (overridable per notification).
DEFAULT_SEVERITY: dict[EventType, Severity] = {
    EventType.ORDER_PLACED: Severity.INFO,
    EventType.ORDER_REJECTED: Severity.ERROR,
    EventType.PARTIAL_FILL: Severity.WARNING,
    EventType.RECONCILIATION_FAILURE: Severity.CRITICAL,
    EventType.BROKER_DISCONNECTED: Severity.CRITICAL,
    EventType.MARKET_DATA_STALE: Severity.ERROR,
    EventType.STRATEGY_FAILURE: Severity.ERROR,
    EventType.DAILY_LOSS_THRESHOLD: Severity.CRITICAL,
    EventType.DRAWDOWN_THRESHOLD: Severity.CRITICAL,
    EventType.KILL_SWITCH_ACTIVATED: Severity.CRITICAL,
    EventType.SYSTEM_STARTUP: Severity.INFO,
    EventType.SYSTEM_SHUTDOWN: Severity.WARNING,
    EventType.END_OF_DAY_SUMMARY: Severity.INFO,
    EventType.TRADING_REFUSED: Severity.ERROR,
}


@dataclass(frozen=True)
class Notification:
    event: EventType
    title: str
    body: str
    at: datetime
    severity: Severity | None = None
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_severity(self) -> Severity:
        return self.severity or DEFAULT_SEVERITY[self.event]


class Notifier(ABC):
    """A delivery channel."""

    name: str

    @abstractmethod
    def send(self, notification: Notification) -> None:
        """Deliver or raise. Implementations must not swallow their own errors."""


@dataclass(frozen=True)
class DeliveryResult:
    channel: str
    delivered: bool
    error: str | None = None


class NotificationRouter:
    def __init__(self, channels: Sequence[Notifier], min_severity: Severity) -> None:
        self._channels = tuple(channels)
        self._min = min_severity

    def publish(self, notification: Notification) -> list[DeliveryResult]:
        if notification.effective_severity.rank < self._min.rank:
            return []
        results = []
        for channel in self._channels:
            try:
                channel.send(notification)
                results.append(DeliveryResult(channel.name, delivered=True))
            except Exception as exc:  # noqa: BLE001 - alerting must not crash trading
                _log.exception(
                    "notification_failed", channel=channel.name, event_type=notification.event
                )
                results.append(DeliveryResult(channel.name, delivered=False, error=str(exc)))
        return results
