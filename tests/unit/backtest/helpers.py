"""Fixtures for backtest tests: generated prices (never real data) and scripted strategies."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from adaptive_quant.config.loader import load_config
from adaptive_quant.config.schema import BacktestConfig, Settings
from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.core.enums import StrategyFamily
from adaptive_quant.quant.backtest.engine import BacktestEngine, BacktestResult, EngineSettings
from adaptive_quant.quant.data.bars import make_bars
from adaptive_quant.quant.indicators.specs import IndicatorSpec
from adaptive_quant.quant.strategies.base import Evaluation, Strategy, StrategyContext
from adaptive_quant.quant.strategies.params import ParamSpec
from tests.conftest import REPO_CONFIG
from tests.data_helpers import calendar

SETTINGS: Settings = load_config("development", config_dir=REPO_CONFIG).settings
START, END = date(2020, 1, 2), date(2020, 12, 31)  # price data range
BT_START = date(2020, 1, 10)  # backtests start later so every decision has prior bars


def frames_from_returns(
    r: np.ndarray, start: date = START, end: date = END, seed: int = 0, volume: float = 1e7
) -> dict[str, pd.DataFrame]:
    """QQQ with close-to-close returns ``r``; TQQQ/SQQQ exactly +/-3x daily. Opens differ
    from the previous close by a deterministic gap so open vs close fills are distinguishable."""
    sessions = calendar().sessions_in_range(start, end)
    if len(r) != len(sessions):
        raise ValueError(f"need {len(sessions)} returns, got {len(r)}")
    rng = np.random.default_rng(seed)
    out = {}
    for sym, lev, base in (("QQQ", 1.0, 100.0), ("TQQQ", 3.0, 50.0), ("SQQQ", -3.0, 50.0)):
        close = base * np.cumprod(1 + lev * r)
        prev = np.concatenate([[base], close[:-1]])
        gap = 1 + lev * rng.normal(0, 0.002, len(r))
        opens = prev * gap
        out[sym] = make_bars(
            [s.close for s in sessions],
            open=opens.tolist(),
            high=(np.maximum(opens, close) * 1.001).tolist(),
            low=(np.minimum(opens, close) * 0.999).tolist(),
            close=close.tolist(),
            volume=[volume] * len(r),
        )
    return out


def random_frames(
    seed: int = 1, vol: float = 0.01, start: date = START, end: date = END
) -> dict[str, pd.DataFrame]:
    n = len(calendar().sessions_in_range(start, end))
    return frames_from_returns(np.random.default_rng(seed).normal(0.0003, vol, n), start, end, seed)


class ScriptedExposure(Strategy):
    """Exposure chosen by the date of the *last visible* bar (default ``base``).

    Lets tests pin down exactly which bar a decision saw and when it filled.
    """

    implementation = "test_scripted"
    family = StrategyFamily.BENCHMARK
    description = "scripted exposure for tests"
    param_specs = (
        ParamSpec("max_long_exposure", float, 3.0, 0.0, 3.0),
        ParamSpec("max_short_exposure", float, 3.0, 0.0, 3.0),
    )

    def __init__(self, schedule: Mapping[date, float] | None = None, base: float = 0.0) -> None:
        self.schedule = dict(schedule or {})
        self.base = base
        super().__init__("scripted", {})

    def indicators(self) -> list[IndicatorSpec]:
        return []

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        seen = ctx.bars.index[-1].tz_convert(MARKET_TZ).date()
        e = self.schedule.get(seen, self.base)
        return Evaluation(max(-1.0, min(1.0, e / 3.0)), f"scripted {e} after {seen}", exposure=e)


class SignOfLastReturn(Strategy):
    """Long 1x after an up day, flat after a down day. On i.i.d. returns this has no edge -
    unless the engine leaks the bar it is about to trade."""

    implementation = "test_sign_oracle"
    family = StrategyFamily.BENCHMARK
    description = "1-day sign momentum"

    def indicators(self) -> list[IndicatorSpec]:
        return [IndicatorSpec("rate_of_change", {"window": 1}, name="r1")]

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        up = ctx.values["r1"] > 0
        return Evaluation(1.0 if up else 0.0, "last return up" if up else "last return down")


def backtest_config(**overrides: Any) -> BacktestConfig:
    return BacktestConfig.model_validate({**SETTINGS.backtest.model_dump(), **overrides})


def zero_costs(**extra: Any) -> dict[str, Any]:
    return {
        "costs": {
            "half_spread_bps": {"default": 0.0},
            "slippage_bps": 0.0,
            "impact_coefficient_bps": 0.0,
            "commission_per_share": 0.0,
            "commission_per_order": 0.0,
            "commission_minimum": 0.0,
            **extra,
        }
    }


def run(
    frames: dict[str, pd.DataFrame],
    strategy: Strategy,
    *,
    start: date = BT_START,
    end: date = END,
    threshold: float = 0.0,
    fractional: bool = True,
    limits: bool = False,
    **cfg: Any,
) -> BacktestResult:
    config = backtest_config(**cfg)
    if not limits:
        config = config.model_copy(
            update={"allocation": config.allocation.model_copy(update={"apply_risk_limits": False})}
        )
    engine = BacktestEngine(
        frames=frames,
        strategies=[strategy],
        instruments=SETTINGS.universe.by_symbol,
        calendar=calendar(),
        settings=EngineSettings(config, threshold, fractional, SETTINGS.risk if limits else None),
    )
    return engine.run(start, end)


def session_list(start: date = START, end: date = END):  # type: ignore[no-untyped-def]
    return calendar().sessions_in_range(start, end)
