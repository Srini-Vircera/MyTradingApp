"""Monte Carlo path metrics, adverse percentiles and resampling dimensions."""

import math

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.research import montecarlo as mc


def test_path_metrics_hand_values() -> None:
    r = np.array([0.10, -0.50, 0.20])
    m = mc.path_metrics(r, 1000.0)
    assert m.ending_equity == pytest.approx(1000 * 1.1 * 0.5 * 1.2)
    assert m.max_drawdown == pytest.approx(-0.5)
    assert m.worst_year == pytest.approx(1.1 * 0.5 * 1.2 - 1)  # one 252-block
    assert m.cagr == pytest.approx(0.66 ** (252 / 3) - 1)
    sd = np.std(r, ddof=1)
    assert m.sharpe == pytest.approx(r.mean() / sd * math.sqrt(252))
    empty = mc.path_metrics(np.array([]), 5.0)
    assert math.isnan(empty.cagr) and empty.ending_equity == 5.0


def test_worst_year_uses_calendar_years_when_dated() -> None:
    idx = pd.DatetimeIndex(["2020-12-30", "2020-12-31", "2021-01-04"], tz="UTC") + pd.Timedelta(
        hours=21
    )
    m = mc.path_metrics(np.array([0.1, 0.1, -0.3]), 1.0, idx)
    assert m.worst_year == pytest.approx(-0.3)


def test_distribution_reports_adverse_percentiles() -> None:
    d = mc.distribution([float(x) for x in range(1, 101)] + [math.nan])
    assert d.n == 100
    assert d.median == pytest.approx(50.5)
    assert d.p90 == pytest.approx(np.quantile(np.arange(1, 101), 0.10))
    assert d.p95 < d.p90 < d.p75 < d.median
    assert d.worst == 1.0
    assert mc.distribution([]).n == 0


def test_resampling_dimensions_are_seeded_and_sane() -> None:
    idx = pd.date_range("2015-01-01", periods=1500, freq="B", tz="UTC")
    r = pd.Series(np.random.default_rng(0).normal(0.0004, 0.01, 1500), index=idx)
    a = mc.return_blocks(r, 1e5, 50, 10.0, seed=1)
    assert a == mc.return_blocks(r, 1e5, 50, 10.0, seed=1)
    assert len(a) == 50
    const = pd.Series(0.001, index=idx)
    for p in mc.return_blocks(const, 1.0, 5, 10.0, seed=2):  # constant returns: every path equal
        assert p.ending_equity == pytest.approx(1.001**1500)
    starts = mc.start_dates(r, 1.0, 40, 0.33, 3.0, seed=3)
    assert len(starts) == 40
    assert mc.start_dates(r[:700], 1.0, 10, 0.33, 3.0, seed=3) == []  # < 3 years left


def test_trade_sequence() -> None:
    same = mc.trade_sequence([0.01] * 20, 2.0, 100.0, 30, seed=0)
    assert all(p.ending_equity == pytest.approx(100 * 1.01**20) for p in same)
    assert all(p.max_drawdown == 0.0 and math.isnan(p.sharpe) for p in same)
    mixed = mc.trade_sequence([0.05, -0.04, 0.02, -0.01], 1.0, 1.0, 200, seed=1)
    assert min(p.max_drawdown for p in mixed) < -0.04
    assert mc.trade_sequence([], 1.0, 1.0, 10, seed=0) == []
    wiped = mc.trade_sequence([-1.5], 1.0, 1.0, 3, seed=0)
    assert all(p.ending_equity == 0.0 for p in wiped)


def test_rounding_avoids_false_precision() -> None:
    assert mc.rounded("cagr", 0.12345) == 0.125
    assert mc.rounded("max_drawdown", -0.2371) == -0.235
    assert mc.rounded("sharpe", 0.8123) == 0.8
    assert mc.rounded("ending_equity", 123456.7) == 123000.0
    assert mc.rounded("cagr", math.nan) is None
