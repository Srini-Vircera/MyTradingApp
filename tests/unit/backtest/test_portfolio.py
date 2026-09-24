"""Exact Decimal ledger: cash, FIFO lots, realized/unrealized P&L, identity."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from adaptive_quant.core.enums import OrderSide
from adaptive_quant.quant.backtest.costs import FillQuote
from adaptive_quant.quant.backtest.portfolio import AccountingError, Fill, Portfolio

T0 = datetime(2024, 1, 2, 21, tzinfo=UTC)


def fill(
    side: OrderSide, qty: str, price: str, commission: str = "0", day: int = 0, sym: str = "QQQ"
) -> tuple[Fill, FillQuote]:
    q, p = Decimal(qty), Decimal(price)
    notional = (q * p).quantize(Decimal("0.01"))
    t = T0 + timedelta(days=day)
    f = Fill(
        1,
        t,
        t,
        t,
        sym,
        side,
        q,
        q,
        p,
        p,
        notional,
        Decimal(commission),
        Decimal(0),
        Decimal(0),
        Decimal(0),
    )
    return f, FillQuote(q, p, p, notional, Decimal(commission), Decimal(0), Decimal(0), Decimal(0))


def test_round_trip_hand_calculated() -> None:
    pf = Portfolio(Decimal("100000"))
    pf.apply(*fill(OrderSide.BUY, "100", "100", "1.00"))
    assert pf.cash == Decimal("89999.00")
    prices = {"QQQ": Decimal("110")}
    assert pf.equity(prices) == Decimal("100999.00")
    assert pf.unrealized_pnl(prices) == Decimal("1000.00")
    pf.apply(*fill(OrderSide.SELL, "100", "120", "1.00", day=10))
    assert pf.cash == Decimal("101998.00")
    assert pf.realized_pnl == Decimal("2000.00")
    assert pf.commissions == Decimal("2.00")
    [rt] = pf.round_trips
    assert rt.pnl == Decimal("1998.00")  # net of both commissions
    assert rt.holding_days == pytest.approx(10.0)
    assert pf.identity_gap({"QQQ": Decimal("120")}) == 0


def test_fifo_partial_sells_split_cost_exactly() -> None:
    pf = Portfolio(Decimal("100000"))
    pf.apply(*fill(OrderSide.BUY, "3", "10.01", "0.03"))  # cost 30.03
    pf.apply(*fill(OrderSide.BUY, "2", "20", "0", day=1))
    pf.apply(*fill(OrderSide.SELL, "1", "15", day=2))  # consumes 1/3 of lot 1
    assert pf.round_trips[0].cost == Decimal("10.01")
    pf.apply(*fill(OrderSide.SELL, "3", "15", day=3))  # rest of lot 1 (2 sh) + 1 of lot 2
    costs = [rt.cost for rt in pf.round_trips]
    assert costs == [Decimal("10.01"), Decimal("20.02"), Decimal("20.00")]
    assert sum(costs) + pf.cost_basis("QQQ") == Decimal("30.03") + Decimal(
        "40.00"
    )  # nothing lost to rounding
    assert pf.quantity("QQQ") == Decimal(1)
    assert pf.identity_gap({"QQQ": Decimal("17")}) == 0


def test_identity_holds_through_many_random_trades() -> None:
    import random

    rng = random.Random(7)
    pf = Portfolio(Decimal("50000"))
    price = Decimal("100")
    for day in range(300):
        price = (price * Decimal(str(1 + rng.uniform(-0.03, 0.03)))).quantize(Decimal("0.000001"))
        held = pf.quantity("QQQ")
        if rng.random() < 0.5 and pf.cash > 1000:
            qty = Decimal(str(rng.randint(1, 40)))
            comm = Decimal(str(rng.choice(["0", "1.25", "0.01"])))
            if (qty * price).quantize(Decimal("0.01")) + comm <= pf.cash:
                pf.apply(*fill(OrderSide.BUY, str(qty), str(price), str(comm), day))
        elif held > 0:
            qty = min(held, Decimal(str(rng.randint(1, 60))))
            pf.apply(*fill(OrderSide.SELL, str(qty), str(price), "0.07", day))
        pf.accrue_interest(pf.cash * Decimal("0.00005"))
        assert pf.identity_gap({"QQQ": price}) == 0
        assert pf.cash >= 0


def test_overdraft_and_oversell_are_bugs() -> None:
    pf = Portfolio(Decimal("1000"))
    with pytest.raises(AccountingError, match="needs"):
        pf.apply(*fill(OrderSide.BUY, "100", "100"))
    with pytest.raises(AccountingError, match="exceeds holding"):
        pf.apply(*fill(OrderSide.SELL, "1", "100"))
    with pytest.raises(AccountingError):
        Portfolio(Decimal(0))
    with pytest.raises(AccountingError, match="negative interest"):
        pf.accrue_interest(Decimal("-1"))


def test_missing_mark_price_is_an_error() -> None:
    pf = Portfolio(Decimal("1000"))
    pf.apply(*fill(OrderSide.BUY, "1", "10"))
    with pytest.raises(AccountingError, match="no mark price"):
        pf.equity({})
