"""Fixtures for research tests: generated prices only (never real market data)."""

from __future__ import annotations

import dataclasses
from datetime import date

import pandas as pd

from adaptive_quant.config.schema import ResearchConfig
from adaptive_quant.quant.backtest.engine import EngineSettings
from adaptive_quant.quant.research.trials import TrialContext, data_fingerprint
from adaptive_quant.quant.strategies.catalog import StrategyCatalog, StrategyVersion
from tests.data_helpers import calendar
from tests.unit.backtest.helpers import SETTINGS, random_frames

Y = 1 / 252


def context(
    start: date = date(2017, 3, 1),
    end: date = date(2018, 12, 31),
    seed: int = 3,
    synthetic_before: date | None = None,
) -> TrialContext:
    frames = random_frames(seed, start=date(2016, 1, 4), end=end)
    syn = {k: pd.Series(False, index=v.index) for k, v in frames.items()}
    if synthetic_before is not None:
        cut = pd.Timestamp(synthetic_before, tz="America/New_York").tz_convert("UTC")
        for sym in ("TQQQ", "SQQQ"):
            syn[sym] = pd.Series(frames[sym].index < cut, index=frames[sym].index)
    settings = EngineSettings(
        SETTINGS.backtest,
        SETTINGS.trading.rebalance_threshold_weight,
        SETTINGS.trading.allow_fractional_shares,
        SETTINGS.risk,
    )
    return TrialContext(
        frames,
        syn,
        SETTINGS.universe.by_symbol,
        calendar(),
        settings,
        start,
        end,
        data_fingerprint(frames),
    )


def version(sid: str, grid: dict[str, list[int | float | str | bool]]) -> StrategyVersion:
    v = StrategyCatalog.from_config(SETTINGS.strategies).get(sid).version
    return dataclasses.replace(v, param_grid=grid)


def small_config(**over: object) -> ResearchConfig:
    r = SETTINGS.research
    cfg = r.model_copy(
        update={
            "walk_forward": r.walk_forward.model_copy(
                update={
                    "train_years": 120 * Y,
                    "validate_years": 60 * Y,
                    "test_years": 60 * Y,
                    "step_years": 60 * Y,
                }
            ),
            "monte_carlo": r.monte_carlo.model_copy(
                update={
                    "simulations": 50,
                    "cost_simulations": 2,
                    "delays": [0, 1],
                    "min_years": 0.5,
                    "top_n": 1,
                }
            ),
            "bootstrap": r.bootstrap.model_copy(update={"samples": 200}),
            "gates": r.gates.model_copy(update={"min_oos_sessions": 100}),
            "pbo_partitions": 6,
        }
    )
    return cfg.model_copy(update=over)
