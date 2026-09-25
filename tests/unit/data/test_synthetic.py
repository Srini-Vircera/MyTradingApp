from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.core.errors import DataQualityError
from adaptive_quant.quant.data.bars import Frequency
from adaptive_quant.quant.data.synthetic import (
    FinancingAssumptions,
    LeveragedProductSpec,
    align_risk_free,
    constant_risk_free,
    load_risk_free_csv,
    splice_with_real,
    swap_notional,
    synthesize_leveraged_bars,
    synthesize_leveraged_returns,
    tracking_report,
)
from adaptive_quant.quant.data.validation import BarValidator
from tests.data_helpers import calendar, daily_bars

TQQQ = LeveragedProductSpec("TQQQ", 3.0, 0.0, date(2010, 2, 11))
SQQQ = LeveragedProductSpec("SQQQ", -3.0, 0.0, date(2010, 2, 11))
NO_COST = FinancingAssumptions()
UNDER = daily_bars(date(2022, 1, 3), date(2023, 12, 29), vol=0.015)


def rf0(df: pd.DataFrame) -> pd.Series:
    return constant_risk_free(pd.DatetimeIndex(df.index), 0.0)


def test_swap_notional() -> None:
    assert (swap_notional(3), swap_notional(-3), swap_notional(1)) == (2, 3, 0)


def test_volatility_drag_emerges_from_daily_compounding() -> None:
    """+10% / -10% alternating: underlying loses 1% per pair, 3x loses ~9% per pair."""
    rets = [0.0] + [0.10, -0.10] * 10  # 21 sessions; the first close is the base
    under = daily_bars(date(2024, 1, 2), date(2024, 1, 31), returns=rets)
    r, _ = synthesize_leveraged_returns(under["close"], TQQQ, rf0(under), NO_COST)
    lev_total = float(np.prod(1 + r.iloc[1:].to_numpy()) - 1)
    under_total = float(under["close"].iloc[-1] / under["close"].iloc[0] - 1)
    assert under_total == pytest.approx(0.99**10 - 1, rel=1e-9)
    assert lev_total == pytest.approx(0.91**10 - 1, rel=1e-9)
    assert lev_total < 3 * under_total  # not "3x the return"


def test_synthetic_bars_are_valid_and_labelled() -> None:
    for spec in (TQQQ, SQQQ):
        bars, wipeouts = synthesize_leveraged_bars(UNDER, spec, rf0(UNDER), NO_COST)
        assert wipeouts == 0
        assert bars["is_synthetic"].all()
        assert (bars["volume"] == 0).all()
        report = BarValidator(calendar()).validate(
            bars, frequency=Frequency.DAILY, subject=spec.symbol
        )
        assert report.ok, report.summary()


def test_inverse_product_moves_opposite() -> None:
    bars, _ = synthesize_leveraged_bars(UNDER, SQQQ, rf0(UNDER), NO_COST)
    u = UNDER["close"].pct_change().iloc[1:].to_numpy()
    s = bars["close"].pct_change().to_numpy()[1:]
    assert np.allclose(s, -3 * u[1:], rtol=1e-9)


def test_wipeout_is_floored_and_counted() -> None:
    under = daily_bars(date(2024, 1, 2), date(2024, 1, 5), returns=[0.0, -0.40, 0.01, 0.01])
    bars, wipeouts = synthesize_leveraged_bars(under, TQQQ, rf0(under), NO_COST)
    assert wipeouts == 1
    assert bars["close"].iloc[0] == 0.0


def test_risk_free_must_align_and_be_complete() -> None:
    bad = rf0(UNDER).iloc[1:]
    with pytest.raises(DataQualityError, match="aligned"):
        synthesize_leveraged_returns(UNDER["close"], TQQQ, bad, NO_COST)
    gappy = rf0(UNDER).copy()
    gappy.iloc[5] = np.nan
    with pytest.raises(DataQualityError, match="gaps"):
        synthesize_leveraged_returns(UNDER["close"], TQQQ, gappy, NO_COST)


def test_splice_is_continuous_and_flags_rows() -> None:
    synthetic, _ = synthesize_leveraged_bars(UNDER, TQQQ, rf0(UNDER), NO_COST)
    real = synthetic[synthetic.index >= synthetic.index[250]].drop(columns=["is_synthetic"]) * 7.0
    real["volume"] = 1.0
    spliced = splice_with_real(real, synthetic)
    assert spliced["is_synthetic"].iloc[:250].all()  # 250 synthetic rows precede real data
    assert not spliced["is_synthetic"].iloc[250:].any()
    rets_spliced = spliced["close"].pct_change().iloc[1:]
    rets_syn = synthetic["close"].pct_change().iloc[1:]
    assert np.allclose(rets_spliced.to_numpy(), rets_syn.to_numpy(), rtol=1e-9)


def test_splice_requires_coverage() -> None:
    synthetic, _ = synthesize_leveraged_bars(UNDER, TQQQ, rf0(UNDER), NO_COST)
    real = daily_bars(date(2024, 2, 1), date(2024, 3, 1))
    with pytest.raises(DataQualityError, match="does not cover"):
        splice_with_real(real, synthetic)
    with pytest.raises(DataQualityError, match="empty"):
        splice_with_real(real.iloc[0:0], synthetic)


def test_tracking_report_pass_and_fail() -> None:
    synthetic, _ = synthesize_leveraged_bars(UNDER, TQQQ, rf0(UNDER), NO_COST)
    noise = np.random.default_rng(1).normal(0, 0.0005, len(synthetic))
    near = synthetic["close"] * np.cumprod(1 + noise)
    ok = tracking_report("TQQQ", near, synthetic["close"], bound=0.03)
    assert ok.passed
    assert ok.correlation > 0.99
    far = synthetic["close"] * np.cumprod(1 + noise * 20)
    bad = tracking_report("TQQQ", far, synthetic["close"], bound=0.03)
    assert not bad.passed
    assert "FAIL" in bad.summary()


def test_tracking_needs_overlap() -> None:
    s = UNDER["close"]
    with pytest.raises(DataQualityError, match="overlapping"):
        tracking_report("X", s.iloc[:10], s.iloc[:10], bound=0.03)


def test_fred_csv_loading_and_alignment(tmp_path: Path) -> None:
    path = tmp_path / "DTB3.csv"
    path.write_text("observation_date,DTB3\n2021-12-30,0.05\n2021-12-31,.\n2022-06-01,1.10\n")
    rates = load_risk_free_csv(path)
    assert rates.loc[date(2021, 12, 31)] == pytest.approx(0.0005)  # forward-filled
    aligned = align_risk_free(rates, pd.DatetimeIndex(UNDER.index))
    assert aligned.iloc[0] == pytest.approx(0.0005)
    assert aligned.iloc[-1] == pytest.approx(0.011)


def test_rf_csv_errors(tmp_path: Path) -> None:
    with pytest.raises(DataQualityError, match="not found"):
        load_risk_free_csv(tmp_path / "nope.csv")
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(DataQualityError, match="expected a date column"):
        load_risk_free_csv(bad)
    late = tmp_path / "late.csv"
    late.write_text("DATE,DTB3\n2023-01-03,4.0\n")
    with pytest.raises(DataQualityError, match="after the bars"):
        align_risk_free(load_risk_free_csv(late), pd.DatetimeIndex(UNDER.index))
