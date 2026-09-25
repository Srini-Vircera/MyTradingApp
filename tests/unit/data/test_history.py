from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.config.loader import load_config
from adaptive_quant.config.schema import Settings
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.errors import DataQualityError, MissingDataError
from adaptive_quant.quant.data.bars import Adjustment, Frequency, SeriesKey
from adaptive_quant.quant.data.history import build_synthetic, extended_history, product_spec
from adaptive_quant.quant.data.store import ParquetBarStore
from adaptive_quant.quant.data.synthetic import (
    FinancingAssumptions,
    constant_risk_free,
    synthesize_leveraged_bars,
)
from adaptive_quant.quant.data.validation import BarValidator, IssueKind
from tests.conftest import REPO_CONFIG
from tests.data_helpers import calendar, daily_bars, market_dates

SETTINGS: Settings = load_config("development", config_dir=REPO_CONFIG).settings
V = BarValidator(calendar())
UNDER = daily_bars(date(2009, 1, 2), date(2011, 12, 30), vol=0.012)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> ParquetBarStore:
    s = ParquetBarStore(tmp_path / "data", clock)
    s.write(
        SeriesKey("file", "QQQ", Frequency.DAILY, Adjustment.SPLIT),
        UNDER,
        provider="file",
        validation=V.validate(UNDER, frequency=Frequency.DAILY, subject="u"),
    )
    return s


def store_real(store: ParquetBarStore, noise: float) -> pd.DataFrame:
    """A 'real' TQQQ = the model's own output from inception plus tracking noise."""
    spec = product_spec(SETTINGS, "TQQQ")
    syn = SETTINGS.data.synthetic
    rf = constant_risk_free(pd.DatetimeIndex(UNDER.index), syn.risk_free.constant_annual_rate)
    model, _ = synthesize_leveraged_bars(
        UNDER,
        spec,
        rf,
        FinancingAssumptions(syn.swap_spread_annual, syn.underlying_expense_addback),
    )
    real = model[market_dates(model) >= spec.inception].drop(columns=["is_synthetic"])
    rng = np.random.default_rng(3)
    factor = np.cumprod(1 + rng.normal(0, noise, len(real)))
    for col in ("open", "high", "low", "close"):
        real[col] = real[col] * factor * 0.5
    real["high"] = real[["open", "high", "low", "close"]].max(axis=1)
    real["low"] = real[["open", "high", "low", "close"]].min(axis=1)
    real["volume"] = 1e6
    store.write(
        SeriesKey("file", "TQQQ", Frequency.DAILY, Adjustment.ALL),
        real,
        provider="file",
        validation=V.validate(real, frequency=Frequency.DAILY, subject="r"),
    )
    return real


def test_build_without_real_series_records_assumptions(store: ParquetBarStore) -> None:
    build = build_synthetic(store, SETTINGS, "TQQQ", source="file")
    assert build.ok
    assert build.tracking is None
    assert build.snapshot.is_synthetic
    assert "constant" in build.assumptions["risk_free"]
    assert "not measured" in build.assumptions["tracking"]
    assert build.snapshot.notes["expense_ratio"] == "0.0084"


def test_tracking_within_bound_passes(store: ParquetBarStore) -> None:
    store_real(store, noise=0.0003)
    build = build_synthetic(store, SETTINGS, "TQQQ", source="file")
    assert build.tracking is not None
    assert build.tracking.passed
    assert build.ok


def test_tracking_beyond_bound_fails_closed(store: ParquetBarStore) -> None:
    store_real(store, noise=0.01)
    build = build_synthetic(store, SETTINGS, "TQQQ", source="file")
    assert not build.ok
    assert any(IssueKind.TRACKING_ERROR_EXCEEDED.value in i for i in build.snapshot.issues)
    # the failed synthetic series is never used to extend history
    ext = extended_history(store, "TQQQ", source="file")
    assert not ext["is_synthetic"].any()


def test_extended_history_splices(store: ParquetBarStore) -> None:
    real = store_real(store, noise=0.0003)
    build_synthetic(store, SETTINGS, "TQQQ", source="file")
    ext = extended_history(store, "TQQQ", source="file")
    assert ext["is_synthetic"].sum() == len(ext) - len(real)
    assert ext.index[0] < real.index[0]
    assert ext.loc[real.index[0], "close"] == pytest.approx(real["close"].iloc[0])


def test_requires_underlying_and_product_config(tmp_path: Path, clock: FrozenClock) -> None:
    empty = ParquetBarStore(tmp_path / "empty", clock)
    with pytest.raises(MissingDataError):
        build_synthetic(empty, SETTINGS, "TQQQ", source="file")
    with pytest.raises(DataQualityError, match="no synthetic product"):
        product_spec(SETTINGS, "QQQ")


def test_risk_free_csv_is_used(store: ParquetBarStore, tmp_path: Path) -> None:
    csv = tmp_path / "DTB3.csv"
    csv.write_text("DATE,DTB3\n2008-12-31,0.1\n2010-06-01,0.2\n")
    data = SETTINGS.data.model_copy(
        update={
            "synthetic": SETTINGS.data.synthetic.model_copy(
                update={
                    "risk_free": SETTINGS.data.synthetic.risk_free.model_copy(
                        update={"csv_path": csv}
                    )
                }
            )
        }
    )
    settings = SETTINGS.model_copy(update={"data": data})
    build = build_synthetic(store, settings, "SQQQ", source="file")
    assert build.assumptions["risk_free"].startswith("csv:")
