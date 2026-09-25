"""Performance metrics (exact definitions; see ``docs/BACKTESTING.md``).

Inputs are a daily equity curve (``pd.Series`` indexed by UTC session-close
timestamps) and, where relevant, exposure/turnover series and round trips.
Daily simple returns ``r_t = E_t / E_{t-1} - 1``; ``P = 252`` periods/year.

* total return = E_last / E_first - 1
* CAGR = (E_last / E_first) ** (365.25 / calendar days) - 1
* annualized return = (1 + total) ** (P / n_returns) - 1
* volatility = std(r, ddof=1) * sqrt(P)
* downside deviation = sqrt(mean(min(r - rf_d, 0)^2)) * sqrt(P)
* Sharpe = mean(r - rf_d) / std(r - rf_d, ddof=1) * sqrt(P), rf_d = rf / P
* Sortino = mean(r - rf_d) * P / downside deviation
* max drawdown = min(E / cummax(E) - 1) (<= 0); duration = longest run of
  sessions spent below a previous peak (peak -> recovery, or to the end)
* Calmar = CAGR / |max drawdown|
* VaR_q = -quantile(r, 1 - q) (historical, linear interpolation);
  ES_q = -mean(r[r <= quantile(r, 1 - q)])
* monthly / annual returns: compounding within exchange-local calendar
  months / years; the first period is measured from the first equity value
* trade statistics come from FIFO round trips, net of all costs

Undefined values (e.g. Sharpe with zero volatility) are ``None``, never inf.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from adaptive_quant.core.clock import MARKET_TZ

P = 252


def _num(x: float) -> float | None:
    return float(x) if x is not None and math.isfinite(float(x)) else None


def daily_returns(equity: pd.Series[float]) -> pd.Series[float]:
    if (equity <= 0).any():
        raise ValueError("equity must stay positive")
    return equity.pct_change().dropna()


def total_return(equity: pd.Series[float]) -> float:
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)


def cagr(equity: pd.Series[float]) -> float | None:
    days = (equity.index[-1] - equity.index[0]).days
    if days <= 0:
        return None
    return _num((equity.iloc[-1] / equity.iloc[0]) ** (365.25 / days) - 1.0)


def annualized_return(equity: pd.Series[float]) -> float | None:
    n = len(equity) - 1
    return None if n <= 0 else _num((1 + total_return(equity)) ** (P / n) - 1.0)


def volatility(r: pd.Series[float]) -> float | None:
    return None if len(r) < 2 else _num(r.std(ddof=1) * math.sqrt(P))


def downside_deviation(r: pd.Series[float], rf_annual: float = 0.0) -> float | None:
    if len(r) == 0:
        return None
    ex = r - rf_annual / P
    return _num(math.sqrt(float((np.minimum(ex, 0.0) ** 2).mean())) * math.sqrt(P))


def sharpe(r: pd.Series[float], rf_annual: float = 0.0) -> float | None:
    if len(r) < 2:
        return None
    ex = r - rf_annual / P
    sd = ex.std(ddof=1)
    return None if not sd or sd < 1e-15 else _num(ex.mean() / sd * math.sqrt(P))


def sortino(r: pd.Series[float], rf_annual: float = 0.0) -> float | None:
    dd = downside_deviation(r, rf_annual)
    if dd is None or dd < 1e-15:
        return None
    return _num((r - rf_annual / P).mean() * P / dd)


def drawdown_series(equity: pd.Series[float]) -> pd.Series[float]:
    return equity / equity.cummax() - 1.0


def max_drawdown(equity: pd.Series[float]) -> float:
    return float(drawdown_series(equity).min())


def max_drawdown_duration(equity: pd.Series[float]) -> int:
    """Longest number of sessions spent below a prior peak."""
    below = (drawdown_series(equity) < 0).to_numpy()
    longest = run = 0
    for b in below:
        run = run + 1 if b else 0
        longest = max(longest, run)
    return longest


def calmar(equity: pd.Series[float]) -> float | None:
    mdd, c = max_drawdown(equity), cagr(equity)
    return None if c is None or mdd >= 0 else _num(c / abs(mdd))


def var_es(r: pd.Series[float], level: float = 0.95) -> tuple[float | None, float | None]:
    if len(r) == 0:
        return None, None
    q = float(np.quantile(r.to_numpy(), 1.0 - level))
    tail = r[r <= q]
    return _num(-q), _num(-tail.mean()) if len(tail) else None


def period_returns(equity: pd.Series[float], freq: str) -> pd.Series[float]:
    """Compounded returns per exchange-local calendar month ('M') or year ('Y')."""
    local = equity.copy()
    local.index = pd.DatetimeIndex(equity.index).tz_convert(MARKET_TZ)
    key = [local.index.year, local.index.month] if freq == "M" else [local.index.year]
    ends = local.groupby(key).last()
    starts_prev = ends.shift(1)
    starts_prev.iloc[0] = local.iloc[0]
    out = ends / starts_prev - 1.0
    if freq == "M":
        out.index = pd.PeriodIndex([pd.Period(year=y, month=m, freq="M") for y, m in out.index])
    else:
        out.index = pd.Index([int(y) for y in out.index], name="year")
    return out.astype("float64")


@dataclass(frozen=True)
class TradeStats:
    count: int
    win_rate: float | None
    profit_factor: float | None
    average: float | None
    median: float | None
    best: float | None
    worst: float | None
    average_holding_days: float | None


def trade_stats(
    pnls: Sequence[float], returns: Sequence[float], holding_days: Sequence[float]
) -> TradeStats:
    """Stats over round trips; ``average``/``median``/``best``/``worst`` are trade returns."""
    n = len(pnls)
    if n == 0:
        return TradeStats(0, None, None, None, None, None, None, None)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_loss = -sum(losses)
    return TradeStats(
        count=n,
        win_rate=len(wins) / n,
        profit_factor=(sum(wins) / gross_loss) if gross_loss > 0 else None,
        average=float(np.mean(returns)),
        median=float(np.median(returns)),
        best=float(max(returns)),
        worst=float(min(returns)),
        average_holding_days=float(np.mean(holding_days)),
    )


def beta_correlation(
    r: pd.Series[float], bench: pd.Series[float]
) -> tuple[float | None, float | None]:
    both = pd.concat([r, bench], axis=1, join="inner").dropna()
    if len(both) < 2:
        return None, None
    a, b = both.iloc[:, 0], both.iloc[:, 1]
    var_b = b.var(ddof=1)
    beta = _num(a.cov(b) / var_b) if var_b and var_b > 1e-18 else None
    corr = _num(a.corr(b)) if a.std() > 0 and b.std() > 0 else None
    return beta, corr


def performance_summary(
    equity: pd.Series[float],
    *,
    rf_annual: float = 0.0,
    exposure: pd.Series[float] | None = None,
    turnover: pd.Series[float] | None = None,
    trades: TradeStats | None = None,
    benchmark: pd.Series[float] | None = None,
) -> dict[str, float | int | None]:
    """All headline metrics for one equity curve, as a flat dict (None = undefined)."""
    r = daily_returns(equity)
    var95, es95 = var_es(r, 0.95)
    var99, es99 = var_es(r, 0.99)
    months = period_returns(equity, "M")
    years = period_returns(equity, "Y")
    years_span = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    out: dict[str, float | int | None] = {
        "start_equity": float(equity.iloc[0]),
        "end_equity": float(equity.iloc[-1]),
        "total_return": total_return(equity),
        "cagr": cagr(equity),
        "annualized_return": annualized_return(equity),
        "volatility": volatility(r),
        "downside_volatility": downside_deviation(r, rf_annual),
        "sharpe": sharpe(r, rf_annual),
        "sortino": sortino(r, rf_annual),
        "max_drawdown": max_drawdown(equity),
        "max_drawdown_duration_sessions": max_drawdown_duration(equity),
        "calmar": calmar(equity),
        "var_95": var95,
        "es_95": es95,
        "var_99": var99,
        "es_99": es99,
        "best_month": _num(months.max()) if len(months) else None,
        "worst_month": _num(months.min()) if len(months) else None,
        "winning_months_pct": _num((months > 0).mean()) if len(months) else None,
        "best_year": _num(years.max()) if len(years) else None,
        "worst_year": _num(years.min()) if len(years) else None,
        "sessions": len(equity),
    }
    if exposure is not None:
        out["average_exposure"] = _num(exposure.mean())
        out["time_invested_pct"] = _num((exposure > 1e-6).mean())
    if turnover is not None:
        out["annual_turnover"] = _num(turnover.sum() / years_span)
    if trades is not None:
        out.update(
            {
                "trades": trades.count,
                "win_rate": trades.win_rate,
                "profit_factor": trades.profit_factor,
                "average_trade": trades.average,
                "median_trade": trades.median,
                "best_trade": trades.best,
                "worst_trade": trades.worst,
                "average_holding_days": trades.average_holding_days,
            }
        )
    if benchmark is not None:
        b_eq = benchmark.reindex(equity.index).dropna()
        if len(b_eq) >= 2:
            beta, corr = beta_correlation(r, daily_returns(b_eq))
            b_cagr = cagr(b_eq)
            c = out["cagr"]
            out["beta"] = beta
            out["correlation"] = corr
            out["excess_cagr"] = (
                _num(float(c) - b_cagr) if isinstance(c, float) and b_cagr is not None else None
            )
    return out
