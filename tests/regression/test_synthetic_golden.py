"""Golden numbers for the synthetic leveraged-ETF model (hand-computed)."""

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.data.synthetic import (
    FinancingAssumptions,
    LeveragedProductSpec,
    synthesize_leveraged_returns,
)

pytestmark = pytest.mark.regression

# Tue, Wed, then Mon after a weekend: dt = 1, 1... and 5 calendar days / 365
IDX = pd.DatetimeIndex(
    [datetime(2024, 1, d, 21, tzinfo=UTC) for d in (2, 3, 4)]
    + [datetime(2024, 1, 8, 21, tzinfo=UTC)]
)
CLOSE = pd.Series([100.0, 101.0, 99.99, 99.99], index=IDX)
RF = pd.Series(0.05, index=IDX)
TQQQ = LeveragedProductSpec("TQQQ", 3.0, 0.0095, date(2010, 2, 11))
SQQQ = LeveragedProductSpec("SQQQ", -3.0, 0.0095, date(2010, 2, 11))


def test_tqqq_daily_formula() -> None:
    fin = FinancingAssumptions(swap_spread=0.004, underlying_expense_addback=0.002)
    r, _ = synthesize_leveraged_returns(CLOSE, TQQQ, RF, fin)
    dt = 1 / 365
    expected_day1 = 3 * (0.01 + 0.002 * dt) - 0.0095 * dt - 2 * 0.05 * dt - 2 * 0.004 * dt
    assert r.iloc[1] == pytest.approx(expected_day1, rel=1e-12)
    dt_weekend = 4 / 365  # Thu -> Mon
    expected_mon = (
        3 * (0.0 + 0.002 * dt_weekend)
        - 0.0095 * dt_weekend
        - 2 * 0.05 * dt_weekend
        - 2 * 0.004 * dt_weekend
    )
    assert r.iloc[3] == pytest.approx(expected_mon, rel=1e-12)
    assert np.isnan(r.iloc[0])


def test_sqqq_earns_financing_credit() -> None:
    fin = FinancingAssumptions(swap_spread=0.004)
    r, _ = synthesize_leveraged_returns(CLOSE, SQQQ, RF, fin)
    dt = 1 / 365
    expected = -3 * 0.01 - 0.0095 * dt + 4 * 0.05 * dt - 3 * 0.004 * dt
    assert r.iloc[1] == pytest.approx(expected, rel=1e-12)
