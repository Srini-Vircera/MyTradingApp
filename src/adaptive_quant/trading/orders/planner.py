"""Order planner: approved target portfolio -> order requests (idempotent by construction).

::

    delta(symbol) = target_qty - broker_confirmed_position - remaining qty of open orders

so re-planning after a restart, or while orders are working, never duplicates
exposure (target 1,500, confirmed 1,000, open BUY 500 -> delta 0 -> no order).

Refusals (the whole plan, fail closed):
    account blocked; an UNKNOWN order (ours or the broker's); a broker open
    order we did not create; a non-terminal intent of ours the broker does not
    show; duplicate client order ids; a position in an unknown instrument; a
    missing or non-positive price for a symbol that must be traded.

Per-order rules:
    * quantities are rounded toward zero (whole shares unless fractional
      trading is allowed by config *and* broker);
    * deltas inside the rebalance band are skipped (full exits always go);
    * ``max_order_notional`` caps every order;
    * risk-increasing orders are skipped if the price moved more than
      ``max_price_deviation`` since the risk decision (spikes / large gaps) or
      no decision-time price is known;
    * buys are limited to (1 - buffer) x buying power (sell proceeds are not
      counted; the order manager re-plans after sells complete);
    * sells are sequenced before buys.

Only a :class:`TargetPortfolio` carrying a risk ``decision_id`` can be planned,
and the database refuses order intents whose decision is not stored.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from adaptive_quant.config.schema import RiskConfig, TradingConfig
from adaptive_quant.core.enums import OrderSide, OrderState, OrderType, TimeInForce
from adaptive_quant.core.ids import client_order_id
from adaptive_quant.core.models import (
    AccountSnapshot,
    Instrument,
    OrderRequest,
    OrderSnapshot,
    Position,
    TargetPortfolio,
)
from adaptive_quant.persistence.records import IntentView
from adaptive_quant.trading.orders.state_machine import TERMINAL_STATES
from adaptive_quant.trading.safety.preflight import RefusalReason

WHOLE = Decimal(1)
FRACTION = Decimal("0.000001")

SequenceAllocator = Callable[[str, OrderSide], int]


@dataclass(frozen=True)
class Skipped:
    symbol: str
    reason: str
    quantity: Decimal = Decimal(0)


@dataclass(frozen=True)
class OrderPlan:
    orders: tuple[OrderRequest, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    notes: tuple[str, ...] = ()
    refused: RefusalReason | None = None
    refusal_detail: str = ""
    expected_prices: Mapping[str, Decimal] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.refused is None


@dataclass(frozen=True)
class PlanningInput:
    cycle_id: str
    target: TargetPortfolio
    account: AccountSnapshot
    positions: Sequence[Position]
    broker_open_orders: Sequence[OrderSnapshot]
    internal_open: Sequence[IntentView]  # our persisted non-terminal intents
    prices: Mapping[str, Decimal]  # current quotes
    reference_prices: Mapping[str, Decimal]  # prices the risk decision was made with


class OrderPlanner:
    def __init__(
        self,
        trading: TradingConfig,
        risk: RiskConfig,
        instruments: Mapping[str, Instrument],
        broker_supports_fractional: bool,
    ) -> None:
        self.cfg = trading
        self.risk = risk
        self.instruments = dict(instruments)
        self.lot = (
            FRACTION if (trading.allow_fractional_shares and broker_supports_fractional) else WHOLE
        )

    def plan(self, inp: PlanningInput, sequence: SequenceAllocator) -> OrderPlan:
        refusal = self._refusal(inp)
        if refusal is not None:
            return OrderPlan(refused=refusal[0], refusal_detail=refusal[1])
        cfg = self.cfg
        equity = inp.account.equity
        if equity <= 0:
            return OrderPlan(
                refused=RefusalReason.EQUITY_UNCONFIRMED, refusal_detail=f"equity {equity}"
            )
        investable = equity * (1 - Decimal(str(cfg.sizing_cash_buffer)))
        confirmed = {p.symbol: p.quantity for p in inp.positions}
        pending: dict[str, Decimal] = {}
        for o in inp.broker_open_orders:
            rem = o.quantity - o.filled_quantity
            pending[o.symbol] = pending.get(o.symbol, Decimal(0)) + (
                rem if o.side is OrderSide.BUY else -rem
            )
        net_now = sum(
            (
                float(q * inp.prices.get(s, Decimal(0))) * self.instruments[s].leverage
                for s, q in confirmed.items()
            ),
            0.0,
        ) / float(equity)

        symbols = sorted(set(inp.target.weights) | set(confirmed) | set(pending))
        skipped: list[Skipped] = []
        notes: list[str] = []
        drafts: list[tuple[str, OrderSide, Decimal, bool]] = []
        for sym in symbols:
            inst = self.instruments.get(sym)
            if inst is None:
                return OrderPlan(
                    refused=RefusalReason.RISK_CALCULATION_FAILURE,
                    refusal_detail=f"target or open order in unknown instrument {sym}",
                )
            weight = inp.target.weights.get(sym, Decimal(0))
            if not inst.tradeable:
                if weight > 0:
                    return OrderPlan(
                        refused=RefusalReason.RISK_CALCULATION_FAILURE,
                        refusal_detail=f"target holds non-tradeable {sym}",
                    )
                continue
            price = inp.prices.get(sym)
            if price is None or price <= 0:
                return OrderPlan(
                    refused=RefusalReason.MARKET_DATA_MISSING,
                    refusal_detail=f"no valid price for {sym}",
                )
            target_qty = (weight * investable / price).quantize(self.lot, ROUND_DOWN)
            delta = target_qty - confirmed.get(sym, Decimal(0)) - pending.get(sym, Decimal(0))
            delta = (
                delta.quantize(self.lot, ROUND_DOWN)
                if delta > 0
                else -((-delta).quantize(self.lot, ROUND_DOWN))
            )
            if delta == 0:
                continue
            if target_qty > 0 and abs(delta) * price / equity < Decimal(
                str(cfg.rebalance_threshold_weight)
            ):
                skipped.append(Skipped(sym, "within rebalance band", abs(delta)))
                continue
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            qty = abs(delta)
            risk_increasing = side is OrderSide.BUY or inst.leverage * net_now <= 0
            if risk_increasing:
                ref = inp.reference_prices.get(sym)
                if ref is None or ref <= 0:
                    skipped.append(
                        Skipped(sym, "no decision-time reference price (risk-increasing)", qty)
                    )
                    continue
                move = abs(price / ref - 1)
                if move > Decimal(str(cfg.max_price_deviation)):
                    skipped.append(
                        Skipped(sym, f"price moved {move:.1%} since the risk decision", qty)
                    )
                    continue
            cap = (Decimal(str(self.risk.max_order_notional)) / price).quantize(
                self.lot, ROUND_DOWN
            )
            if qty > cap:
                notes.append(f"{sym}: {qty} capped at {cap} (max_order_notional)")
                qty = cap
            if qty <= 0:
                skipped.append(Skipped(sym, "rounds to zero", abs(delta)))
                continue
            drafts.append((sym, side, qty, risk_increasing))

        # buying power: sells never need it; buys are scaled into it (rounded toward zero)
        bp = inp.account.buying_power * (1 - Decimal(str(cfg.buying_power_buffer)))
        buy_value = sum(
            (q * inp.prices[s] for s, side, q, _ in drafts if side is OrderSide.BUY), Decimal(0)
        )
        if buy_value > 0 and buy_value > bp:
            k = max(bp, Decimal(0)) / buy_value
            scaled = []
            for s, side, q, ri in drafts:
                if side is OrderSide.BUY:
                    new_q = (q * k).quantize(self.lot, ROUND_DOWN)
                    if new_q <= 0:
                        skipped.append(Skipped(s, "insufficient buying power", q))
                        continue
                    notes.append(f"{s}: buy {q} reduced to {new_q} (buying power)")
                    q = new_q
                scaled.append((s, side, q, ri))
            drafts = scaled

        drafts.sort(key=lambda d: (d[1] is OrderSide.BUY, d[0]))  # sells first
        orders = tuple(
            OrderRequest(
                client_order_id=client_order_id(inp.cycle_id, s, side, sequence(s, side)),
                symbol=s,
                side=side,
                quantity=q,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                risk_increasing=ri,
                decision_id=inp.target.decision_id,
            )
            for s, side, q, ri in drafts
        )
        return OrderPlan(
            orders=orders,
            skipped=tuple(skipped),
            notes=tuple(notes),
            expected_prices={o.symbol: inp.prices[o.symbol] for o in orders},
        )

    def _refusal(self, inp: PlanningInput) -> tuple[RefusalReason, str] | None:
        if inp.account.trading_blocked:
            return RefusalReason.BROKER_UNAVAILABLE, "broker reports the account is blocked"
        unknown = [
            i.client_order_id for i in inp.internal_open if i.state == OrderState.UNKNOWN.value
        ]
        unknown += [
            o.client_order_id for o in inp.broker_open_orders if o.state is OrderState.UNKNOWN
        ]
        if unknown:
            return RefusalReason.UNCERTAIN_OPEN_ORDERS, f"orders in UNKNOWN state: {unknown}"
        ids = [o.client_order_id for o in inp.broker_open_orders]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            return RefusalReason.DUPLICATE_ORDERS, f"duplicate client order ids at broker: {dupes}"
        ours = {i.client_order_id for i in inp.internal_open}
        foreign = sorted(set(ids) - ours)
        if foreign:
            return (
                RefusalReason.UNCERTAIN_OPEN_ORDERS,
                f"broker open orders not created by us: {foreign}",
            )
        unseen = sorted(
            i.client_order_id
            for i in inp.internal_open
            if OrderState(i.state) not in TERMINAL_STATES and i.client_order_id not in set(ids)
        )
        if unseen:
            return (
                RefusalReason.UNCERTAIN_OPEN_ORDERS,
                f"our open orders not seen at the broker (run recovery): {unseen}",
            )
        strange = sorted(p.symbol for p in inp.positions if p.symbol not in self.instruments)
        if strange:
            return (
                RefusalReason.POSITIONS_UNCONFIRMED,
                f"positions in unknown instruments: {strange}",
            )
        return None
