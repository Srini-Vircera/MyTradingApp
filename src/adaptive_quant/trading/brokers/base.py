"""Broker adapter contract.

Strategies, the ensemble and the risk engine never import this module: they
produce a :class:`~adaptive_quant.core.models.TargetPortfolio`. Only the order
manager talks to a broker, and only through this interface, so Alpaca, Interactive
Brokers or Schwab adapters are interchangeable.

Error contract for implementations
----------------------------------
* Transport failures (timeout, disconnect, 5xx) raise :class:`BrokerError`.
  For ``submit_order`` the caller must then treat the order as UNKNOWN and
  investigate with :meth:`get_order_by_client_id` - never resubmit blindly.
* A broker that rejects a duplicate ``client_order_id`` must surface that as
  :class:`DuplicateClientOrderId` so the caller can look up the original.
* A definitive refusal (validation, buying power) is :class:`OrderRejectedByBroker`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from adaptive_quant.core.enums import TradingMode
from adaptive_quant.core.errors import BrokerError, SafetyViolation
from adaptive_quant.core.models import (
    AccountSnapshot,
    MarketClock,
    OrderRequest,
    OrderSnapshot,
    Position,
)


class DuplicateClientOrderId(BrokerError):
    """The broker already has an order with this client_order_id."""


class OrderRejectedByBroker(BrokerError):
    """The broker definitively refused the order (e.g. insufficient buying power).

    Unlike a transport failure the outcome is known: the order is REJECTED and
    is never retried automatically.
    """


@dataclass(frozen=True)
class BrokerCapabilities:
    name: str
    is_live: bool
    supports_fractional: bool
    supports_client_order_id: bool
    supports_closing_auction: bool


@dataclass(frozen=True)
class TradableAsset:
    symbol: str
    tradable: bool
    fractionable: bool
    shortable: bool


class BrokerAdapter(ABC):
    """Common interface for all brokers."""

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities: ...

    @abstractmethod
    def get_account(self) -> AccountSnapshot: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_open_orders(self) -> list[OrderSnapshot]: ...

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> OrderSnapshot: ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    def get_order(self, broker_order_id: str) -> OrderSnapshot: ...

    @abstractmethod
    def get_order_by_client_id(self, client_order_id: str) -> OrderSnapshot | None:
        """Look up an order by our idempotency key; ``None`` if the broker never saw it."""

    @abstractmethod
    def get_market_clock(self) -> MarketClock: ...

    @abstractmethod
    def get_tradeable_assets(self, symbols: list[str]) -> dict[str, TradableAsset]: ...

    def get_buying_power(self) -> Decimal:
        return self.get_account().buying_power

    def reconcile_positions(self) -> dict[str, Decimal]:
        """Broker-confirmed quantities by symbol (the reconciliation input)."""
        return {p.symbol: p.quantity for p in self.get_positions()}


def assert_broker_matches_mode(broker: BrokerAdapter, mode: TradingMode) -> None:
    """Refuse to pair a live broker with a non-live mode, or vice versa."""
    is_live = broker.capabilities.is_live
    if is_live and not mode.uses_real_money:
        raise SafetyViolation(
            f"broker {broker.capabilities.name!r} is LIVE but trading mode is {mode}",
            hint="use a paper broker adapter for paper/shadow modes",
        )
    if mode.uses_real_money and not is_live:
        raise SafetyViolation(
            f"trading mode is live but broker {broker.capabilities.name!r} is not a live adapter"
        )
