"""Ensemble weighting and combination.

Weights (``strategies.yaml -> ensemble``):

* ``equal``         1/N
* ``fixed``         configured ``fixed_weights`` (missing strategies get 0)
* ``risk_adjusted`` inverse volatility of shadow returns over ``vol_window``
* ``walk_forward``  max(Sharpe, 0) of shadow returns over ``lookback_sessions``,
                    refitted every ``refit_sessions`` on past data only and shrunk
                    towards equal weight by ``shrinkage``

With fewer than ``min_history`` shadow returns, data-driven methods fall back to
equal weight (flagged). Then:

1. **correlation clustering** - strategies whose shadow-return correlation
   exceeds ``max_pairwise_correlation`` are linked (single linkage); a cluster
   counts as one strategy: its total is the *mean* of its members' weights;
2. **family caps** - no family may exceed ``max_family_weight``; the excess is
   redistributed to uncapped families, and if every family is capped the
   remainder stays unallocated (cash).

Combination: exposure = sum(w_i x suggested_exposure_i) (unallocated weight
contributes 0), and likewise for score and confidence.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from adaptive_quant.config.schema import EnsembleConfig
from adaptive_quant.core.enums import EnsembleMethod, StrategyFamily
from adaptive_quant.core.models import StrategySignal

TOL = 1e-12


@dataclass(frozen=True)
class Member:
    strategy_id: str
    family: StrategyFamily


@dataclass(frozen=True)
class StrategyWeights:
    weights: dict[str, float]  # sum <= 1
    method: str
    clusters: tuple[tuple[str, ...], ...]
    adjustments: tuple[str, ...] = ()

    @property
    def unallocated(self) -> float:
        return max(0.0, 1.0 - sum(self.weights.values()))


@dataclass(frozen=True)
class EnsembleScore:
    exposure: float
    score: float
    confidence: float
    weights: StrategyWeights
    contributions: dict[str, float] = field(default_factory=dict)


class EnsembleEngine:
    def __init__(self, members: Sequence[Member], cfg: EnsembleConfig) -> None:
        if not members:
            raise ValueError("ensemble needs at least one member")
        ids = [m.strategy_id for m in members]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate ensemble members")
        self.members = list(members)
        self.ids = ids
        self.cfg = cfg
        self._fit: np.ndarray | None = None
        self._fit_rows = -1

    # ------------------------------------------------------------------ weights
    def weights(self, shadow: np.ndarray) -> StrategyWeights:
        """Weights from shadow returns known at the decision (rows oldest first)."""
        h = np.asarray(shadow, dtype=float).reshape(-1, len(self.ids))
        h = h[-self.cfg.lookback_sessions :]
        notes: list[str] = []
        method = self.cfg.method
        enough = len(h) >= self.cfg.min_history
        if method in (EnsembleMethod.RISK_ADJUSTED, EnsembleMethod.WALK_FORWARD) and not enough:
            notes.append(
                f"{method}: {len(h)} < {self.cfg.min_history} shadow returns - equal weight"
            )
            raw = np.ones(len(self.ids))
        elif method is EnsembleMethod.EQUAL:
            raw = np.ones(len(self.ids))
        elif method is EnsembleMethod.FIXED:
            raw = np.array([self.cfg.fixed_weights.get(i, 0.0) for i in self.ids], dtype=float)
            if raw.sum() <= TOL:
                raise ValueError("fixed ensemble weights are all zero for the members")
        elif method is EnsembleMethod.RISK_ADJUSTED:
            raw = self._inverse_vol(h[-self.cfg.vol_window :])
        else:
            raw = self._walk_forward(shadow)
        raw = raw / raw.sum()
        clusters = (
            self._clusters(h)
            if enough and len(self.ids) > 1
            else [[i] for i in range(len(self.ids))]
        )
        w = raw.copy()
        for c in clusters:
            if len(c) > 1:
                total = float(raw[c].mean())
                w[c] = raw[c] / raw[c].sum() * total
                notes.append(f"cluster {[self.ids[i] for i in c]} shares one weight ({total:.3f})")
        w = w / w.sum()
        w = self._family_caps(w, notes)
        return StrategyWeights(
            weights={i: float(v) for i, v in zip(self.ids, w, strict=True)},
            method=str(method),
            clusters=tuple(tuple(self.ids[i] for i in c) for c in clusters),
            adjustments=tuple(notes),
        )

    def _inverse_vol(self, h: np.ndarray) -> np.ndarray:
        sd = h.std(axis=0, ddof=1)
        pos = sd[sd > TOL]
        if len(pos) == 0:
            return np.ones(len(self.ids))
        floor = float(np.median(pos))  # a flat (always-cash) strategy is not "infinitely safe"
        return 1.0 / np.where(sd > TOL, sd, floor)

    def _walk_forward(self, shadow: np.ndarray) -> np.ndarray:
        rows = len(shadow)
        if self._fit is None or rows - self._fit_rows >= self.cfg.refit_sessions:
            h = np.asarray(shadow, dtype=float)[-self.cfg.lookback_sessions :]
            sd = h.std(axis=0, ddof=1)
            with np.errstate(divide="ignore", invalid="ignore"):
                sr = np.where(sd > TOL, h.mean(axis=0) / sd, 0.0)
            fit = np.maximum(sr, 0.0)
            fit = fit / fit.sum() if fit.sum() > TOL else np.full(len(self.ids), 1 / len(self.ids))
            lam = self.cfg.shrinkage
            self._fit = (1 - lam) * fit + lam / len(self.ids)
            self._fit_rows = rows
        return self._fit.copy()

    def _clusters(self, h: np.ndarray) -> list[list[int]]:
        n = len(self.ids)
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        sd = h.std(axis=0, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            corr = np.corrcoef(h, rowvar=False) if n > 1 else np.ones((1, 1))
        for a in range(n):
            for b in range(a + 1, n):
                if (
                    sd[a] > TOL
                    and sd[b] > TOL
                    and math.isfinite(corr[a, b])
                    and corr[a, b] > self.cfg.max_pairwise_correlation
                ):
                    parent[find(a)] = find(b)
        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)
        return sorted(groups.values())

    def _family_caps(self, w: np.ndarray, notes: list[str]) -> np.ndarray:
        cap = self.cfg.max_family_weight
        fam = np.array([m.family.value for m in self.members])
        w = w.copy()
        fixed: set[str] = set()
        for _ in range(len(set(fam)) + 1):
            totals = {f: float(w[fam == f].sum()) for f in set(fam)}
            over = {f for f, t in totals.items() if t > cap + TOL}
            if not over:
                break
            excess = 0.0
            for f in over:
                mask = fam == f
                excess += totals[f] - cap
                w[mask] *= cap / totals[f]
                fixed.add(f)
                notes.append(f"family {f} capped at {cap:.0%}")
            free = np.array([f not in fixed for f in fam])
            free_total = float(w[free].sum())
            if free_total <= TOL:
                notes.append(f"{excess:.1%} of ensemble weight unallocated (every family capped)")
                break
            w[free] += w[free] / free_total * excess
        return w

    # ------------------------------------------------------------------ combine
    def combine(self, signals: Sequence[StrategySignal], weights: StrategyWeights) -> EnsembleScore:
        by_id = {s.strategy_name: s for s in signals}
        missing = [i for i in self.ids if i not in by_id]
        if missing:
            raise ValueError(f"ensemble: no signal from {missing}")
        contrib = {i: weights.weights[i] * by_id[i].suggested_exposure for i in self.ids}
        return EnsembleScore(
            exposure=float(sum(contrib.values())),
            score=float(sum(weights.weights[i] * by_id[i].normalized_score for i in self.ids)),
            confidence=float(sum(weights.weights[i] * by_id[i].confidence for i in self.ids)),
            weights=weights,
            contributions=contrib,
        )
