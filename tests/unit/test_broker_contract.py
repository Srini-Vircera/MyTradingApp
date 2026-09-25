from datetime import UTC, datetime
from decimal import Decimal

import pytest

from adaptive_quant.core.enums import OrderSide, OrderState, TradingMode
from adaptive_quant.core.errors import SafetyViolation
from adaptive_quant.core.models import (
    AccountSnapshot,
    MarketClock,
    OrderRequest,
    OrderSnapshot,
    Position,
)
from adaptive_quant.trading.brokers.base import (
    BrokerAdapter,
    BrokerCapabilities,
    TradableAsset,
    assert_broker_matches_mode,
)

NOW = datetime(2024, 1, 2, 20, 0, tzinfo=UTC)


class FakeBroker(BrokerAdapter):
    def __init__(self, is_live: bool) -> None:
        self._caps = BrokerCapabilities("fake", is_live, True, True, False)

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._caps

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            as_of=NOW,
            equity=Decimal(100_000),
            cash=Decimal(40_000),
            buying_power=Decimal(80_000),
            is_paper=not self._caps.is_live,
        )

    def get_positions(self) -> list[Position]:
        return [
            Position(
                symbol="TQQQ",
                quantity=Decimal(100),
                avg_entry_price=Decimal(50),
                market_value=Decimal(6000),
            )
        ]

    def get_open_orders(self) -> list[OrderSnapshot]:
        return []

    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        return OrderSnapshot(
            client_order_id=request.client_order_id,
            broker_order_id="b1",
            symbol=request.symbol,
            side=OrderSide.BUY,
            quantity=request.quantity,
            state=OrderState.ACKNOWLEDGED,
            updated_at=NOW,
        )

    def cancel_order(self, broker_order_id: str) -> None:
        return None

    def get_order(self, broker_order_id: str) -> OrderSnapshot:
        raise NotImplementedError

    def get_order_by_client_id(self, client_order_id: str) -> OrderSnapshot | None:
        return None

    def get_market_clock(self) -> MarketClock:
        return MarketClock(as_of=NOW, is_open=True, next_open=NOW, next_close=NOW)

    def get_tradeable_assets(self, symbols: list[str]) -> dict[str, TradableAsset]:
        return {s: TradableAsset(s, True, True, False) for s in symbols}


def test_default_helpers() -> None:
    broker = FakeBroker(is_live=False)
    assert broker.get_buying_power() == Decimal(80_000)
    assert broker.reconcile_positions() == {"TQQQ": Decimal(100)}


@pytest.mark.parametrize("mode", [TradingMode.PAPER, TradingMode.SHADOW, TradingMode.BACKTEST])
def test_live_broker_refused_outside_live_mode(mode: TradingMode) -> None:
    with pytest.raises(SafetyViolation, match="LIVE"):
        assert_broker_matches_mode(FakeBroker(is_live=True), mode)


def test_live_mode_requires_live_broker() -> None:
    with pytest.raises(SafetyViolation, match="not a live adapter"):
        assert_broker_matches_mode(FakeBroker(is_live=False), TradingMode.LIVE)


def test_matching_pairs_pass() -> None:
    assert_broker_matches_mode(FakeBroker(is_live=False), TradingMode.PAPER)
    assert_broker_matches_mode(FakeBroker(is_live=True), TradingMode.LIVE)
