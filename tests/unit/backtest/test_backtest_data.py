from datetime import date
from pathlib import Path

import pytest

from adaptive_quant.config.loader import load_config
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.errors import MissingDataError
from adaptive_quant.quant.backtest.data import load_backtest_data
from adaptive_quant.quant.data.bars import Adjustment, Frequency, SeriesKey
from adaptive_quant.quant.data.history import build_synthetic
from adaptive_quant.quant.data.store import ParquetBarStore
from adaptive_quant.quant.data.validation import BarValidator
from tests.conftest import REPO_CONFIG
from tests.data_helpers import calendar, daily_bars

SETTINGS = load_config("development", config_dir=REPO_CONFIG).settings


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> ParquetBarStore:
    s = ParquetBarStore(tmp_path, clock)
    v = BarValidator(calendar())
    qqq = daily_bars(date(2009, 1, 2), date(2011, 6, 30), seed=1)
    tqqq = daily_bars(date(2010, 2, 11), date(2011, 6, 30), seed=2)
    for sym, df, adj in (
        ("QQQ", qqq, Adjustment.ALL),
        ("QQQ", qqq, Adjustment.SPLIT),
        ("TQQQ", tqqq, Adjustment.ALL),
    ):
        s.write(
            SeriesKey("file", sym, Frequency.DAILY, adj),
            df,
            provider="file",
            validation=v.validate(df, frequency=Frequency.DAILY, subject=sym),
        )
    return s


def test_real_only_loading_records_provenance(store: ParquetBarStore) -> None:
    data = load_backtest_data(store, "file", ["QQQ", "TQQQ"], ["SPY"])
    assert set(data.frames) == {"QQQ", "TQQQ"}
    assert data.missing_optional == ["SPY"]
    assert not data.synthetic["TQQQ"].any()
    assert data.provenance["QQQ"].startswith("file:QQQ:1d:all #")


def test_required_missing_raises(store: ParquetBarStore) -> None:
    with pytest.raises(MissingDataError):
        load_backtest_data(store, "file", ["QQQ", "SQQQ"])


def test_failed_synthetic_model_is_never_used(store: ParquetBarStore) -> None:
    """The fixture's 'real' TQQQ is unrelated noise: tracking fails, extension is refused."""
    build = build_synthetic(store, SETTINGS, "TQQQ", source="file")
    assert not build.ok
    data = load_backtest_data(
        store, "file", ["QQQ", "TQQQ"], use_synthetic=True, synthetic_symbols=["TQQQ"]
    )
    assert not data.synthetic["TQQQ"].any()
    assert "SYNTHETIC" not in data.provenance["TQQQ"]


def test_synthetic_extension_is_flagged_per_row(store: ParquetBarStore) -> None:
    # replace 'real' TQQQ with a model-consistent series so the tracking check passes
    import pandas as pd

    from adaptive_quant.quant.data.history import product_spec
    from adaptive_quant.quant.data.synthetic import (
        FinancingAssumptions,
        constant_risk_free,
        synthesize_leveraged_bars,
    )

    under = store.read(SeriesKey("file", "QQQ", Frequency.DAILY, Adjustment.SPLIT))
    syn = SETTINGS.data.synthetic
    model, _ = synthesize_leveraged_bars(
        under,
        product_spec(SETTINGS, "TQQQ"),
        constant_risk_free(pd.DatetimeIndex(under.index), 0.02),
        FinancingAssumptions(syn.swap_spread_annual, syn.underlying_expense_addback),
    )
    real = model[model.index >= pd.Timestamp("2010-02-11 21:00", tz="UTC")].drop(
        columns=["is_synthetic"]
    )
    real["volume"] = 1e6
    store.write(
        SeriesKey("file", "TQQQ", Frequency.DAILY, Adjustment.ALL),
        real,
        provider="file",
        validation=BarValidator(calendar()).validate(real, frequency=Frequency.DAILY, subject="t"),
    )
    assert build_synthetic(store, SETTINGS, "TQQQ", source="file").ok
    data = load_backtest_data(
        store, "file", ["QQQ", "TQQQ"], use_synthetic=True, synthetic_symbols=["TQQQ"]
    )
    flags = data.synthetic["TQQQ"]
    tq = data.frames["TQQQ"]
    assert "is_synthetic" not in tq.columns
    assert tq.index[0] < flags[~flags].index[0]  # extended before the real start
    assert flags.iloc[0] and not flags.iloc[-1]
    assert "SYNTHETIC rows" in data.provenance["TQQQ"]
    real_only = load_backtest_data(store, "file", ["QQQ", "TQQQ"])
    assert len(real_only.frames["TQQQ"]) < len(tq)
