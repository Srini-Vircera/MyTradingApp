"""Market regimes for consistency analysis (point-in-time labels).

Each session's return is attributed to the regime *known before that session*:
labels are computed from closes through the previous session (``shift(1)``),
using the causal M3 indicators.

* ``trend``      - QQQ above / below its 200-session SMA
* ``volatility`` - 20-session realised-vol percentile within 252 sessions:
                   low (< 1/3), normal, high (> 2/3)
* ``decade``     - calendar decade (exchange-local)

Consistency = share of regime buckets (with enough sessions) in which the
strategy's return beat cash, i.e. its mean daily return was positive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.quant.indicators.trend import sma
from adaptive_quant.quant.indicators.volatility import volatility_percentile

MIN_BUCKET_SESSIONS = 60
P = 252


def regime_labels(close: pd.Series, index: pd.DatetimeIndex) -> pd.DataFrame:
    """One row per session in ``index``; columns trend / volatility / decade."""
    trend_raw = np.where(close > sma(close, 200), "uptrend", "downtrend")
    trend = pd.Series(trend_raw, index=close.index).where(sma(close, 200).notna())
    pct = volatility_percentile(close, 20, 252)
    vol = pd.Series(
        np.select([pct < 1 / 3, pct > 2 / 3], ["low vol", "high vol"], "normal vol"),
        index=close.index,
    ).where(pct.notna())
    known = pd.DataFrame({"trend": trend, "volatility": vol}).shift(1).reindex(index)
    local = pd.DatetimeIndex(index).tz_convert(MARKET_TZ)
    known["decade"] = [f"{y // 10 * 10}s" for y in local.year]
    return known


@dataclass(frozen=True)
class RegimeStat:
    dimension: str
    regime: str
    sessions: int
    annual_return: float
    sharpe: float


@dataclass(frozen=True)
class RegimeReport:
    stats: tuple[RegimeStat, ...]
    consistency: float  # share of qualifying buckets with a positive mean return

    def by_dimension(self, dim: str) -> list[RegimeStat]:
        return [s for s in self.stats if s.dimension == dim]


def regime_report(returns: pd.Series, labels: pd.DataFrame) -> RegimeReport:
    stats: list[RegimeStat] = []
    for dim in ("trend", "volatility", "decade"):
        lab = labels[dim].reindex(returns.index)
        for regime in sorted(lab.dropna().unique()):
            r = returns[lab == regime].to_numpy(dtype=float)
            if len(r) == 0:
                continue
            sd = float(np.std(r, ddof=1)) if len(r) > 1 else 0.0
            stats.append(
                RegimeStat(
                    dimension=dim,
                    regime=str(regime),
                    sessions=len(r),
                    annual_return=float(np.prod(1 + r) ** (P / len(r)) - 1),
                    sharpe=float(np.mean(r)) / sd * math.sqrt(P) if sd > 1e-15 else math.nan,
                )
            )
    qualifying = [s for s in stats if s.sessions >= MIN_BUCKET_SESSIONS]
    consistency = (
        sum(s.annual_return > 0 for s in qualifying) / len(qualifying) if qualifying else math.nan
    )
    return RegimeReport(tuple(stats), consistency)
