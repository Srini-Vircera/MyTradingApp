"""No look-ahead: every indicator value at t depends only on rows <= t.

Two independent checks per indicator configuration:

1. **Truncation** - computing on ``bars[: t + 1]`` gives exactly the value the
   full-history computation reports at ``t``.
2. **Future perturbation** - arbitrarily changing every row after ``t`` leaves
   all values up to ``t`` unchanged.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.indicators.specs import REGISTRY, IndicatorSpec
from tests.data_helpers import daily_bars
from tests.unit.indicators.cases import CASES, ids

BARS = daily_bars(date(2021, 1, 4), date(2022, 12, 30), vol=0.015, seed=21)  # ~500 rows
CUTS = sorted({0, 1, 2, 5, 13, 14, 19, 20, 21, 62, 63, 64, 69, 70, 150, 333, len(BARS) - 2})


def assert_same(a: np.ndarray, b: np.ndarray) -> None:
    np.testing.assert_allclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True)


def test_every_registered_kind_is_covered() -> None:
    assert {c.kind for c in CASES} == set(REGISTRY)


@pytest.mark.parametrize("spec", CASES, ids=ids)
def test_truncated_history_gives_identical_values(spec: IndicatorSpec) -> None:
    full = spec.compute(BARS).to_numpy()
    for t in CUTS:
        partial = spec.compute(BARS.iloc[: t + 1]).to_numpy()
        assert len(partial) == t + 1
        assert_same(partial, full[: t + 1])


@pytest.mark.parametrize("spec", CASES, ids=ids)
def test_changing_the_future_does_not_change_the_past(spec: IndicatorSpec) -> None:
    full = spec.compute(BARS).to_numpy()
    rng = np.random.default_rng(99)
    for t in (30, 120, 400):
        future = BARS.copy()
        shock = pd.Series(np.exp(rng.normal(0, 0.2, len(BARS))), index=BARS.index)
        shock.iloc[: t + 1] = 1.0  # rows <= t untouched
        for col in ("open", "high", "low", "close"):
            future[col] = future[col] * shock
        future["volume"] = future["volume"] * shock
        changed = spec.compute(future).to_numpy()
        assert_same(changed[: t + 1], full[: t + 1])
        # sanity: the perturbation really changes later values for price-based indicators
        if spec.kind not in {"drawdown_duration"} and np.isfinite(full[t + 1 :]).any():
            assert not np.allclose(changed[t + 1 :], full[t + 1 :], equal_nan=True)
