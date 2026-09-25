"""Order planner: idempotent deltas, refusals, caps, buying power, price guards."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from adaptive_quant.config.schema import TradingConfig
from adaptive_quant.core.enums import OrderSide, OrderState
from adaptive_quant.core.models import AccountSnapshot, OrderSnapshot, Position, TargetPortfolio
from adaptive_quant.persistence.records import IntentView
from adaptive_quant.trading.orders.planner import OrderPlanner, PlanningInput
from adaptive_quant.trading.safety.preflight import RefusalReason
from tests.unit.backtest.helpers import SETTINGS

NOW = datetime(2024, 1, 2, 20, 45, tzinfo=UTC)
PRICES = {"QQQ": Decimal("400"), "TQQQ": Decimal("50"), "SQQQ": Decimal("20")}


def planner(fractional: bool = False, **cfg: Any) -> OrderPlanner:
    trading = TradingConfig.model_validate(
        {
            **SETTINGS.trading.model_dump(),
            "allow_fractional_shares": fractional,
            "sizing_cash_buffer": 0.0,
            **cfg,
        }
    )
    return OrderPlanner(
        trading, SETTINGS.risk, SETTINGS.universe.by_symbol, broker_supports_fractional=True
    )


def account(equity: str = "100000", bp: str = "100000", blocked: bool = False) -> AccountSnapshot:
    return AccountSnapshot(
        as_of=NOW,
        equity=Decimal(equity),
        cash=Decimal(bp),
        buying_power=Decimal(bp),
        is_paper=True,
        trading_blocked=blocked,
    )


def target(**w: str) -> TargetPortfolio:
    return TargetPortfolio(
        as_of=NOW,
        weights={k: Decimal(v) for k, v in w.items()},
        decision_id="dec-1",
        config_version="cfg",
    )


def pos(sym: str, qty: str) -> Position:
    q = Decimal(qty)
    return Position(
        symbol=sym, quantity=q, avg_entry_price=PRICES[sym], market_value=q * PRICES[sym]
    )


def open_order(
    sym: str,
    qty: str,
    side: OrderSide = OrderSide.BUY,
    cid: str = "aq-open",
    state: OrderState = OrderState.ACKNOWLEDGED,
    filled: str = "0",
) -> OrderSnapshot:
    return OrderSnapshot(
        client_order_id=cid,
        broker_order_id="b",
        symbol=sym,
        side=side,
        quantity=Decimal(qty),
        filled_quantity=Decimal(filled),
        state=state,
        updated_at=NOW,
    )


def intent(cid: str = "aq-open", state: str = "acknowledged", sym: str = "TQQQ") -> IntentView:
    return IntentView(cid, "cyc", "dec-1", sym, "buy", Decimal(1), state, None, True)


def plan(p: OrderPlanner, tgt: TargetPortfolio, **kw: Any):  # type: ignore[no-untyped-def]
    inp = PlanningInput(
        cycle_id="cyc-paper-2024-01-02",
        target=tgt,
        account=kw.pop("account", account()),
        positions=kw.pop("positions", []),
        broker_open_orders=kw.pop("open_orders", []),
        internal_open=kw.pop("internal", []),
        prices=kw.pop("prices", PRICES),
        reference_prices=kw.pop("reference", PRICES),
    )
    return p.plan(inp, lambda sym, side: 0)


def test_the_1500_1000_500_example_yields_no_order() -> None:
    # target 1,500 TQQQ (75,000 of 100,000 equity), 1,000 held, BUY 500 open
    r = plan(
        planner(),
        target(TQQQ="0.75"),
        positions=[pos("TQQQ", "1000")],
        open_orders=[open_order("TQQQ", "500")],
        internal=[intent()],
    )
    assert r.ok and r.orders == ()


def test_partial_fills_count_only_the_remaining_quantity() -> None:
    r = plan(
        planner(),
        target(TQQQ="0.75"),
        positions=[pos("TQQQ", "1200")],
        open_orders=[open_order("TQQQ", "500", filled="200")],
        internal=[intent()],
    )
    assert r.ok and r.orders == ()


def test_sells_before_buys_whole_shares_and_risk_flags() -> None:
    r = plan(planner(), target(QQQ="0.5"), positions=[pos("TQQQ", "600")])
    assert [(o.symbol, o.side, o.quantity) for o in r.orders] == [
        ("TQQQ", OrderSide.SELL, Decimal("600")),
        ("QQQ", OrderSide.BUY, Decimal("125")),
    ]
    sell, buy = r.orders
    assert not sell.risk_increasing and buy.risk_increasing
    assert sell.decision_id == "dec-1" and sell.client_order_id.startswith("aq-TQQQ-s0-")
    assert r.expected_prices == {"TQQQ": Decimal("50"), "QQQ": Decimal("400")}


def test_rounding_is_toward_zero_and_fractional_when_allowed() -> None:
    assert plan(planner(), target(QQQ="0.333")).orders[0].quantity == Decimal("83")  # 83.25 -> 83
    assert plan(planner(fractional=True), target(QQQ="0.333")).orders[0].quantity == Decimal(
        "83.25"
    )


def test_rebalance_band_skips_small_changes_but_never_exits() -> None:
    r = plan(
        planner(), target(QQQ="0.5"), positions=[pos("QQQ", "123")]
    )  # 5 shares = 2% -> 0.5% change
    assert r.orders == () and r.skipped[0].reason == "within rebalance band"
    exit_all = plan(planner(), target(), positions=[pos("QQQ", "1")])  # tiny, but a full exit
    assert [(o.side, o.quantity) for o in exit_all.orders] == [(OrderSide.SELL, Decimal("1"))]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"internal": [intent(state="unknown")]}, RefusalReason.UNCERTAIN_OPEN_ORDERS),
        (
            {"open_orders": [open_order("TQQQ", "5", cid="manual-1")]},
            RefusalReason.UNCERTAIN_OPEN_ORDERS,
        ),
        ({"internal": [intent(state="submitted")]}, RefusalReason.UNCERTAIN_OPEN_ORDERS),
        (
            {
                "open_orders": [open_order("TQQQ", "5", cid="x"), open_order("TQQQ", "5", cid="x")],
                "internal": [intent("x")],
            },
            RefusalReason.DUPLICATE_ORDERS,
        ),
        ({"account": account(blocked=True)}, RefusalReason.BROKER_UNAVAILABLE),
        ({"account": account(equity="0")}, RefusalReason.EQUITY_UNCONFIRMED),
        (
            {"prices": {"QQQ": Decimal("0"), **{k: v for k, v in PRICES.items() if k != "QQQ"}}},
            RefusalReason.MARKET_DATA_MISSING,
        ),
    ],
)
def test_uncertain_state_refuses_the_whole_plan(kw: dict[str, Any], reason: RefusalReason) -> None:
    r = plan(planner(), target(QQQ="0.5"), **kw)
    assert r.refused is reason and r.orders == ()


def test_unknown_instrument_position_refuses() -> None:
    odd = Position(
        symbol="ARKK", quantity=Decimal(1), avg_entry_price=Decimal(1), market_value=Decimal(1)
    )
    assert (
        plan(planner(), target(QQQ="0.5"), positions=[odd]).refused
        is RefusalReason.POSITIONS_UNCONFIRMED
    )


def test_zero_buying_power_skips_buys_but_not_sells() -> None:
    r = plan(planner(), target(QQQ="0.5"), positions=[pos("TQQQ", "100")], account=account(bp="0"))
    assert [(o.symbol, o.side) for o in r.orders] == [("TQQQ", OrderSide.SELL)]
    assert any(s.reason == "insufficient buying power" for s in r.skipped)


def test_insufficient_buying_power_scales_buys_down() -> None:
    r = plan(planner(), target(QQQ="0.5"), account=account(bp="20000"))
    (o,) = r.orders
    assert o.quantity == Decimal("49")  # 20,000 x 0.99 / 400 = 49.5 -> 49
    assert any("buying power" in n for n in r.notes)


def test_price_spike_blocks_risk_increasing_orders_only() -> None:
    spiked = {**PRICES, "TQQQ": Decimal("56")}  # +12% since the decision
    r = plan(planner(), target(TQQQ="0.3"), prices=spiked)
    assert r.orders == () and "price moved 12.0%" in r.skipped[0].reason
    r = plan(
        planner(), target(), positions=[pos("TQQQ", "100")], prices=spiked
    )  # reducing: allowed
    assert [(o.side, o.quantity) for o in r.orders] == [(OrderSide.SELL, Decimal("100"))]
    missing_ref = plan(planner(), target(TQQQ="0.3"), reference={})
    assert missing_ref.orders == () and "reference price" in missing_ref.skipped[0].reason


def test_large_gap_resizes_on_current_price_and_notional_cap() -> None:
    gapped = {**PRICES, "QQQ": Decimal("392")}  # -2% gap: inside tolerance, sized on 392
    r = plan(planner(), target(QQQ="0.5"), prices=gapped)
    assert r.orders[0].quantity == Decimal("127")  # 50,000 / 392 = 127.55
    big = plan(planner(), target(QQQ="1.0"), account=account(equity="1000000", bp="1000000"))
    assert big.orders[0].quantity == Decimal("625")  # max_order_notional 250,000 / 400
    assert any("max_order_notional" in n for n in big.notes)


def test_selling_an_inverse_etf_while_net_long_is_risk_increasing() -> None:
    r = plan(planner(), target(TQQQ="0.3"), positions=[pos("TQQQ", "600"), pos("SQQQ", "100")])
    sqqq = next(o for o in r.orders if o.symbol == "SQQQ")
    assert sqqq.side is OrderSide.SELL and sqqq.risk_increasing
