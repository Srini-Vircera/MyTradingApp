"""In-memory broker with fault injection (tests, shadow rehearsals, drills).

It behaves like a cash (no margin, no shorting) paper account: buys need
buying power, sells need a position, and ``client_order_id`` is an idempotency
key. Faults are queued per call and consumed in order:

* ``"timeout_after_accept"`` - the broker accepts (and may fill) the order but the
  response is lost: ``submit_order`` raises :class:`BrokerError`;
* ``"timeout_before_accept"`` - the request never reaches the broker;
* ``"reject"`` - a definitive refusal (:class:`OrderRejectedByBroker`);
* ``"unavailable"`` (``set_unavailable``) - every call fails with ``BrokerError``.

Fill behaviour: ``fill_ratio`` of each order fills immediately at the current
price (± ``slippage_bps``); the rest stays open until :meth:`fill_open_orders`.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from adaptive_quant.core.clock import Clock
from adaptive_quant.core.enums import OrderSide, OrderState
from adaptive_quant.core.errors import BrokerError
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
    DuplicateClientOrderId,
    OrderRejectedByBroker,
    TradableAsset,
)

CENT = Decimal("0.01")


@dataclass
class _Order:
    broker_order_id: str
    request: OrderRequest
    state: OrderState
    updated_at: datetime
    filled: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)

    def snapshot(self) -> OrderSnapshot:
        r = self.request
        return OrderSnapshot(
            client_order_id=r.client_order_id,
            broker_order_id=self.broker_order_id,
            symbol=r.symbol,
            side=r.side,
            quantity=r.quantity,
            filled_quantity=self.filled,
            avg_fill_price=(self.notional / self.filled).quantize(Decimal("0.000001"))
            if self.filled
            else None,
            state=self.state,
            updated_at=self.updated_at,
        )


@dataclass
class SimulatedBroker(BrokerAdapter):
    clock: Clock
    prices: dict[str, Decimal]
    cash: Decimal = Decimal(100_000)
    positions: dict[str, Decimal] = field(default_factory=dict)
    fill_ratio: Decimal = Decimal(1)
    slippage_bps: Decimal = Decimal(0)
    fractional: bool = True
    trading_blocked: bool = False
    tradeable: frozenset[str] = frozenset({"QQQ", "TQQQ", "SQQQ", "SPY"})

    def __post_init__(self) -> None:
        self._orders: dict[str, _Order] = {}
        self._by_client: dict[str, str] = {}
        self._faults: deque[str] = deque()
        self._ids = itertools.count(1)
        self._unavailable = False
        self.submit_calls = 0  # every submit_order invocation, for "never resubmit" assertions
        self.cost_basis: dict[str, Decimal] = {}

    # ------------------------------------------------------------------ fault injection
    def inject(self, *faults: str) -> None:
        allowed = {"timeout_after_accept", "timeout_before_accept", "reject"}
        for f in faults:
            if f not in allowed:
                raise ValueError(f"unknown fault {f!r}")
            self._faults.append(f)

    def set_unavailable(self, unavailable: bool = True) -> None:
        self._unavailable = unavailable

    def set_price(self, symbol: str, price: Decimal) -> None:
        self.prices[symbol] = price

    def inject_position(self, symbol: str, qty: Decimal) -> None:
        """A position the platform did not create (e.g. a manual trade)."""
        self.positions[symbol] = self.positions.get(symbol, Decimal(0)) + qty

    # ------------------------------------------------------------------ contract
    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities("simulated", False, self.fractional, True, True)

    def _up(self) -> None:
        if self._unavailable:
            raise BrokerError("simulated broker unavailable (injected)")

    def get_account(self) -> AccountSnapshot:
        self._up()
        return AccountSnapshot(
            as_of=self.clock.now(),
            equity=self.equity(),
            cash=self.cash,
            buying_power=self.buying_power(),
            is_paper=True,
            trading_blocked=self.trading_blocked,
        )

    def equity(self) -> Decimal:
        return (
            self.cash + sum((q * self.prices[s] for s, q in self.positions.items()), Decimal(0))
        ).quantize(CENT)

    def buying_power(self) -> Decimal:
        reserved = sum(
            (
                (o.request.quantity - o.filled) * self.prices[o.request.symbol]
                for o in self._orders.values()
                if o.request.side is OrderSide.BUY and o.state in _OPEN
            ),
            Decimal(0),
        )
        return max(self.cash - reserved, Decimal(0)).quantize(CENT)

    def get_positions(self) -> list[Position]:
        self._up()
        return [
            Position(
                symbol=s,
                quantity=q,
                avg_entry_price=(self.cost_basis.get(s, q * self.prices[s]) / q).quantize(CENT)
                if q
                else Decimal(0),
                market_value=(q * self.prices[s]).quantize(CENT),
            )
            for s, q in sorted(self.positions.items())
            if q != 0
        ]

    def get_open_orders(self) -> list[OrderSnapshot]:
        self._up()
        return [o.snapshot() for o in self._orders.values() if o.state in _OPEN]

    def get_order(self, broker_order_id: str) -> OrderSnapshot:
        self._up()
        if broker_order_id not in self._orders:
            raise BrokerError(f"no order {broker_order_id}")
        return self._orders[broker_order_id].snapshot()

    def get_order_by_client_id(self, client_order_id: str) -> OrderSnapshot | None:
        self._up()
        bid = self._by_client.get(client_order_id)
        return None if bid is None else self._orders[bid].snapshot()

    def get_market_clock(self) -> MarketClock:
        now = self.clock.now()
        return MarketClock(
            as_of=now,
            is_open=True,
            next_open=now + timedelta(days=1),
            next_close=now + timedelta(hours=1),
        )

    def get_tradeable_assets(self, symbols: list[str]) -> dict[str, TradableAsset]:
        self._up()
        return {s: TradableAsset(s, s in self.tradeable, self.fractional, False) for s in symbols}

    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        self.submit_calls += 1
        self._up()
        fault = self._faults.popleft() if self._faults else None
        if fault == "timeout_before_accept":
            raise BrokerError(
                f"timeout submitting {request.client_order_id} (injected, never reached broker)"
            )
        if request.client_order_id in self._by_client:
            raise DuplicateClientOrderId(f"duplicate client_order_id {request.client_order_id}")
        if fault == "reject":
            raise OrderRejectedByBroker(f"{request.client_order_id} rejected (injected)")
        self._validate(request)
        order = _Order(
            f"sim-{next(self._ids)}", request, OrderState.ACKNOWLEDGED, updated_at=self.clock.now()
        )
        self._orders[order.broker_order_id] = order
        self._by_client[request.client_order_id] = order.broker_order_id
        self._fill(order, (request.quantity * self.fill_ratio).quantize(self._lot(), ROUND_DOWN))
        if fault == "timeout_after_accept":
            raise BrokerError(
                f"timeout waiting for response to {request.client_order_id} (injected)"
            )
        return order.snapshot()

    def cancel_order(self, broker_order_id: str) -> None:
        self._up()
        o = self._orders.get(broker_order_id)
        if o is not None and o.state in _OPEN:
            o.state = OrderState.CANCELLED
            o.updated_at = self.clock.now()

    # ------------------------------------------------------------------ simulation
    def fill_open_orders(self) -> None:
        for o in self._orders.values():
            if o.state in _OPEN:
                self._fill(o, o.request.quantity - o.filled)

    def _lot(self) -> Decimal:
        return Decimal("0.000001") if self.fractional else Decimal(1)

    def _price(self, req: OrderRequest) -> Decimal:
        p = self.prices[req.symbol]
        k = self.slippage_bps / Decimal(10_000)
        return (p * (1 + k) if req.side is OrderSide.BUY else p * (1 - k)).quantize(
            Decimal("0.000001")
        )

    def _validate(self, req: OrderRequest) -> None:
        if self.trading_blocked:
            raise OrderRejectedByBroker("account is blocked from trading")
        if req.symbol not in self.tradeable or req.symbol not in self.prices:
            raise OrderRejectedByBroker(f"{req.symbol} is not tradeable")
        if not self.fractional and req.quantity != req.quantity.to_integral_value():
            raise OrderRejectedByBroker("fractional quantities are not supported")
        if req.side is OrderSide.BUY:
            if req.quantity * self._price(req) > self.buying_power():
                raise OrderRejectedByBroker(f"insufficient buying power for {req.client_order_id}")
        else:
            reserved = sum(
                (
                    o.request.quantity - o.filled
                    for o in self._orders.values()
                    if o.request.symbol == req.symbol
                    and o.request.side is OrderSide.SELL
                    and o.state in _OPEN
                ),
                Decimal(0),
            )
            if req.quantity > self.positions.get(req.symbol, Decimal(0)) - reserved:
                raise OrderRejectedByBroker(
                    f"insufficient position to sell {req.symbol} (no shorting)"
                )

    def _fill(self, o: _Order, qty: Decimal) -> None:
        if qty <= 0:
            return
        price = self._price(o.request)
        sym, value = o.request.symbol, qty * price
        if o.request.side is OrderSide.BUY:
            self.cash -= value.quantize(CENT)
            self.positions[sym] = self.positions.get(sym, Decimal(0)) + qty
            self.cost_basis[sym] = self.cost_basis.get(sym, Decimal(0)) + value
        else:
            self.cash += value.quantize(CENT)
            held = self.positions.get(sym, Decimal(0))
            if held:
                self.cost_basis[sym] = self.cost_basis.get(sym, Decimal(0)) * (held - qty) / held
            self.positions[sym] = held - qty
        o.filled += qty
        o.notional += value
        o.state = (
            OrderState.FILLED if o.filled >= o.request.quantity else OrderState.PARTIALLY_FILLED
        )
        o.updated_at = self.clock.now()


_OPEN = frozenset({OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED})
