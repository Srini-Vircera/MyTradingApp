"""Warm-up: NaN until the full window is available - never a partial value."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.indicators.trend import ema, sma
from tests.data_helpers import daily_bars
from tests.unit.indicators.cases import CASES, ids

BARS = daily_bars(date(2021, 1, 4), date(2022, 12, 30), vol=0.015, seed=5)


@pytest.mark.parametrize("spec", CASES, ids=ids)
def test_declared_warmup_matches_output(spec: IndicatorSpec) -> None:
    out = spec.compute(BARS).to_numpy()
    w = spec.warmup
    assert np.isnan(out[:w]).all(), f"{spec.name}: value before warm-up of {w}"
    assert np.isfinite(out[w:]).all(), f"{spec.name}: NaN after warm-up of {w}"


@pytest.mark.parametrize("spec", [c for c in CASES if c.warmup > 0], ids=ids)
def test_too_little_history_gives_only_nan(spec: IndicatorSpec) -> None:
    short = BARS.iloc[: spec.warmup]  # one row short of the first valid value
    assert np.isnan(spec.compute(short).to_numpy()).all()


@pytest.mark.parametrize("spec", CASES, ids=ids)
def test_first_value_needs_exactly_warmup_plus_one_rows(spec: IndicatorSpec) -> None:
    just_enough = BARS.iloc[: spec.warmup + 1]
    out = spec.compute(just_enough).to_numpy()
    assert np.isfinite(out[-1])
    assert np.isnan(out[:-1]).all()


def test_chained_indicators_propagate_warmup() -> None:
    x = BARS["close"]
    inner = sma(x, 10)  # 9 leading NaN
    outer = ema(inner, 5)  # seed needs 5 valid values -> 9 + 4 = 13 leading NaN
    arr = outer.to_numpy()
    assert np.isnan(arr[:13]).all()
    assert np.isfinite(arr[13:]).all()
    assert outer.iloc[13] == pytest.approx(inner.iloc[9:14].mean())


def test_empty_input_returns_empty() -> None:
    for spec in CASES:
        assert spec.compute(BARS.iloc[0:0]).empty


def test_window_of_one_has_no_warmup() -> None:
    x = pd.Series([3.0, 1.0, 2.0])
    assert sma(x, 1).tolist() == [3.0, 1.0, 2.0]
