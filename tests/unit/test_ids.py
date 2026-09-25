from datetime import UTC, datetime

import pytest

from adaptive_quant.core.enums import OrderSide
from adaptive_quant.core.ids import client_order_id, new_run_id, trading_cycle_id


def test_client_order_id_is_deterministic_for_same_intent() -> None:
    """A retry or restart must regenerate the identical ID (broker de-duplicates)."""
    cycle = trading_cycle_id("2024-01-02", "paper")
    assert client_order_id(cycle, "TQQQ", OrderSide.BUY) == client_order_id(
        cycle, "tqqq", OrderSide.BUY
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (("2024-01-02", "TQQQ", OrderSide.BUY, 0), ("2024-01-03", "TQQQ", OrderSide.BUY, 0)),
        (("2024-01-02", "TQQQ", OrderSide.BUY, 0), ("2024-01-02", "SQQQ", OrderSide.BUY, 0)),
        (("2024-01-02", "TQQQ", OrderSide.BUY, 0), ("2024-01-02", "TQQQ", OrderSide.SELL, 0)),
        (("2024-01-02", "TQQQ", OrderSide.BUY, 0), ("2024-01-02", "TQQQ", OrderSide.BUY, 1)),
    ],
)
def test_client_order_id_differs_for_different_intents(
    a: tuple[str, str, OrderSide, int], b: tuple[str, str, OrderSide, int]
) -> None:
    def make(t: tuple[str, str, OrderSide, int]) -> str:
        return client_order_id(trading_cycle_id(t[0], "paper"), t[1], t[2], t[3])

    assert make(a) != make(b)


def test_client_order_id_is_broker_safe() -> None:
    cid = client_order_id(trading_cycle_id("2024-01-02", "paper"), "TQQQ", OrderSide.SELL, 12)
    assert cid.startswith("aq-TQQQ-s12-")
    assert len(cid) <= 48


@pytest.mark.parametrize("symbol", ["", "TQ QQ", "X;DROP"])
def test_invalid_symbol_rejected(symbol: str) -> None:
    with pytest.raises(ValueError, match="symbol"):
        client_order_id("cyc-paper-2024-01-02", symbol, OrderSide.BUY)


def test_negative_sequence_rejected() -> None:
    with pytest.raises(ValueError, match="sequence"):
        client_order_id("cyc", "QQQ", OrderSide.BUY, -1)


def test_cycle_id_validates_date() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        trading_cycle_id("01/02/2024", "paper")


def test_run_id_is_sortable_and_unique() -> None:
    now = datetime(2024, 1, 2, 20, 45, tzinfo=UTC)
    a, b = new_run_id(now), new_run_id(now)
    assert a != b
    assert a.startswith("run-20240102T204500Z-")
