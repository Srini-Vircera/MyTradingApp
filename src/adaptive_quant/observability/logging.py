"""Structured logging built on ``structlog``.

* Every record is a key/value event with a UTC ISO-8601 timestamp.
* ``json`` format for machines (files, CloudWatch); ``console`` for humans.
* Context such as ``run_id`` and ``config_version`` is bound once with
  :func:`bind_context` and then appears on every subsequent record.
* Secret-looking keys are redacted by a processor as defence in depth.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog

from adaptive_quant.config.schema import is_secret_key

REDACTED = "***"


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: mask values of keys that look like credentials."""
    for key in list(event_dict):
        if is_secret_key(key) and event_dict[key] not in (None, ""):
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json", stream: TextIO | None = None) -> None:
    """Configure structlog and the stdlib root logger. Safe to call more than once."""
    numeric_level = logging.getLevelNamesMapping()[level.upper()]
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: structlog.typing.Processor
    if fmt == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer(sort_keys=True)

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=_StreamLoggerFactory(stream),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=numeric_level, stream=stream, force=True)


class _StreamLoggerFactory:
    """Writes to ``stream`` or, if none was given, to *whatever* ``sys.stderr`` is at
    write time (so redirection and test capture keep working after configuration)."""

    def __init__(self, stream: TextIO | None) -> None:
        self._stream = stream

    def __call__(self, *_args: Any) -> structlog.PrintLogger:
        return structlog.PrintLogger(self._stream or sys.stderr)


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(component=name)
    return logger


def bind_context(**values: Any) -> None:
    """Attach values (e.g. ``run_id``, ``config_version``) to all later log records."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
