"""Core domain schemas shared by research, risk, portfolio and execution layers.

All models are immutable (``frozen=True``) and reject unknown fields, so a
decision record cannot be mutated after it was made and typos fail loudly.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from adaptive_quant.core.clock import ensure_utc
from adaptive_quant.core.enums import (
    AssetClass,
    OrderSide,
    OrderState,
    OrderType,
    SignalDirection,
    TimeInForce,
)
from adaptive_quant.core.money import quantize_weight

UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
"""A datetime that must be timezone-aware; it is normalised to UTC."""

#: Scores within this distance of zero are reported as NEUTRAL.
NEUTRAL_BAND = 0.05
#: Weights may exceed their bound by this much due to rounding.
WEIGHT_TOLERANCE = Decimal("0.000010")


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


# --------------------------------------------------------------------- instruments
class Instrument(DomainModel):
    """A tradeable or signal-only instrument.

    ``leverage`` is the *daily* exposure multiple to ``underlying``
    (TQQQ = 3, SQQQ = -3, QQQ = 1). It is used to compute beta-equivalent
    ("net underlying") exposure. Options/futures will get dedicated models in a
    later module rather than overloading this one.
    """

    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z0-9.^-]+$")
    asset_class: AssetClass
    leverage: float = 1.0
    underlying: str | None = None
    tradeable: bool = True
    currency: str = "USD"

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not math.isfinite(self.leverage) or self.leverage == 0:
            raise ValueError(f"{self.symbol}: leverage must be finite and non-zero")
        if self.asset_class in (AssetClass.OPTION, AssetClass.FUTURE):
            raise ValueError(
                f"{self.symbol}: {self.asset_class} instruments are not supported in version 1"
            )
        if self.asset_class is AssetClass.INDEX and self.tradeable:
            raise ValueError(f"{self.symbol}: index instruments are signal-only")
        if abs(self.leverage) != 1 and self.underlying is None:
            raise ValueError(f"{self.symbol}: leveraged instruments must declare an underlying")
        return self

    @property
    def is_leveraged(self) -> bool:
        return abs(self.leverage) > 1

    @property
    def is_inverse(self) -> bool:
        return self.leverage < 0


# --------------------------------------------------------------------- signals
class StrategySignal(DomainModel):
    """The output of one strategy evaluation at one point in time.

    Scores are continuous: ``-1`` strongly bearish, ``0`` neutral, ``+1``
    strongly bullish. ``data_timestamp`` is the timestamp of the most recent
    input observation used, which makes look-ahead audits possible: it can
    never be later than ``timestamp``.
    """

    strategy_name: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    timestamp: UtcDatetime
    data_timestamp: UtcDatetime
    direction: SignalDirection
    raw_score: float
    normalized_score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_exposure: float = Field(
        ge=-3.0,
        le=3.0,
        description="Suggested net underlying (beta-equivalent) exposure as a multiple of equity.",
    )
    reason: str = Field(min_length=1)
    indicator_values: dict[str, float | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not math.isfinite(self.raw_score):
            raise ValueError("raw_score must be finite")
        if self.data_timestamp > self.timestamp:
            raise ValueError("data_timestamp is after signal timestamp: possible look-ahead bias")
        expected = direction_for_score(self.normalized_score)
        if self.direction is not expected:
            raise ValueError(
                f"direction {self.direction} inconsistent with normalized_score "
                f"{self.normalized_score:+.3f} (expected {expected})"
            )
        for key, val in self.indicator_values.items():
            if val is not None and not math.isfinite(val):
                raise ValueError(f"indicator {key!r} is not finite; use None for unavailable")
        return self


def direction_for_score(score: float) -> SignalDirection:
    """Map a normalized score to a direction using :data:`NEUTRAL_BAND`."""
    if score > NEUTRAL_BAND:
        return SignalDirection.BULLISH
    if score < -NEUTRAL_BAND:
        return SignalDirection.BEARISH
    return SignalDirection.NEUTRAL


# --------------------------------------------------------------------- portfolio
class TargetPortfolio(DomainModel):
    """Desired end-of-cycle weights as fractions of account equity.

    Version 1 is long-only and unlevered at the account level: every weight is
    ``>= 0`` and the weights sum to at most 1. Bearish exposure is obtained by
    *holding* an inverse ETF, never by shorting. Cash is the remainder.
    """

    as_of: UtcDatetime
    weights: dict[str, Decimal]
    decision_id: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    rationale: str = ""

    @field_validator("weights", mode="after")
    @classmethod
    def _round_weights(cls, weights: dict[str, Decimal]) -> dict[str, Decimal]:
        return {symbol: quantize_weight(w) for symbol, w in weights.items()}

    @model_validator(mode="after")
    def _check(self) -> Self:
        for symbol, w in self.weights.items():
            if w < 0:
                raise ValueError(f"{symbol}: negative weight {w} (shorting is not allowed)")
            if symbol.upper() == "CASH":
                raise ValueError("do not list CASH explicitly; it is the implied remainder")
        total = self.invested_weight
        if total > Decimal(1) + WEIGHT_TOLERANCE:
            raise ValueError(f"weights sum to {total} > 1 (account leverage is not allowed)")
        return self

    @property
    def invested_weight(self) -> Decimal:
        return sum(self.weights.values(), Decimal(0))

    @property
    def cash_weight(self) -> Decimal:
        return max(Decimal(0), Decimal(1) - self.invested_weight)

    def net_underlying_exposure(self, instruments: dict[str, Instrument]) -> float:
        """Beta-equivalent exposure, e.g. 50% TQQQ + 10% QQQ = 1.6x."""
        total = 0.0
        for symbol, w in self.weights.items():
            if symbol not in instruments:
                raise KeyError(f"unknown instrument {symbol!r} in target portfolio")
            total += float(w) * instruments[symbol].leverage
        return total


# --------------------------------------------------------------------- broker views
class AccountSnapshot(DomainModel):
    """Account state as confirmed by the broker at ``as_of``."""

    as_of: UtcDatetime
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    currency: str = "USD"
    is_paper: bool
    trading_blocked: bool = False


class Position(DomainModel):
    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    market_value: Decimal


class OrderRequest(DomainModel):
    """An order intent. Persisted *before* it is transmitted to any broker."""

    client_order_id: str = Field(min_length=1, max_length=48)
    symbol: str
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Decimal | None = None
    risk_increasing: bool
    decision_id: str

    @model_validator(mode="after")
    def _check(self) -> Self:
        needs_limit = self.order_type in (OrderType.LIMIT, OrderType.LIMIT_ON_CLOSE)
        if needs_limit and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError(f"{self.order_type} orders require a positive limit_price")
        if not needs_limit and self.limit_price is not None:
            raise ValueError(f"{self.order_type} orders must not carry a limit_price")
        return self


class OrderSnapshot(DomainModel):
    """An order as reported by the broker."""

    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: OrderSide
    quantity: Decimal
    filled_quantity: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    state: OrderState
    updated_at: UtcDatetime
    raw: dict[str, Any] = Field(default_factory=dict)


class MarketClock(DomainModel):
    as_of: UtcDatetime
    is_open: bool
    next_open: UtcDatetime
    next_close: UtcDatetime
