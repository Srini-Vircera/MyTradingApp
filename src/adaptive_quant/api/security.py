"""Single-operator bearer-token authentication and response hardening."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

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
