"""Transaction costs and fill pricing (Decimal).

For an order of ``q`` shares at reference price ``P`` (the open or close of
the fill bar):

* half-spread ``s`` bps (per symbol), slippage ``k`` bps, and square-root
  market impact ``i = c * sqrt(q / ADV)`` bps (ADV in shares, known at the
  decision time) are added for buys / subtracted for sells:
  ``fill = P * (1 ± (s + k + i) / 10_000)``;
* commission = ``max(minimum, per_order + per_share * q)``.

Implicit costs (spread, slippage, impact) are attributed in dollars so that
they sum *exactly* to ``|notional - q * P|``. All cash amounts are rounded to
cents with banker's rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from adaptive_quant.config.schema import CostConfig
from adaptive_quant.core.enums import OrderSide
from adaptive_quant.core.money import CENT, quantize_price, to_decimal

BPS = Decimal(10_000)
PRICE_TICK = Decimal("0.000001")  # adjusted prices carry more precision than cents


@dataclass(frozen=True)
class FillQuote:
    quantity: Decimal
    ref_price: Decimal
    fill_price: Decimal
    notional: Decimal  # cash exchanged for the shares (excl. commission), cents
    commission: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    impact_cost: Decimal

    @property
    def implicit_cost(self) -> Decimal:
        return self.spread_cost + self.slippage_cost + self.impact_cost


class CostModel:
    def __init__(self, config: CostConfig) -> None:
        self.config = config

    def participation_cap(self, adv_shares: Decimal | None) -> Decimal | None:
        """Maximum shares one order may fill; ``None`` = no cap (volume unknown, assumed liquid)."""
        if adv_shares is None or adv_shares <= 0:
            return None
        return adv_shares * to_decimal(self.config.max_participation)

    def impact_bps(self, quantity: Decimal, adv_shares: Decimal | None) -> Decimal:
        if adv_shares is None or adv_shares <= 0 or quantity <= 0:
            return Decimal(0)
        ratio = quantity / adv_shares
        return to_decimal(self.config.impact_coefficient_bps) * ratio.sqrt()

    def commission(self, quantity: Decimal) -> Decimal:
        if quantity <= 0:
            return Decimal(0)
        c = self.config
        raw = to_decimal(c.commission_per_order) + to_decimal(c.commission_per_share) * quantity
        return max(to_decimal(c.commission_minimum), raw).quantize(CENT)

    def quote(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        ref_price: Decimal,
        adv_shares: Decimal | None,
    ) -> FillQuote:
        if quantity <= 0:
            raise ValueError("quote quantity must be positive")
        if ref_price <= 0:
            raise ValueError("reference price must be positive")
        spread = to_decimal(self.config.half_spread(symbol))
        slip = to_decimal(self.config.slippage_bps)
        impact = self.impact_bps(quantity, adv_shares)
        total_bps = spread + slip + impact
        direction = Decimal(1) if side is OrderSide.BUY else Decimal(-1)
        fill_price = quantize_price(ref_price * (1 + direction * total_bps / BPS), PRICE_TICK)
        if fill_price <= 0:
            raise ValueError(f"{symbol}: costs exceed the price")
        notional = (quantity * fill_price).quantize(CENT)
        implicit = abs(notional - (quantity * ref_price).quantize(CENT))
        spread_cost, slippage_cost, impact_cost = _split(implicit, (spread, slip, impact))
        return FillQuote(
            quantity=quantity,
            ref_price=ref_price,
            fill_price=fill_price,
            notional=notional,
            commission=self.commission(quantity),
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            impact_cost=impact_cost,
        )


def _split(
    total: Decimal, weights: tuple[Decimal, Decimal, Decimal]
) -> tuple[Decimal, Decimal, Decimal]:
    """Split ``total`` cents proportionally; the last share absorbs rounding (exact sum)."""
    w_sum = sum(weights, Decimal(0))
    if w_sum == 0 or total == 0:
        return Decimal(0), Decimal(0), Decimal(0)
    a = (total * weights[0] / w_sum).quantize(CENT)
    b = (total * weights[1] / w_sum).quantize(CENT)
    return a, b, total - a - b
