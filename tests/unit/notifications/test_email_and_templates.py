"""Every event type has a template; the email channel formats, authenticates and fails loudly."""

from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any, ClassVar, cast

import pytest
from pydantic import SecretStr

from adaptive_quant.config.schema import EmailConfig
from adaptive_quant.core.enums import Severity
from adaptive_quant.notifications.base import EventType, Notification, NotificationRouter
from adaptive_quant.notifications.email_channel import EmailNotifier, SmtpFactory
from adaptive_quant.notifications.templates import TEMPLATES, render

NOW = datetime(2024, 7, 2, 20, 0, tzinfo=UTC)
CFG = EmailConfig(
    enabled=True,
    smtp_host="smtp.example.test",
    from_address="aq@example.test",
    to_addresses=["ops@example.test"],
)


class FakeSMTP:
    instances: ClassVar[list["FakeSMTP"]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *_a: object) -> None:
        self.calls.append("quit")

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}")

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


def note(event: EventType, **ctx: Any) -> Notification:
    return Notification(
        event,
        event.value,
        "",
        NOW,
        context={"mode": "paper", "environment": "paper", "config_version": "cfg-1", **ctx},
    )


def test_every_event_type_has_a_template() -> None:
    assert set(TEMPLATES) == set(EventType)
    for event in EventType:
        subject, body = render(note(event))
        assert subject and "\n" not in subject
        assert "Environment: paper | mode: paper | config: cfg-1" in body
        assert "hypothetical" in body


def test_missing_context_never_suppresses_an_alert() -> None:
    subject, body = render(note(EventType.ORDER_REJECTED, symbol="TQQQ"))
    assert "TQQQ" in subject and "<missing: reason>" in body


def test_email_message_login_and_no_secret_in_content() -> None:
    FakeSMTP.instances.clear()
    n = EmailNotifier(
        CFG,
        SecretStr("user@example.test"),
        SecretStr("smtp-pw-123"),
        smtp_factory=cast(SmtpFactory, FakeSMTP),
    )
    n.send(note(EventType.RECONCILIATION_FAILURE, cycle_id="cyc-1", differences="QQQ 10 vs 12"))
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.test", 587)
    assert smtp.calls == ["starttls", "login:user@example.test", "quit"]
    msg = smtp.sent[0]
    assert msg["Subject"].startswith("[AQ CRITICAL] [paper] RECONCILIATION FAILED")
    assert msg["To"] == "ops@example.test" and msg["X-AQ-Event"] == "reconciliation_failure"
    assert "smtp-pw-123" not in msg.as_string()
    assert "BLOCKED until a person acknowledges" in msg.get_content()


def test_disabled_email_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="disabled"):
        EmailNotifier(EmailConfig(), None, None)


def test_failing_channel_is_reported_not_raised() -> None:
    class Broken(FakeSMTP):
        def send_message(self, msg: EmailMessage) -> None:
            raise OSError("connection refused")

    router = NotificationRouter(
        [EmailNotifier(CFG, None, None, smtp_factory=cast(SmtpFactory, Broken))], Severity.INFO
    )
    (res,) = router.publish(note(EventType.SYSTEM_STARTUP, next_step="x", time="t"))
    assert not res.delivered and "connection refused" in (res.error or "")
