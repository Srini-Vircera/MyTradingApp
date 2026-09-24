"""Risk-engine inputs and outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import numpy as np

from adaptive_quant.core.errors import AQError
from adaptive_quant.core.models import TargetPortfolio


class RiskCalculationError(AQError):
    """A risk rule could not be evaluated or its result failed verification: refuse."""


@dataclass(frozen=True)
class ProposedPortfolio:
    """Weights proposed by the allocation policy (fractions of equity, cash = remainder)."""

    as_of: datetime
    weights: dict[str, float]
    requested_exposure: float = 0.0  # ensemble net exposure before mapping
    rationale: str = ""


@dataclass(frozen=True)
class RiskContext:
    """Point-in-time account and market state known at the decision."""

    as_of: datetime
    equity: float
    peak_equity: float
    previous_equity: float  # equity at the previous completed session (daily-loss reference)
    current_weights: dict[str, float]
    underlying_returns: np.ndarray  # daily simple returns of the underlying, oldest first
    kill_switch_engaged: bool = False
    previous_band: str | None = None


@dataclass(frozen=True)
class RiskAdjustment:
    rule: str
    before: float
    after: float
    reason: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.before:+.4f} -> {self.after:+.4f} ({self.reason})"


@dataclass(frozen=True)
class RiskDecision:
    as_of: datetime
    weights: dict[str, Decimal]  # approved; long-only, sum <= 1, rounded down
    proposed: dict[str, float]
    adjustments: tuple[RiskAdjustment, ...]
    band: str
    drawdown: float
    vol_scale: float
    forecast_vol: float
    regime: str | None
    daily_loss: float
    risk_increasing_blocked: bool
    flags: tuple[str, ...] = field(default=())

    @property
    def cash_weight(self) -> Decimal:
        return Decimal(1) - sum(self.weights.values(), Decimal(0))

    def to_target(self, decision_id: str, config_version: str) -> TargetPortfolio:
        return TargetPortfolio(
            as_of=self.as_of,
            weights=dict(self.weights),
            decision_id=decision_id,
            config_version=config_version,
            rationale="; ".join(str(a) for a in self.adjustments) or "no risk adjustments",
        )
