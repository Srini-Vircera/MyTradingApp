"""Per-job context: progress, cooperative cancellation, keep-alive and safe errors."""

from __future__ import annotations

import threading
from datetime import timedelta

from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import AQError
from adaptive_quant.observability.logging import get_logger
from adaptive_quant.persistence.control import ControlRepository

_log = get_logger(__name__)
KEEPALIVE_SECONDS = 30.0
ORPHAN_AFTER = timedelta(minutes=10)


class JobCancelled(AQError):
    """Raised at a safe checkpoint after the operator cancelled the job."""


def scrub(text: str, secrets: Secrets) -> str:
    """Remove any configured credential value from a message (defence in depth)."""
    out = text
    for name in type(secrets).model_fields:
        value = getattr(secrets, name)
        if value is None:
            continue
        raw = value.get_secret_value()
        if len(raw) >= 6:
            out = out.replace(raw, "***")
    return out


def safe_error(exc: BaseException, secrets: Secrets) -> str:
    if isinstance(exc, AQError):
        msg = exc.message + (f" - {exc.hint}" if exc.hint else "")
    else:
        msg = f"{type(exc).__name__}: {exc}"
    return scrub(msg, secrets)[:4000]


class JobContext:
    def __init__(
        self,
        repo: ControlRepository,
        job_id: str,
        worker_id: str,
        clock: Clock,
        secrets: Secrets,
    ) -> None:
        self.repo = repo
        self.job_id = job_id
        self.worker_id = worker_id
        self.clock = clock
        self.secrets = secrets
        self.cancelled = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._keepalive, daemon=True)

    def __enter__(self) -> JobContext:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _keepalive(self) -> None:
        while not self._stop.wait(KEEPALIVE_SECONDS):
            try:
                if self.repo.heartbeat(self.job_id, self.worker_id, self.clock.now()):
                    self.cancelled = True
            except AQError as exc:  # pragma: no cover - database hiccup; next beat retries
                _log.warning("job_keepalive_failed", job=self.job_id, error=exc.message)

    def progress(self, message: str, fraction: float | None = None) -> None:
        """Record progress; a cancellation request stops the job here (a safe checkpoint)."""
        message = scrub(message, self.secrets)
        now = self.clock.now()
        if self.repo.heartbeat(
            self.job_id, self.worker_id, now, progress=fraction, message=message
        ):
            self.cancelled = True
        self.repo.log(self.job_id, "info", message, now)
        if self.cancelled:
            raise JobCancelled("cancelled by the operator")

    def log(self, level: str, message: str) -> None:
        self.repo.log(self.job_id, level, scrub(message, self.secrets), self.clock.now())
