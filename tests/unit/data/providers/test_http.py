import httpx
import pytest

from adaptive_quant.core.errors import DataProviderError, ProviderAuthError
from adaptive_quant.quant.data.providers.http import JsonHttpClient


def client(handler, max_retries: int = 2) -> tuple[JsonHttpClient, list[float]]:  # type: ignore[no-untyped-def]
    sleeps: list[float] = []
    c = JsonHttpClient(
        provider="test",
        base_url="https://example.test",
        headers={"X-Key": "secret"},
        timeout_seconds=1,
        max_retries=max_retries,
        backoff_seconds=0.5,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    return c, sleeps


def test_retries_rate_limit_then_succeeds_honouring_retry_after() -> None:
    calls = iter(
        [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json={"ok": 1})]
    )
    c, sleeps = client(lambda r: next(calls))
    assert c.get_json("/x") == {"ok": 1}
    assert sleeps == [3.0]


def test_retries_server_errors_with_exponential_backoff() -> None:
    calls = iter([httpx.Response(503), httpx.Response(502), httpx.Response(200, json={})])
    c, sleeps = client(lambda r: next(calls))
    assert c.get_json("/x") == {}
    assert sleeps == [0.5, 1.0]


def test_gives_up_after_max_retries() -> None:
    c, sleeps = client(lambda r: httpx.Response(500), max_retries=2)
    with pytest.raises(DataProviderError, match="HTTP 500 after 3 attempts"):
        c.get_json("/x")
    assert len(sleeps) == 2


def test_transport_errors_are_retried_then_raised() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    c, sleeps = client(boom, max_retries=1)
    with pytest.raises(DataProviderError, match="network error"):
        c.get_json("/x")
    assert len(sleeps) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_not_retried(status: int) -> None:
    c, sleeps = client(lambda r: httpx.Response(status))
    with pytest.raises(ProviderAuthError, match=str(status)):
        c.get_json("/x")
    assert sleeps == []


def test_client_errors_and_bad_payloads() -> None:
    c, _ = client(lambda r: httpx.Response(404, text="unknown symbol"))
    with pytest.raises(DataProviderError, match="unknown symbol"):
        c.get_json("/x")
    c, _ = client(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(DataProviderError, match="not JSON"):
        c.get_json("/x")
    c, _ = client(lambda r: httpx.Response(200, json=[1, 2]))
    with pytest.raises(DataProviderError, match="JSON object"):
        c.get_json("/x")
