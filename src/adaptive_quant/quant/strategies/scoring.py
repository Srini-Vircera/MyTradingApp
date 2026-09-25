"""Small, explicit helpers for turning indicator values into bounded scores."""

from __future__ import annotations

import math


def squash(x: float, scale: float) -> float:
    """Smoothly map any real number into (-1, 1): ``tanh(x / scale)``."""
    if scale <= 0:
        raise ValueError("scale must be positive")
    return math.tanh(x / scale)


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def sign(x: float) -> float:
    return 1.0 if x > 0 else -1.0 if x < 0 else 0.0


def exposure_for(score: float, max_long: float, max_short: float) -> float:
    """Suggested net underlying exposure: positive scores scale ``max_long``,
    negative scores scale ``max_short`` (0 means "reduce to cash, never short")."""
    return score * max_long if score >= 0 else score * max_short


def pct(x: float) -> str:
    return f"{x:+.1%}"
