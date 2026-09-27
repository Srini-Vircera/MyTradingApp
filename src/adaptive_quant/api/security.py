"""Single-operator bearer-token authentication and response hardening."""

from __future__ import annotations

import hmac
import threading
import time
from collections.abc import Awaitable, Callable
from typing import ClassVar

from fastapi import HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from adaptive_quant.observability.logging import get_logger

_log = get_logger(__name__)
bearer = HTTPBearer(auto_error=False, description="Operator token (AQ_API_TOKEN)")


def check_token(
    expected: SecretStr, creds: HTTPAuthorizationCredentials | None, client: str
) -> None:
    supplied = "" if creds is None or creds.scheme.lower() != "bearer" else creds.credentials
    if not hmac.compare_digest(supplied.encode(), expected.get_secret_value().encode()):
        _log.warning("api_auth_failed", client=client)  # never log the supplied value
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid operator token",
            headers={"WWW-Authenticate": "Bearer"},
        )


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


async def harden(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


class RateLimiter:
    """Sliding-window limits per client and bucket for control operations (in memory).

    A single operator never comes close; this stops a leaked-token script or a
    stuck browser tab from flooding the job queue or the audit trail.
    """

    LIMITS: ClassVar[dict[str, tuple[int, float]]] = {
        "job": (30, 60.0),  # queue at most 30 jobs a minute
        "upload": (10, 600.0),  # 10 uploads per 10 minutes
        "control": (20, 60.0),  # lifecycle, settings, scheduler, mode, kill switch
    }

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._hits: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def check(self, bucket: str, client: str) -> None:
        limit, window = self.LIMITS[bucket]
        now = self._clock()
        with self._lock:
            hits = [t for t in self._hits.get((bucket, client), []) if now - t < window]
            if len(hits) >= limit:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"too many {bucket} requests; try again shortly",
                    headers={"Retry-After": str(int(window))},
                )
            hits.append(now)
            self._hits[(bucket, client)] = hits


def client_of(request: Request) -> str:
    return request.client.host if request.client else "?"
