"""Monte Carlo robustness analysis.

Dimensions (``docs/RESEARCH.md``):

* ``trade_sequence``   - bootstrap of round-trip returns (with replacement)
* ``return_blocks``    - stationary block bootstrap of daily returns
* ``costs``            - re-simulations with spread/slippage/impact scaled by a random factor
* ``parameters``       - every parameter set in the plateau neighbourhood of the choice
* ``start_date``       - random later start dates (returns after the start)
* ``signal_delay``     - re-simulations with 0/1/2 sessions of extra execution delay

The engine-based dimensions (``costs``, ``signal_delay``) are produced by the
pipeline with real re-simulations; this module turns any collection of return
paths into the percentile table. Everything is seeded and deterministic.

Percentiles are reported in the *adverse* direction: "p90" is the value that
90 % of simulations beat. Values are rounded for display to avoid false precision.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.quant.research.stats import stationary_bootstrap_indices

P = 252
METRICS = ("cagr", "max_drawdown", "worst_year", "sharpe", "ending_equity")
ADVERSE = (("median", 0.50), ("p75", 0.75), ("p90", 0.90), ("p95", 0.95))


@dataclass(frozen=True)
class PathMetrics:
    cagr: float
    max_drawdown: float
    worst_year: float
    sharpe: float
    ending_equity: float


@dataclass(frozen=True)
class Distribution:
    n: int
    median: float
    p75: float
    p90: float
    p95: float
    worst: float


def path_metrics(
    r: np.ndarray, initial: float, index: pd.DatetimeIndex | None = None
) -> PathMetrics:
    """Metrics of one return path. ``index`` (if given) assigns calendar years for worst year;
    otherwise consecutive 252-return blocks stand in for years."""
    r = np.asarray(r, dtype=float)
    if len(r) == 0:
        return PathMetrics(math.nan, math.nan, math.nan, math.nan, initial)
    growth = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(np.concatenate([[1.0], growth]))[1:]
    mdd = float(min((growth / peak - 1.0).min(), 0.0))
    years = len(r) / P
    end = float(growth[-1])
    cagr = end ** (1.0 / years) - 1.0 if end > 0 else -1.0
    sd = float(np.std(r, ddof=1)) if len(r) > 1 else 0.0
    sharpe = float(np.mean(r)) / sd * math.sqrt(P) if sd > 1e-15 else math.nan
    if index is not None:
        years_id = np.asarray(pd.DatetimeIndex(index).tz_convert(MARKET_TZ).year)
    else:
        years_id = np.arange(len(r)) // P
    yearly = [float(np.prod(1.0 + r[years_id == y]) - 1.0) for y in np.unique(years_id)]
    return PathMetrics(cagr, mdd, min(yearly), sharpe, initial * end)


def distribution(values: Sequence[float]) -> Distribution:
    """Adverse-direction percentiles for a higher-is-better metric (drawdowns are negative)."""
    v = np.array([x for x in values if math.isfinite(x)], dtype=float)
    if len(v) == 0:
        return Distribution(0, math.nan, math.nan, math.nan, math.nan, math.nan)
    q = {name: float(np.quantile(v, 1.0 - p)) for name, p in ADVERSE}
    return Distribution(len(v), q["median"], q["p75"], q["p90"], q["p95"], float(v.min()))


def table(paths: Sequence[PathMetrics]) -> dict[str, Distribution]:
    return {m: distribution([getattr(p, m) for p in paths]) for m in METRICS}


# ------------------------------------------------------------------ resampling dimensions
def return_blocks(
    r: pd.Series, initial: float, sims: int, mean_block: float, seed: int
) -> list[PathMetrics]:
    x = r.to_numpy(dtype=float)
    idx = stationary_bootstrap_indices(len(x), mean_block, sims, np.random.default_rng(seed))
    index = pd.DatetimeIndex(r.index)
    return [path_metrics(x[row], initial, index) for row in idx]


def trade_sequence(
    trade_returns: Sequence[float], years: float, initial: float, sims: int, seed: int
) -> list[PathMetrics]:
    """Resample the round trips (with replacement) and compound them in random order.

    Trade returns are P&L as a fraction of equity at entry. The path spans the
    original ``years``; Sharpe and worst year are undefined for trade paths.
    """
    t = np.asarray(trade_returns, dtype=float)
    if len(t) == 0 or years <= 0:
        return []
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(sims):
        seq = t[rng.integers(0, len(t), len(t))]
        growth = np.cumprod(1.0 + seq)
        if (growth <= 0).any():
            out.append(PathMetrics(-1.0, -1.0, math.nan, math.nan, 0.0))
            continue
        peak = np.maximum.accumulate(np.concatenate([[1.0], growth]))[1:]
        end = float(growth[-1])
        out.append(
            PathMetrics(
                end ** (1.0 / years) - 1.0,
                float(min((growth / peak - 1.0).min(), 0.0)),
                math.nan,
                math.nan,
                initial * end,
            )
        )
    return out


def start_dates(
    r: pd.Series, initial: float, sims: int, max_skip_fraction: float, min_years: float, seed: int
) -> list[PathMetrics]:
    """Start the evaluation at a random later session (up to ``max_skip_fraction`` of the
    sample, leaving at least ``min_years`` of returns)."""
    n = len(r)
    max_skip = min(int(n * max_skip_fraction), n - int(min_years * P))
    if max_skip <= 0:
        return []
    rng = np.random.default_rng(seed)
    out = []
    for skip in rng.integers(0, max_skip + 1, sims):
        part = r.iloc[int(skip) :]
        out.append(path_metrics(part.to_numpy(dtype=float), initial, pd.DatetimeIndex(part.index)))
    return out


# ------------------------------------------------------------------ display rounding
def rounded(metric: str, value: float) -> float | None:
    """Coarse display precision: 0.5 pp for returns/drawdowns, 0.05 for Sharpe,
    3 significant figures for equity."""
    if not math.isfinite(value):
        return None
    if metric == "sharpe":
        return round(round(value / 0.05) * 0.05, 2)
    if metric == "ending_equity":
        if value == 0:
            return 0.0
        digits = 3 - math.floor(math.log10(abs(value))) - 1
        return float(round(value, digits))
    return round(round(value / 0.005) * 0.005, 3)
