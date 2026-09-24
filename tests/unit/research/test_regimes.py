"""Regime labels are point-in-time; consistency counts only well-populated buckets."""

import math

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.research.regimes import regime_labels, regime_report


def closes(n: int = 900, seed: int = 0) -> pd.Series:
    idx = pd.date_range("2015-01-01", periods=n, freq="B", tz="UTC") + pd.Timedelta(hours=21)
    r = np.random.default_rng(seed).normal(0.0004, 0.012, n)
    return pd.Series(100 * np.cumprod(1 + r), index=idx)


def test_labels_use_only_prior_sessions() -> None:
    c = closes()
    base = regime_labels(c, pd.DatetimeIndex(c.index))
    for cut in (400, 600, 850):
        poisoned = c.copy()
        poisoned.iloc[cut:] = poisoned.iloc[cut:] * np.linspace(0.2, 5.0, len(c) - cut)
        lab = regime_labels(poisoned, pd.DatetimeIndex(c.index))
        # a label for session t is known before t: rows up to and including `cut` are unchanged
        pd.testing.assert_frame_equal(base.iloc[: cut + 1], lab.iloc[: cut + 1])
    # SMA(200) is first defined on row 199; shifted by one session it labels row 200
    assert base["trend"].iloc[:200].isna().all() and base["trend"].notna().iloc[200]
    assert set(base["trend"].dropna()) <= {"uptrend", "downtrend"}
    assert set(base["volatility"].dropna()) <= {"low vol", "normal vol", "high vol"}
    assert set(base["decade"]) == {"2010s"}


def test_regime_report_and_consistency() -> None:
    c = closes()
    idx = pd.DatetimeIndex(c.index)
    labels = regime_labels(c, idx)
    r = pd.Series(0.001, index=idx)
    rep = regime_report(r, labels)
    assert rep.consistency == 1.0
    up = next(s for s in rep.by_dimension("trend") if s.regime == "uptrend")
    assert up.annual_return == pytest.approx(1.001**252 - 1)
    assert math.isnan(up.sharpe)  # zero volatility
    neg = regime_report(-r, labels)
    assert neg.consistency == 0.0
