"""The decision chain shared by backtests and (from M10) the live cycle:

signals -> ensemble weights & combination -> allocation policy -> risk engine.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from adaptive_quant.core.models import StrategySignal
from adaptive_quant.quant.ensemble.engine import EnsembleEngine, EnsembleScore
from adaptive_quant.quant.portfolio.policy import AllocationPolicy
from adaptive_quant.quant.risk.engine import RiskEngine
from adaptive_quant.quant.risk.models import ProposedPortfolio, RiskContext, RiskDecision


@dataclass(frozen=True)
class PortfolioDecision:
    ensemble: EnsembleScore
    proposal: ProposedPortfolio
    risk: RiskDecision


class PortfolioManager:
    def __init__(
        self, ensemble: EnsembleEngine, policy: AllocationPolicy, risk: RiskEngine
    ) -> None:
        self.ensemble = ensemble
        self.policy = policy
        self.risk = risk

    def decide(
        self, signals: Sequence[StrategySignal], shadow: np.ndarray, ctx: RiskContext
    ) -> PortfolioDecision:
        """Raises ``RiskCalculationError`` (refuse) on any failure after the ensemble."""
        weights = self.ensemble.weights(shadow)
        score = self.ensemble.combine(signals, weights)
        proposal = self.policy.propose(score.exposure, ctx.as_of)
        return PortfolioDecision(score, proposal, self.risk.evaluate(proposal, ctx))
