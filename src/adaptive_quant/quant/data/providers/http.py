"""Small JSON-over-HTTPS client shared by network providers.

* Retries transport errors, HTTP 429 (honouring ``Retry-After``) and 5xx with
  exponential backoff; gives up after ``max_retries`` with a readable error.
* 401/403 become :class:`ProviderAuthError` with a hint about credentials.
* Credentials travel in headers only, never in URLs, so they cannot leak into
  logs or error messages.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from adaptive_quant.core.errors import DataProviderError, ProviderAuthError
from adaptive_quant.observability.logging import get_logger

_log = get_logger(__name__)
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class JsonHttpClient:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            headers=dict(headers),
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def get_json(self, url: str, params: Mapping[str, str | int] | None = None) -> dict[str, Any]:
        """GET ``url`` (relative to base, or absolute) and return the decoded JSON object."""
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.get(url, params=dict(params or {}))
            except httpx.TransportError as exc:
                if attempt > self._max_retries:
                    raise DataProviderError(
                        f"{self.provider}: network error after {attempt} attempts: {exc}",
                        hint="check connectivity / provider status and retry later",
                    ) from exc
                self._pause(attempt, None, f"transport error {type(exc).__name__}")
                continue
            status = response.status_code
            if status in (401, 403):
                raise ProviderAuthError(
                    f"{self.provider}: request rejected with HTTP {status}",
                    hint="check the API key environment variables and your data subscription",
                )
            if status in _RETRY_STATUS:
                if attempt > self._max_retries:
                    raise DataProviderError(
                        f"{self.provider}: HTTP {status} after {attempt} attempts",
                        hint="the provider is rate limiting or unavailable; retry later",
                    )
                self._pause(attempt, response.headers.get("Retry-After"), f"HTTP {status}")
                continue
            if status >= 400:
                raise DataProviderError(
                    f"{self.provider}: HTTP {status}: {response.text[:300]}",
                    hint="check the symbol, dates and parameters of the request",
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise DataProviderError(f"{self.provider}: response is not JSON") from exc
            if not isinstance(payload, dict):
                raise DataProviderError(f"{self.provider}: expected a JSON object")
            return payload

    def _pause(self, attempt: int, retry_after: str | None, why: str) -> None:
        delay = self._backoff * 2 ** (attempt - 1)
        if retry_after is not None:
            with contextlib.suppress(ValueError):  # HTTP-date form: keep exponential delay
                delay = max(delay, float(retry_after))
        _log.warning(
            "provider_retry", provider=self.provider, attempt=attempt, reason=why, delay=delay
        )
        self._sleep(delay)

    def close(self) -> None:
        self._client.close()
