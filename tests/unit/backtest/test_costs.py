"""Transaction costs against hand calculations (exact Decimal)."""

from decimal import Decimal

import pytest

from adaptive_quant.config.schema import CostConfig
from adaptive_quant.core.enums import OrderSide
from adaptive_quant.quant.backtest.costs import CostModel


def model(**kw: object) -> CostModel:
    base: dict[str, object] = {
        "half_spread_bps": {"default": 5.0, "QQQ": 1.0},
        "slippage_bps": 2.0,
        "impact_coefficient_bps": 10.0,
        "commission_per_share": 0.005,
        "commission_per_order": 1.0,
        "commission_minimum": 1.5,
    }
    base.update(kw)
    return CostModel(CostConfig.model_validate(base))


def test_buy_quote_hand_calculated() -> None:
    # 400 shares at 100, ADV 10,000: impact = 10 * sqrt(400/10000) = 2 bps
    # total = 1 (QQQ half-spread) + 2 (slippage) + 2 (impact) = 5 bps -> fill 100.05
    q = model().quote("QQQ", OrderSide.BUY, Decimal(400), Decimal(100), Decimal(10_000))
    assert q.fill_price == Decimal("100.050000")
    assert q.notional == Decimal("40020.00")
    assert q.commission == Decimal("3.00")  # 1.00 + 400 * 0.005
    assert (
        q.spread_cost == Decimal("4.00")
        and q.slippage_cost == Decimal("8.00")
        and q.impact_cost == Decimal("8.00")
    )
    assert q.implicit_cost == q.notional - Decimal(400) * Decimal(100)


def test_sell_quote_moves_price_down() -> None:
    q = model().quote(
        "TQQQ", OrderSide.SELL, Decimal(100), Decimal(50), None
    )  # unknown ADV -> no impact
    # 5 + 2 = 7 bps below 50 -> 49.965
    assert q.fill_price == Decimal("49.965000")
    assert q.notional == Decimal("4996.50")
    assert q.impact_cost == 0
    assert q.spread_cost + q.slippage_cost == Decimal("3.50")
    assert q.commission == Decimal("1.50")  # 1.00 + 0.50 = 1.50 == minimum


def test_commission_minimum_applies() -> None:
    assert model().commission(Decimal(10)) == Decimal("1.50")
    assert model().commission(Decimal(0)) == Decimal(0)


def test_implicit_costs_always_sum_exactly() -> None:
    m = model()
    for qty in (Decimal("0.333333"), Decimal(7), Decimal(1234), Decimal("98765.4321")):
        for side in OrderSide:
            q = m.quote("SQQQ", side, qty, Decimal("37.123457"), Decimal(250_000))
            gross = (qty * Decimal("37.123457")).quantize(Decimal("0.01"))
            assert q.spread_cost + q.slippage_cost + q.impact_cost == abs(q.notional - gross)


def test_participation_cap() -> None:
    m = model(max_participation=0.05)
    assert m.participation_cap(Decimal(1000)) == Decimal(50)
    assert m.participation_cap(None) is None
    assert m.participation_cap(Decimal(0)) is None


def test_invalid_quotes() -> None:
    with pytest.raises(ValueError, match="positive"):
        model().quote("QQQ", OrderSide.BUY, Decimal(0), Decimal(100), None)
    with pytest.raises(ValueError, match="positive"):
        model().quote("QQQ", OrderSide.BUY, Decimal(1), Decimal(0), None)


def test_config_requires_default_spread() -> None:
    with pytest.raises(ValueError, match="default"):
        CostConfig.model_validate({"half_spread_bps": {"QQQ": 1.0}})
