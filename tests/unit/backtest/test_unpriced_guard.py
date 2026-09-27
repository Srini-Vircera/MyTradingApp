"""TQQQ/SQQQ may be missing only while no allocation ever holds them."""

from __future__ import annotations

import dataclasses

import pytest

from adaptive_quant.core.errors import DataQualityError
from adaptive_quant.quant.backtest.engine import BacktestEngine, EngineSettings
from tests.data_helpers import calendar
from tests.unit.backtest.helpers import (
    BT_START,
    END,
    SETTINGS,
    ScriptedExposure,
    backtest_config,
    random_frames,
)

UNPRICED = frozenset({"TQQQ", "SQQQ"})


def engine(exposure: float, unpriced: frozenset[str]) -> BacktestEngine:
    frames = {"QQQ": random_frames()["QQQ"]}
    config = backtest_config()
    config = config.model_copy(
        update={"allocation": config.allocation.model_copy(update={"apply_risk_limits": False})}
    )
    return BacktestEngine(
        frames=frames,
        strategies=[ScriptedExposure(base=exposure)],
        instruments=SETTINGS.universe.by_symbol,
        calendar=calendar(),
        settings=dataclasses.replace(
            EngineSettings(config, 0.0, True, None), unpriced_instruments=unpriced
        ),
    )


def test_missing_leveraged_data_is_still_an_error_by_default() -> None:
    with pytest.raises(DataQualityError, match="no price data for tradeable"):
        engine(1.0, frozenset()).run(BT_START, END)


def test_one_x_long_runs_without_leveraged_prices() -> None:
    daily = engine(1.0, UNPRICED).run(BT_START, END).daily
    assert (daily["w_TQQQ"] == 0).all() and (daily["w_SQQQ"] == 0).all()
    assert daily["w_QQQ"].iloc[-1] > 0.9


@pytest.mark.parametrize(("exposure", "sym"), [(2.0, "TQQQ"), (-1.0, "SQQQ")])
def test_allocation_to_an_unpriced_instrument_aborts(exposure: float, sym: str) -> None:
    with pytest.raises(DataQualityError, match=f"holds {sym}"):
        engine(exposure, UNPRICED).run(BT_START, END)
