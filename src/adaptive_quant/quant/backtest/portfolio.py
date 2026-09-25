"""Simulated portfolio ledger with exact Decimal accounting.

Conventions
-----------
* Cash and every cash flow are in cents (banker's rounding).
* Positions are FIFO lots. A lot's cost is the cash paid for its shares
  (``notional``, which already includes spread/slippage/impact); commissions
  are tracked separately.
* Realized P&L (FIFO) = sell notional - cost of the lots consumed.
* The ledger identity holds exactly at all times::

      equity = initial_cash + realized + unrealized - commissions + interest
      equity = cash + market_value

* Long-only, no margin: cash and quantities can never go negative
  (violations raise :class:`AccountingError` - they are bugs, not events).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from adaptive_quant.core.enums import OrderSide
from adaptive_quant.core.errors import AQError
from adaptive_quant.core.money import CENT
from adaptive_quant.quant.backtest.costs import FillQuote

ZERO = Decimal(0)


class AccountingError(AQError):
    """An accounting invariant was violated (always a bug)."""


@dataclass
class Lot:
    quantity: Decimal
    cost: Decimal  # cents paid for these shares
    commission: Decimal  # buy commission attributable to these shares
    opened: datetime


@dataclass(frozen=True)
class Fill:
    order_id: int
    time: datetime
    decision_time: datetime
    data_timestamp: datetime
    symbol: str
    side: OrderSide
    requested_quantity: Decimal
    quantity: Decimal
    ref_price: Decimal
    fill_price: Decimal
    notional: Decimal
    commission: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    impact_cost: Decimal
    limited_by: str = ""  # "", "participation", "cash"


@dataclass(frozen=True)
class RoundTrip:
    symbol: str
    quantity: Decimal
    entry_time: datetime
    exit_time: datetime
    cost: Decimal
    proceeds: Decimal
    commissions: Decimal
    pnl: Decimal  # proceeds - cost - commissions (net of all costs)

    @property
    def return_pct(self) -> float:
        return float(self.pnl / self.cost) if self.cost else 0.0

    @property
    def holding_days(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 86_400


@dataclass
class Portfolio:
    initial_cash: Decimal
    cash: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    commissions: Decimal = ZERO
    implicit_costs: Decimal = ZERO
    interest: Decimal = ZERO
    lots: dict[str, deque[Lot]] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    round_trips: list[RoundTrip] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise AccountingError("initial cash must be positive")
        self.initial_cash = self.initial_cash.quantize(CENT)
        self.cash = self.initial_cash

    # ------------------------------------------------------------ queries
    def quantity(self, symbol: str) -> Decimal:
        return sum((lot.quantity for lot in self.lots.get(symbol, ())), ZERO)

    def cost_basis(self, symbol: str) -> Decimal:
        return sum((lot.cost for lot in self.lots.get(symbol, ())), ZERO)

    def symbols(self) -> list[str]:
        return sorted(s for s, lots in self.lots.items() if lots)

    def market_value(self, prices: dict[str, Decimal]) -> Decimal:
        total = ZERO
        for sym in self.symbols():
            if sym not in prices:
                raise AccountingError(f"no mark price for held position {sym}")
            total += self.quantity(sym) * prices[sym]
        return total

    def equity(self, prices: dict[str, Decimal]) -> Decimal:
        return self.cash + self.market_value(prices)

    def unrealized_pnl(self, prices: dict[str, Decimal]) -> Decimal:
        return self.market_value(prices) - sum((self.cost_basis(s) for s in self.symbols()), ZERO)

    # ------------------------------------------------------------ mutations
    def apply(self, fill: Fill, quote: FillQuote) -> None:
        if fill.quantity <= 0:
            raise AccountingError("fills must have positive quantity")
        if fill.side is OrderSide.BUY:
            self._buy(fill)
        else:
            self._sell(fill)
        self.commissions += fill.commission
        self.implicit_costs += quote.implicit_cost
        self.fills.append(fill)
        self._check()

    def accrue_interest(self, amount: Decimal) -> None:
        amount = amount.quantize(CENT)
        if amount < 0:
            raise AccountingError("negative interest")
        self.cash += amount
        self.interest += amount

    def _buy(self, f: Fill) -> None:
        outflow = f.notional + f.commission
        if outflow > self.cash:
            raise AccountingError(f"buy of {f.symbol} needs {outflow} but cash is {self.cash}")
        self.cash -= outflow
        self.lots.setdefault(f.symbol, deque()).append(
            Lot(f.quantity, f.notional, f.commission, f.time)
        )

    def _sell(self, f: Fill) -> None:
        lots = self.lots.get(f.symbol, deque())
        held = sum((lot.quantity for lot in lots), ZERO)
        if f.quantity > held:
            raise AccountingError(f"sell of {f.quantity} {f.symbol} exceeds holding {held}")
        self.cash += f.notional - f.commission
        remaining = f.quantity
        proceeds_left, sell_comm_left = f.notional, f.commission
        while remaining > 0:
            lot = lots[0]
            take = min(remaining, lot.quantity)
            full_lot = take == lot.quantity
            closing_all = take == remaining
            cost = lot.cost if full_lot else (lot.cost * take / lot.quantity).quantize(CENT)
            buy_comm = (
                lot.commission
                if full_lot
                else (lot.commission * take / lot.quantity).quantize(CENT)
            )
            proceeds = (
                proceeds_left if closing_all else (f.notional * take / f.quantity).quantize(CENT)
            )
            sell_comm = (
                sell_comm_left if closing_all else (f.commission * take / f.quantity).quantize(CENT)
            )
            proceeds_left -= proceeds
            sell_comm_left -= sell_comm
            self.realized_pnl += proceeds - cost
            self.round_trips.append(
                RoundTrip(
                    symbol=f.symbol,
                    quantity=take,
                    entry_time=lot.opened,
                    exit_time=f.time,
                    cost=cost,
                    proceeds=proceeds,
                    commissions=buy_comm + sell_comm,
                    pnl=proceeds - cost - buy_comm - sell_comm,
                )
            )
            if full_lot:
                lots.popleft()
            else:
                lot.quantity -= take
                lot.cost -= cost
                lot.commission -= buy_comm
            remaining -= take

    def _check(self) -> None:
        if self.cash < 0:
            raise AccountingError(f"cash went negative: {self.cash}")
        for sym, lots in self.lots.items():
            for lot in lots:
                if lot.quantity <= 0 or lot.cost < 0:
                    raise AccountingError(f"invalid lot for {sym}: {lot}")

    def identity_gap(self, prices: dict[str, Decimal]) -> Decimal:
        """equity - (initial + realized + unrealized - commissions + interest). Must be 0."""
        rhs = (
            self.initial_cash
            + self.realized_pnl
            + self.unrealized_pnl(prices)
            - self.commissions
            + self.interest
        )
        return self.equity(prices) - rhs
