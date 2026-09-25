"""Multiple-testing and resampling statistics.

References (implemented from the published definitions):

* Probabilistic / Deflated Sharpe Ratio - Bailey & López de Prado (2012, 2014),
  "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest
  Overfitting and Non-Normality", J. Portfolio Management 40(5).
* Probability of Backtest Overfitting via CSCV - Bailey, Borwein, López de
  Prado & Zhu (2017), J. Computational Finance 20(4).
* Stationary bootstrap - Politis & Romano (1994), JASA 89(428).
* Reality Check - White (2000), Econometrica 68(5); Superior Predictive
  Ability - Hansen (2005), JBES 23(4) (studentised, consistent recentring).
* False discovery rate - Benjamini & Hochberg (1995), JRSS B 57(1).

Conventions: Sharpe ratios here are **per period** (not annualised) unless a
name says otherwise; kurtosis is the raw (non-excess) fourth standardised
moment, so a normal distribution has kurtosis 3.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import NormalDist

import numpy as np
import pandas as pd

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()

FloatArray = np.ndarray


# ------------------------------------------------------------------ Sharpe
def sharpe_per_period(r: Sequence[float] | FloatArray, rf_per_period: float = 0.0) -> float:
    """Mean / sample std (ddof=1) of excess returns; NaN when undefined."""
    x = np.asarray(r, dtype=float) - rf_per_period
    if len(x) < 2:
        return math.nan
    sd = float(x.std(ddof=1))
    return float(x.mean()) / sd if sd > 1e-15 else math.nan


def moments(r: Sequence[float] | FloatArray) -> tuple[float, float]:
    """(skewness, raw kurtosis) with population (biased) moments, as in the DSR paper."""
    x = np.asarray(r, dtype=float)
    if len(x) < 3:
        return 0.0, 3.0
    d = x - x.mean()
    m2 = float((d**2).mean())
    if m2 <= 1e-30:
        return 0.0, 3.0
    return float((d**3).mean()) / m2**1.5, float((d**4).mean()) / m2**2


def probabilistic_sharpe_ratio(
    sr: float, sr_benchmark: float, n: int, skew: float = 0.0, kurtosis: float = 3.0
) -> float:
    """P[true SR > sr_benchmark] given an observed per-period ``sr`` over ``n`` returns.

    PSR = Φ( (SR − SR*) √(n−1) / √(1 − γ3·SR + (γ4 − 1)/4 · SR²) )
    """
    if n < 2 or not math.isfinite(sr):
        return math.nan
    denom = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return math.nan
    return _N.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, var_trial_sr: float) -> float:
    """E[max SR] of ``n_trials`` independent zero-skill trials (the DSR benchmark SR0).

    SR0 = √V · ((1 − γ) Φ⁻¹(1 − 1/N) + γ Φ⁻¹(1 − 1/(N e)))
    """
    if n_trials <= 1 or var_trial_sr <= 0:
        return 0.0
    a = _N.inv_cdf(1.0 - 1.0 / n_trials)
    b = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(var_trial_sr) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


@dataclass(frozen=True)
class DeflatedSharpe:
    sharpe: float  # per period
    sr0: float  # expected maximum per-period SR under the null
    dsr: float  # probability the true SR exceeds sr0
    n_obs: int
    n_trials: int
    skew: float
    kurtosis: float


def deflated_sharpe_ratio(
    r: Sequence[float] | FloatArray, n_trials: int, var_trial_sr: float
) -> DeflatedSharpe:
    x = np.asarray(r, dtype=float)
    sr = sharpe_per_period(x)
    skew, kurt = moments(x)
    sr0 = expected_max_sharpe(n_trials, var_trial_sr)
    return DeflatedSharpe(
        sharpe=sr,
        sr0=sr0,
        dsr=probabilistic_sharpe_ratio(sr, sr0, len(x), skew, kurt),
        n_obs=len(x),
        n_trials=n_trials,
        skew=skew,
        kurtosis=kurt,
    )


def sharpe_pvalue(r: Sequence[float] | FloatArray) -> float:
    """One-sided p-value for H0: SR <= 0 (1 − PSR with SR* = 0; non-normality adjusted)."""
    x = np.asarray(r, dtype=float)
    skew, kurt = moments(x)
    psr = probabilistic_sharpe_ratio(sharpe_per_period(x), 0.0, len(x), skew, kurt)
    return 1.0 if not math.isfinite(psr) else 1.0 - psr


# ------------------------------------------------------------------ FDR
def benjamini_hochberg(pvalues: Sequence[float]) -> list[float]:
    """BH step-up adjusted p-values (monotone, capped at 1); input order is preserved."""
    p = np.asarray(pvalues, dtype=float)
    m = len(p)
    if m == 0:
        return []
    if np.any((p < 0) | (p > 1) | ~np.isfinite(p)):
        raise ValueError("p-values must lie in [0, 1]")
    order = np.argsort(p, kind="stable")
    ranked = p[order] * m / np.arange(1, m + 1)
    adjusted = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(adjusted, 1.0)
    return out.tolist()


def bh_reject(pvalues: Sequence[float], q: float) -> list[bool]:
    return [a <= q for a in benjamini_hochberg(pvalues)]


# ------------------------------------------------------------------ bootstrap
def stationary_bootstrap_indices(
    n: int, mean_block: float, samples: int, rng: np.random.Generator
) -> np.ndarray:
    """``(samples, n)`` index matrix of Politis-Romano stationary bootstrap resamples.

    Blocks start at uniformly random positions, have geometric lengths with mean
    ``mean_block`` and wrap around the end of the series.
    """
    if n < 1 or samples < 1:
        raise ValueError("n and samples must be positive")
    if mean_block < 1:
        raise ValueError("mean_block must be >= 1")
    starts = rng.integers(0, n, size=(samples, n))
    new_block = rng.random((samples, n)) < 1.0 / mean_block
    new_block[:, 0] = True
    t = np.arange(n)
    last_start = np.maximum.accumulate(np.where(new_block, t, 0), axis=1)
    block_origin = np.take_along_axis(starts, last_start, axis=1)
    return (block_origin + (t - last_start)) % n


@dataclass(frozen=True)
class ConfidenceInterval:
    point: float
    lower: float
    upper: float
    confidence: float


def bootstrap_ci(
    r: Sequence[float] | FloatArray,
    statistic: Callable[[np.ndarray], float],
    *,
    samples: int,
    mean_block: float,
    confidence: float,
    seed: int,
) -> ConfidenceInterval:
    """Percentile confidence interval of ``statistic`` under the stationary bootstrap."""
    x = np.asarray(r, dtype=float)
    idx = stationary_bootstrap_indices(len(x), mean_block, samples, np.random.default_rng(seed))
    stats = np.array([statistic(x[row]) for row in idx])
    stats = stats[np.isfinite(stats)]
    alpha = (1.0 - confidence) / 2.0
    if len(stats) == 0:
        return ConfidenceInterval(statistic(x), math.nan, math.nan, confidence)
    lo, hi = np.quantile(stats, [alpha, 1.0 - alpha])
    return ConfidenceInterval(statistic(x), float(lo), float(hi), confidence)


# ------------------------------------------------------------------ Reality Check / SPA
@dataclass(frozen=True)
class RealityCheckResult:
    best: str
    best_mean_excess: float  # per period
    reality_check_p: float  # White (2000)
    spa_p: float  # Hansen (2005), consistent
    n_models: int
    samples: int


def reality_check(
    excess: pd.DataFrame, *, samples: int, mean_block: float, seed: int
) -> RealityCheckResult:
    """Is the best model's mean excess return over the benchmark larger than luck allows?

    ``excess`` holds per-period *performance differentials* (model return −
    benchmark return), one column per model. H0: no model beats the benchmark.
    """
    d = excess.to_numpy(dtype=float)
    n, k = d.shape
    if n < 10 or k < 1:
        raise ValueError("reality check needs >= 10 observations and >= 1 model")
    dbar = d.mean(axis=0)
    idx = stationary_bootstrap_indices(n, mean_block, samples, np.random.default_rng(seed))
    boot_means = np.stack([d[row].mean(axis=0) for row in idx])  # (samples, k)
    root_n = math.sqrt(n)
    # White's Reality Check: V = max_k √n d̄_k; V* = max_k √n (d̄*_k − d̄_k)
    v = root_n * dbar.max()
    v_star = (root_n * (boot_means - dbar)).max(axis=1)
    rc_p = float((v_star >= v).mean())
    # Hansen SPA (consistent): studentised statistic, recentre only models that are not poor
    omega = root_n * (boot_means - dbar).std(axis=0, ddof=1)
    omega = np.where(omega > 1e-15, omega, np.inf)
    t_spa = max(float((root_n * dbar / omega).max()), 0.0)
    threshold = -math.sqrt(2.0 * math.log(math.log(n))) if n > 15 else -math.inf
    # g_c(d̄): models far below the benchmark keep their (negative) mean instead of being
    # recentred to zero, so hopeless models cannot inflate the null distribution
    g_c = np.where(root_n * dbar / omega >= threshold, dbar, 0.0)
    t_star = np.maximum((root_n * (boot_means - g_c) / omega).max(axis=1), 0.0)
    spa_p = float((t_star >= t_spa).mean()) if t_spa > 0 else 1.0
    best = int(np.argmax(dbar))
    return RealityCheckResult(
        best=str(excess.columns[best]),
        best_mean_excess=float(dbar[best]),
        reality_check_p=rc_p,
        spa_p=spa_p,
        n_models=k,
        samples=samples,
    )


# ------------------------------------------------------------------ PBO / CSCV
@dataclass(frozen=True)
class PBOResult:
    pbo: float  # share of combinations where the IS-best trial ranks <= median OOS
    logits: tuple[float, ...]
    n_combinations: int
    n_trials: int
    partitions: int
    degradation_slope: float  # OLS slope of OOS vs IS performance of the IS-best trial
    prob_oos_loss: float  # share of combinations where the IS-best trial has OOS SR < 0


def pbo_cscv(
    returns: pd.DataFrame,
    partitions: int = 16,
    metric: Callable[[np.ndarray], np.ndarray] | None = None,
) -> PBOResult:
    """Probability of Backtest Overfitting via Combinatorially Symmetric Cross-Validation.

    ``returns``: rows = periods (in time order), columns = trials. The rows are
    cut into ``partitions`` contiguous blocks; for every choice of half the
    blocks as in-sample, the in-sample best trial's out-of-sample relative rank
    ω is recorded as the logit λ = ln(ω / (1 − ω)). PBO = share of λ <= 0.
    """
    m = returns.to_numpy(dtype=float)
    t, n = m.shape
    if n < 2:
        raise ValueError("PBO needs at least two trials")
    if partitions < 2 or partitions % 2:
        raise ValueError("partitions must be an even number >= 2")
    if t < partitions * 2:
        raise ValueError(f"PBO needs at least {partitions * 2} observations")
    blocks = np.array_split(np.arange(t), partitions)
    if metric is None:
        # Sharpe from per-block sufficient statistics: O(combinations x blocks x trials)
        cnt = np.array([len(b) for b in blocks], dtype=float)
        s1 = np.stack([m[b].sum(axis=0) for b in blocks])
        s2 = np.stack([(m[b] ** 2).sum(axis=0) for b in blocks])

        def perf_of(sel: list[int], rows: np.ndarray) -> np.ndarray:
            k = cnt[sel].sum()
            mean = s1[sel].sum(axis=0) / k
            var = (s2[sel].sum(axis=0) - k * mean**2) / (k - 1)
            sd = np.sqrt(np.maximum(var, 0.0))
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.where(sd > 1e-12, mean / sd, np.nan)

    else:
        custom = metric

        def perf_of(sel: list[int], rows: np.ndarray) -> np.ndarray:
            return np.asarray(custom(m[rows]), dtype=float)

    logits: list[float] = []
    is_best: list[float] = []
    oos_best: list[float] = []
    for combo in combinations(range(partitions), partitions // 2):
        is_sel = list(combo)
        oos_sel = [i for i in range(partitions) if i not in combo]
        is_perf = perf_of(is_sel, np.concatenate([blocks[i] for i in is_sel]))
        oos_perf = perf_of(oos_sel, np.concatenate([blocks[i] for i in oos_sel]))
        best = int(np.argmax(np.where(np.isfinite(is_perf), is_perf, -np.inf)))
        ranks = pd.Series(oos_perf).rank(method="average", na_option="bottom").to_numpy()
        omega = ranks[best] / (n + 1)
        logits.append(math.log(omega / (1 - omega)))
        is_best.append(float(is_perf[best]))
        oos_best.append(float(oos_perf[best]))
    lam = np.array(logits)
    x, y = np.array(is_best), np.array(oos_best)
    ok = np.isfinite(x) & np.isfinite(y)
    slope = (
        float(np.polyfit(x[ok], y[ok], 1)[0])
        if ok.sum() >= 2 and np.ptp(x[ok]) > 1e-15
        else math.nan
    )
    return PBOResult(
        pbo=float((lam <= 0).mean()),
        logits=tuple(float(v) for v in lam),
        n_combinations=len(lam),
        n_trials=n,
        partitions=partitions,
        degradation_slope=slope,
        prob_oos_loss=float((y < 0).mean()),
    )
