"""Golden values for every performance metric, derived by hand (see comments)."""

import math
from datetime import UTC, datetime

import pandas as pd
import pytest

from adaptive_quant.quant.analytics import metrics as m

pytestmark = pytest.mark.regression

# Five consecutive sessions (Mon-Fri), equity 100 -> 110 -> 99 -> 108.9 -> 119.79
# returns r = [+10%, -10%, +10%, +10%]; calendar span 4 days
IDX = pd.DatetimeIndex([datetime(2021, 1, d, 21, tzinfo=UTC) for d in (4, 5, 6, 7, 8)])
EQ = pd.Series([100.0, 110.0, 99.0, 108.9, 119.79], index=IDX)
R = m.daily_returns(EQ)
SQ252 = math.sqrt(252)


def test_returns_and_growth() -> None:
    assert R.tolist() == pytest.approx([0.1, -0.1, 0.1, 0.1])
    assert m.total_return(EQ) == pytest.approx(0.1979)
    assert m.cagr(EQ) == pytest.approx(1.1979 ** (365.25 / 4) - 1)
    assert m.annualized_return(EQ) == pytest.approx(1.1979 ** (252 / 4) - 1)


def test_volatility_sharpe_sortino() -> None:
    # mean 0.05; deviations .05 -.15 .05 .05 -> sum sq .03 / 3 = .01 -> std .1
    assert m.volatility(R) == pytest.approx(0.1 * SQ252)  # 1.587451
    assert m.sharpe(R) == pytest.approx(0.05 / 0.1 * SQ252)  # 7.937254
    # downside: sqrt(mean([0, .01, 0, 0])) = .05 -> annualized .05*sqrt(252)
    assert m.downside_deviation(R) == pytest.approx(0.05 * SQ252)  # 0.793725
    assert m.sortino(R) == pytest.approx(0.05 * 252 / (0.05 * SQ252))  # 15.874508
    # with a 25.2% risk-free rate: daily rf = .001 -> excess mean .049, std unchanged
    assert m.sharpe(R, 0.252) == pytest.approx(0.049 / 0.1 * SQ252)


def test_drawdown_metrics() -> None:
    # peaks 100,110,110,110,119.79 -> drawdowns 0,0,-.1,-.01,0
    assert m.drawdown_series(EQ).tolist() == pytest.approx([0, 0, -0.1, -0.01, 0])
    assert m.max_drawdown(EQ) == pytest.approx(-0.1)
    assert m.max_drawdown_duration(EQ) == 2
    assert m.calmar(EQ) == pytest.approx((1.1979 ** (365.25 / 4) - 1) / 0.1)


def test_var_and_expected_shortfall() -> None:
    # sorted [-.1,.1,.1,.1]; 5% quantile, linear: -.1 + 0.15*(.2) = -.07
    var, es = m.var_es(R, 0.95)
    assert var == pytest.approx(0.07)
    assert es == pytest.approx(0.10)


def test_month_and_year_boundaries() -> None:
    idx = pd.DatetimeIndex(
        [
            datetime(2020, 12, 30, 21, tzinfo=UTC),
            datetime(2020, 12, 31, 21, tzinfo=UTC),
            datetime(2021, 1, 4, 21, tzinfo=UTC),
            datetime(2021, 2, 1, 21, tzinfo=UTC),
        ]
    )
    eq = pd.Series([100.0, 105.0, 94.5, 99.225], index=idx)
    months = m.period_returns(eq, "M")
    assert months.tolist() == pytest.approx([0.05, -0.10, 0.05])  # Dec (from first value), Jan, Feb
    years = m.period_returns(eq, "Y")
    assert years.to_dict() == pytest.approx({2020: 0.05, 2021: 99.225 / 105 - 1})


def test_periods_use_exchange_local_dates() -> None:
    # 2021-02-01 00:30 UTC is still 31 Jan in New York -> belongs to January
    idx = pd.DatetimeIndex(
        [datetime(2021, 1, 29, 21, tzinfo=UTC), datetime(2021, 2, 1, 0, 30, tzinfo=UTC)]
    )
    months = m.period_returns(pd.Series([100.0, 101.0], index=idx), "M")
    assert len(months) == 1


def test_trade_statistics() -> None:
    t = m.trade_stats([100.0, -50.0, 30.0, -20.0], [0.1, -0.05, 0.03, -0.02], [1, 2, 3, 4])
    assert t.count == 4
    assert t.win_rate == pytest.approx(0.5)
    assert t.profit_factor == pytest.approx(130 / 70)
    assert t.average == pytest.approx(0.015)
    assert t.median == pytest.approx(0.005)
    assert t.best == pytest.approx(0.1)
    assert t.worst == pytest.approx(-0.05)
    assert t.average_holding_days == pytest.approx(2.5)
    assert m.trade_stats([10.0], [0.1], [1]).profit_factor is None  # no losses: undefined, not inf
    assert m.trade_stats([], [], []).count == 0


def test_beta_and_correlation() -> None:
    b = pd.Series([0.01, -0.02, 0.015, 0.003, -0.007])
    beta, corr = m.beta_correlation(2 * b, b)
    assert beta == pytest.approx(2.0)
    assert corr == pytest.approx(1.0)


def test_undefined_values_are_none_not_inf() -> None:
    flat = pd.Series([100.0, 100.0, 100.0], index=IDX[:3])
    r = m.daily_returns(flat)
    assert m.sharpe(r) is None
    assert m.sortino(r) is None
    assert m.calmar(flat) is None
    assert m.max_drawdown(flat) == 0.0


def test_summary_contains_every_planned_metric() -> None:
    s = m.performance_summary(
        EQ,
        exposure=pd.Series(0.5, index=IDX),
        turnover=pd.Series(0.1, index=IDX),
        trades=m.trade_stats([1.0], [0.01], [1]),
        benchmark=EQ,
    )
    for key in (
        "cagr",
        "total_return",
        "annualized_return",
        "volatility",
        "downside_volatility",
        "sharpe",
        "sortino",
        "calmar",
        "max_drawdown",
        "max_drawdown_duration_sessions",
        "var_95",
        "es_95",
        "var_99",
        "es_99",
        "best_month",
        "worst_month",
        "winning_months_pct",
        "best_year",
        "worst_year",
        "average_exposure",
        "time_invested_pct",
        "annual_turnover",
        "trades",
        "win_rate",
        "profit_factor",
        "average_trade",
        "median_trade",
        "best_trade",
        "worst_trade",
        "average_holding_days",
        "beta",
        "correlation",
        "excess_cagr",
    ):
        assert key in s, key
    assert s["excess_cagr"] == pytest.approx(0.0)
    assert s["beta"] == pytest.approx(1.0)
    assert s["annual_turnover"] == pytest.approx(0.5 / (4 / 365.25))


def test_non_positive_equity_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        m.daily_returns(pd.Series([100.0, 0.0], index=IDX[:2]))
