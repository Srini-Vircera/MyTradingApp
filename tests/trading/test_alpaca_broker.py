"""Alpaca paper adapter against recorded-format responses (no network)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.enums import OrderSide, OrderState, OrderType
from adaptive_quant.core.errors import BrokerError, SafetyViolation
from adaptive_quant.core.models import OrderRequest
from adaptive_quant.trading.brokers.alpaca import STATUS_MAP, AlpacaPaperBroker
from adaptive_quant.trading.brokers.base import DuplicateClientOrderId, OrderRejectedByBroker

NOW = datetime(2024, 1, 2, 20, 45, tzinfo=UTC)
KEY, SECRET = "PKTESTKEY", "test-secret-value"


def order_json(
    status: str = "new", filled: str = "0", avg: str | None = None, cid: str = "aq-1"
) -> dict[str, Any]:
    return {
        "id": "b-1",
        "client_order_id": cid,
        "symbol": "TQQQ",
        "side": "buy",
        "qty": "10",
        "filled_qty": filled,
        "filled_avg_price": avg,
        "status": status,
        "type": "market",
        "time_in_force": "day",
        "submitted_at": "2024-01-02T20:45:00Z",
        "updated_at": "2024-01-02T20:45:01Z",
    }


class Server:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], list[Any]] = {}

    def on(self, method: str, path: str, *responses: Any) -> None:
        self.routes[(method, path)] = list(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path), [])
        if not queue:
            return httpx.Response(404, json={"message": "not found"})
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body)


def broker(server: Server, url: str = "https://paper-api.alpaca.markets") -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        key_id=SecretStr(KEY),
        secret_key=SecretStr(SECRET),
        base_url=url,
        transport=httpx.MockTransport(server.handler),
        sleep=lambda _s: None,
        clock=FrozenClock(NOW),
    )


def req(order_type: OrderType = OrderType.MARKET) -> OrderRequest:
    return OrderRequest(
        client_order_id="aq-1",
        symbol="TQQQ",
        side=OrderSide.BUY,
        quantity=Decimal("10"),
        order_type=order_type,
        risk_increasing=True,
        decision_id="dec-1",
    )


@pytest.mark.parametrize(
    "url", ["https://api.alpaca.markets", "http://paper-api.alpaca.markets", "https://evil.example"]
)
def test_only_the_paper_endpoint_is_accepted(url: str) -> None:
    with pytest.raises(SafetyViolation, match="paper"):
        broker(Server(), url)


def test_reads_parse_and_send_header_credentials() -> None:
    s = Server()
    s.on(
        "GET",
        "/v2/account",
        (
            200,
            {
                "equity": "100000.5",
                "cash": "20000",
                "buying_power": "40000",
                "trading_blocked": False,
            },
        ),
    )
    s.on(
        "GET",
        "/v2/positions",
        (200, [{"symbol": "QQQ", "qty": "12.5", "avg_entry_price": "400", "market_value": "5000"}]),
    )
    s.on("GET", "/v2/orders", (200, [order_json("partially_filled", "4", "50.5")]))
    s.on(
        "GET",
        "/v2/clock",
        (
            200,
            {
                "timestamp": "2024-01-02T15:45:00-05:00",
                "is_open": True,
                "next_open": "2024-01-03T09:30:00-05:00",
                "next_close": "2024-01-02T16:00:00-05:00",
            },
        ),
    )
    b = broker(s)
    a = b.get_account()
    assert (
        a.equity == Decimal("100000.5") and a.is_paper and not a.trading_blocked and a.as_of == NOW
    )
    assert b.get_positions()[0].quantity == Decimal("12.5")
    o = b.get_open_orders()[0]
    assert (
        o.state is OrderState.PARTIALLY_FILLED
        and o.filled_quantity == 4
        and o.avg_fill_price == Decimal("50.5")
    )
    assert b.get_market_clock().is_open
    for r in s.requests:
        assert r.headers["APCA-API-KEY-ID"] == KEY and SECRET not in str(r.url)
    assert s.requests[2].url.params["status"] == "open"
    assert SECRET not in repr(b)


def test_every_alpaca_status_is_mapped() -> None:
    assert (
        STATUS_MAP["filled"] is OrderState.FILLED
        and STATUS_MAP["pending_new"] is OrderState.SUBMITTED
    )
    assert {STATUS_MAP[s] for s in ("canceled", "expired", "replaced")} == {OrderState.CANCELLED}
    s = Server()
    s.on("GET", "/v2/orders/b-1", (200, order_json("weird_status")))
    with pytest.raises(BrokerError, match="unknown order status"):
        broker(s).get_order("b-1")


def test_submit_is_never_retried_on_timeout() -> None:
    s = Server()
    s.on("POST", "/v2/orders", httpx.ReadTimeout("slow"), (200, order_json()))
    b = broker(s)
    with pytest.raises(BrokerError, match="outcome unknown") as err:
        b.submit_order(req())
    assert "never resubmit" in str(err.value)
    assert [r.method for r in s.requests] == ["POST"]  # exactly one attempt


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (422, {"message": "client_order_id must be unique"}, DuplicateClientOrderId),
        (403, {"message": "insufficient buying power"}, OrderRejectedByBroker),
        (422, {"message": "qty must be > 0"}, OrderRejectedByBroker),
        (503, {"message": "unavailable"}, BrokerError),
    ],
)
def test_submit_error_mapping(status: int, body: dict[str, str], exc: type[Exception]) -> None:
    s = Server()
    s.on("POST", "/v2/orders", (status, body))
    with pytest.raises(exc):
        broker(s).submit_order(req())
    assert len(s.requests) == 1


def test_submit_body_and_on_close_orders() -> None:
    s = Server()
    s.on("POST", "/v2/orders", (200, order_json("accepted")))
    snap = broker(s).submit_order(req(OrderType.MARKET_ON_CLOSE))
    body = json.loads(s.requests[0].content)
    assert body == {
        "symbol": "TQQQ",
        "qty": "10",
        "side": "buy",
        "type": "market",
        "time_in_force": "cls",
        "client_order_id": "aq-1",
    }
    assert snap.state is OrderState.ACKNOWLEDGED and snap.broker_order_id == "b-1"


def test_reads_are_retried_and_lookup_by_client_id() -> None:
    s = Server()
    s.on("GET", "/v2/orders:by_client_order_id", (503, {}), (200, order_json("filled", "10", "50")))
    b = broker(s)
    snap = b.get_order_by_client_id("aq-1")
    assert snap is not None and snap.state is OrderState.FILLED and len(s.requests) == 2
    s2 = Server()  # 404: the broker never saw this order
    assert broker(s2).get_order_by_client_id("aq-zzz") is None


def test_auth_failure_is_readable_and_secret_free() -> None:
    s = Server()
    s.on("GET", "/v2/account", (401, {"message": "unauthorized"}))
    with pytest.raises(BrokerError) as err:
        broker(s).get_account()
    assert "ALPACA_API_KEY_ID" in str(err.value) and SECRET not in str(err.value)
