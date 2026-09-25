"""SMTP email channel (STARTTLS). Credentials come only from SMTP_USERNAME / SMTP_PASSWORD."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage

from pydantic import SecretStr

from adaptive_quant.config.schema import EmailConfig
from adaptive_quant.notifications.base import Notification, Notifier
from adaptive_quant.notifications.templates import render

SmtpFactory = Callable[[str, int, float], smtplib.SMTP]


class EmailNotifier(Notifier):
    name = "email"

    def __init__(
        self,
        cfg: EmailConfig,
        username: SecretStr | None,
        password: SecretStr | None,
        *,
        timeout_seconds: float = 15.0,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        if not cfg.enabled:
            raise ValueError("email notifications are disabled in the configuration")
        self.cfg = cfg
        self._username = username
        self._password = password
        self._timeout = timeout_seconds
        self._factory: SmtpFactory = smtp_factory or (lambda h, p, t: smtplib.SMTP(h, p, timeout=t))

    def message(self, n: Notification) -> EmailMessage:
        subject, body = render(n)
        msg = EmailMessage()
        msg["Subject"] = f"[AQ {n.effective_severity.value.upper()}] {subject}"
        msg["From"] = self.cfg.from_address
        msg["To"] = ", ".join(self.cfg.to_addresses)
        msg["X-AQ-Event"] = n.event.value
        msg.set_content(body)
        return msg

    def send(self, notification: Notification) -> None:
        msg = self.message(notification)
        with self._factory(self.cfg.smtp_host, self.cfg.smtp_port, self._timeout) as smtp:
            if self.cfg.use_starttls:
                smtp.starttls()
            if self._username is not None and self._password is not None:
                smtp.login(self._username.get_secret_value(), self._password.get_secret_value())
            smtp.send_message(msg)
