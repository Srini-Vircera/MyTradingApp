"""Strategy ranking scorecard and validation gates.

Ranking is **never** by CAGR. Each criterion is converted to a percentile
rank across the candidates (best = 1, worst = 0; a single candidate gets 0.5)
and combined with the configured weights. Gates are pass/fail: a candidate
that fails any gate cannot be marked ``validated``, whatever its score.
A missing or undefined value always fails its gate (fail closed).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: criterion -> (evidence attribute, higher is better)
CRITERIA: dict[str, tuple[str, bool]] = {
    "oos_sharpe": ("oos_sharpe", True),
    "oos_sortino": ("oos_sortino", True),
    "oos_max_drawdown": ("oos_max_drawdown", True),  # negative number: closer to 0 is better
    "oos_calmar": ("oos_calmar", True),
    "robustness": ("robustness_score", True),
    "regime_consistency": ("regime_consistency", True),
    "diversification": ("diversification", True),
    "turnover": ("annual_turnover", False),
    "execution": ("execution_feasibility", True),
}


@dataclass
class Evidence:
    strategy_id: str
    version_id: str
    params_label: str
    oos_sharpe: float
    oos_sortino: float
    oos_max_drawdown: float
    oos_calmar: float
    oos_sessions: int
    oos_synthetic_sessions: int
    robustness_score: float
    potentially_overfit: bool
    regime_consistency: float
    annual_turnover: float
    execution_feasibility: float
    dsr: float
    pbo: float
    sharpe_p_value: float
    bh_adjusted_p: float = math.nan
    diversification: float = math.nan
    oos_benchmark_sharpe: float = math.nan  # QQQ buy-and-hold over the same OOS sessions
    oos_returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float), repr=False)


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    value: float | bool
    threshold: float | bool
    rule: str


@dataclass(frozen=True)
class GateSettings:
    dsr_min: float = 0.95
    pbo_max: float = 0.30
    robustness_min: float = 0.50
    oos_sharpe_min: float = 0.0
    fdr_q: float = 0.10
    min_oos_sessions: int = 504
    real_data_only: bool = True
    min_sharpe_vs_benchmark: float = 0.0


def gates(e: Evidence, g: GateSettings) -> list[GateResult]:
    def ge(v: float, t: float) -> bool:
        return math.isfinite(v) and v >= t

    def le(v: float, t: float) -> bool:
        return math.isfinite(v) and v <= t

    out = [
        GateResult("deflated_sharpe", ge(e.dsr, g.dsr_min), e.dsr, g.dsr_min, "DSR >= threshold"),
        GateResult("pbo", le(e.pbo, g.pbo_max), e.pbo, g.pbo_max, "PBO (CSCV) <= threshold"),
        GateResult(
            "robustness",
            ge(e.robustness_score, g.robustness_min) and not e.potentially_overfit,
            e.robustness_score,
            g.robustness_min,
            "parameter plateau, not an isolated peak",
        ),
        GateResult(
            "oos_sharpe",
            math.isfinite(e.oos_sharpe) and e.oos_sharpe > g.oos_sharpe_min,
            e.oos_sharpe,
            g.oos_sharpe_min,
            "stitched walk-forward OOS Sharpe > threshold",
        ),
        GateResult(
            "fdr",
            le(e.bh_adjusted_p, g.fdr_q),
            e.bh_adjusted_p,
            g.fdr_q,
            "BH-adjusted p(SR <= 0) <= q",
        ),
        GateResult(
            "oos_length",
            e.oos_sessions >= g.min_oos_sessions,
            float(e.oos_sessions),
            float(g.min_oos_sessions),
            "enough out-of-sample sessions",
        ),
    ]
    edge = e.oos_sharpe - e.oos_benchmark_sharpe
    out.append(
        GateResult(
            "vs_benchmark",
            math.isfinite(edge) and edge >= g.min_sharpe_vs_benchmark,
            edge,
            g.min_sharpe_vs_benchmark,
            "OOS Sharpe minus QQQ buy-and-hold OOS Sharpe >= threshold",
        )
    )
    if g.real_data_only:
        out.append(
            GateResult(
                "real_data",
                e.oos_synthetic_sessions == 0,
                float(e.oos_synthetic_sessions),
                0.0,
                "no SYNTHETIC sessions in the evaluated OOS period",
            )
        )
    return out


def diversification(candidates: Sequence[Evidence]) -> dict[str, float]:
    """1 - mean |correlation| of each candidate's OOS returns with every other candidate's."""
    if len(candidates) < 2:
        return {c.strategy_id: math.nan for c in candidates}
    frame = pd.DataFrame({c.strategy_id: c.oos_returns for c in candidates})
    corr = frame.corr(min_periods=20).abs()
    out = {}
    for c in candidates:
        others = corr[c.strategy_id].drop(c.strategy_id).dropna()
        out[c.strategy_id] = 1.0 - float(others.mean()) if len(others) else math.nan
    return out


@dataclass(frozen=True)
class ScoredCandidate:
    evidence: Evidence
    score: float
    components: dict[str, float]  # criterion -> percentile rank (0..1)
    gates: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)


def score(
    candidates: Sequence[Evidence], weights: Mapping[str, float], g: GateSettings
) -> list[ScoredCandidate]:
    """Rank candidates: gate-passing first, then by weighted percentile-rank score."""
    unknown = set(weights) - set(CRITERIA)
    if unknown:
        raise ValueError(f"unknown scorecard criteria: {sorted(unknown)}")
    total_w = sum(w for w in weights.values() if w > 0)
    if total_w <= 0:
        raise ValueError("scorecard weights must include a positive weight")
    ranks: dict[str, dict[str, float]] = {c.strategy_id: {} for c in candidates}
    for crit, (attr, higher) in CRITERIA.items():
        vals = pd.Series({c.strategy_id: _f(getattr(c, attr)) for c in candidates}, dtype=float)
        if not higher:
            vals = -vals
        n_ok = int(vals.notna().sum())
        r = vals.rank(method="average")
        for sid in ranks:
            v = r.get(sid, np.nan)
            if not math.isfinite(v):
                ranks[sid][crit] = 0.0  # undefined values rank last
            elif n_ok == 1:
                ranks[sid][crit] = 0.5
            else:
                ranks[sid][crit] = float((v - 1) / (n_ok - 1))
    scored = []
    for c in candidates:
        comp = ranks[c.strategy_id]
        s = (
            sum(weights.get(k, 0.0) * comp[k] for k in CRITERIA if weights.get(k, 0.0) > 0)
            / total_w
        )
        scored.append(ScoredCandidate(c, s, comp, tuple(gates(c, g))))
    return sorted(scored, key=lambda x: (not x.passed, -x.score, x.evidence.strategy_id))


def _f(v: object) -> float:
    return float(v) if isinstance(v, int | float) and math.isfinite(float(v)) else math.nan
