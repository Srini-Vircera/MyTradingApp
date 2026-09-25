from datetime import UTC, datetime

from adaptive_quant.core.enums import Severity
from adaptive_quant.notifications.base import (
    DEFAULT_SEVERITY,
    EventType,
    Notification,
    NotificationRouter,
    Notifier,
)

NOW = datetime(2024, 1, 2, tzinfo=UTC)


class Recorder(Notifier):
    def __init__(self, name: str = "rec") -> None:
        self.name = name
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class Broken(Notifier):
    name = "broken"

    def send(self, notification: Notification) -> None:
        raise ConnectionError("smtp down")


def note(event: EventType, severity: Severity | None = None) -> Notification:
    return Notification(event=event, title="t", body="b", at=NOW, severity=severity)


def test_every_event_has_default_severity() -> None:
    assert set(DEFAULT_SEVERITY) == set(EventType)


def test_filters_below_min_severity() -> None:
    rec = Recorder()
    router = NotificationRouter([rec], Severity.WARNING)
    assert router.publish(note(EventType.ORDER_PLACED)) == []
    router.publish(note(EventType.KILL_SWITCH_ACTIVATED))
    assert [n.event for n in rec.sent] == [EventType.KILL_SWITCH_ACTIVATED]


def test_explicit_severity_overrides_default() -> None:
    rec = Recorder()
    NotificationRouter([rec], Severity.ERROR).publish(
        note(EventType.ORDER_PLACED, Severity.CRITICAL)
    )
    assert len(rec.sent) == 1


def test_failing_channel_does_not_block_others() -> None:
    rec = Recorder()
    results = NotificationRouter([Broken(), rec], Severity.INFO).publish(
        note(EventType.BROKER_DISCONNECTED)
    )
    assert [(r.channel, r.delivered) for r in results] == [("broken", False), ("rec", True)]
    assert results[0].error == "smtp down"
    assert len(rec.sent) == 1
