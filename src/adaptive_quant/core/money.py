"""Decimal helpers for money, quantities and portfolio weights.

Floats are fine for research statistics (returns, Sharpe, ...). Anything that
becomes an order quantity, a price sent to a broker, or an account balance
used for sizing must go through :class:`~decimal.Decimal` via these helpers.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation

DecimalLike = Decimal | int | float | str

CENT = Decimal("0.01")
WEIGHT_QUANTUM = Decimal("0.000001")


def to_decimal(value: DecimalLike) -> Decimal:
    """Convert to ``Decimal`` without binary-float artefacts.

    ``float`` values go through ``repr`` so ``0.1`` becomes ``Decimal('0.1')``,
    not ``Decimal('0.1000000000000000055511151231257827')``. NaN and infinity
    are rejected: they must never reach sizing or order code.
    """
    if isinstance(value, bool):
        raise TypeError("bool is not a valid monetary value")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite value {value!r} cannot be used as money/quantity")
        result = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"cannot parse {value!r} as a decimal") from exc
    else:
        raise TypeError(f"unsupported type {type(value).__name__} for decimal conversion")
    if not result.is_finite():
        raise ValueError(f"non-finite value {value!r} cannot be used as money/quantity")
    return result


def quantize_price(price: DecimalLike, tick: Decimal = CENT) -> Decimal:
    """Round a price to the tick size using banker's rounding."""
    return _quantize(price, tick, ROUND_HALF_EVEN)


def quantize_quantity(qty: DecimalLike, step: Decimal) -> Decimal:
    """Round a share quantity *towards zero* to the lot step.

    Rounding toward zero guarantees we never order more than intended
    (e.g. step ``1`` for whole shares, ``0.000001`` for fractional).
    """
    return _quantize(qty, step, ROUND_DOWN)


def quantize_weight(weight: DecimalLike) -> Decimal:
    """Round a portfolio weight to 1e-6."""
    return _quantize(weight, WEIGHT_QUANTUM, ROUND_HALF_EVEN)


def _quantize(value: DecimalLike, quantum: Decimal, rounding: str) -> Decimal:
    if quantum <= 0:
        raise ValueError("quantum must be positive")
    d = to_decimal(value)
    return (d / quantum).to_integral_value(rounding=rounding) * quantum
