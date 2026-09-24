"""Scenario data for strategy tests (deterministic, generated - never real data)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from adaptive_quant.quant.data.bars import make_bars
from adaptive_quant.quant.data.view import MarketDataView
from adaptive_quant.quant.strategies.base import Strategy
from adaptive_quant.quant.strategies.registry import registry
from tests.data_helpers import calendar

START = date(2018, 1, 2)


def bars_from_returns(
    returns: list[float] | np.ndarray, start: date = START, seed: int = 1, price: float = 100.0
) -> pd.DataFrame:
    """Canonical daily bars on real NYSE sessions following ``returns`` exactly (close-to-close)."""
    r = np.asarray(returns, dtype=float)
    sessions = calendar().sessions_in_range(start, start + timedelta(days=int(len(r) * 1.6) + 30))[
        : len(r)
    ]
    if len(sessions) < len(r):
        raise ValueError("not enough sessions")
    rng = np.random.default_rng(seed)
    close = price * np.cumprod(1.0 + r)
    prev = np.concatenate([[price], close[:-1]])
    opens = prev * (1.0 + rng.normal(0, 0.002, len(r)))
    wig = np.abs(rng.normal(0, 0.004, len(r)))
    return make_bars(
        [s.close for s in sessions],
        open=opens.tolist(),
        high=(np.maximum(opens, close) * (1 + wig)).tolist(),
        low=(np.minimum(opens, close) * (1 - wig)).tolist(),
        close=close.tolist(),
        volume=(1e6 * (1 + rng.uniform(0, 0.3, len(r)))).tolist(),
    )


def random_walk(
    n: int = 800, drift: float = 0.0004, vol: float = 0.012, seed: int = 7
) -> pd.DataFrame:
    return bars_from_returns(np.random.default_rng(seed).normal(drift, vol, n), seed=seed)


def view_at(frames: dict[str, pd.DataFrame], i: int, symbol: str = "QQQ") -> MarketDataView:
    """View as of the close of row ``i`` of ``symbol``."""
    as_of: datetime = frames[symbol].index[i].to_pydatetime()
    return MarketDataView(frames, as_of)


def default_strategies() -> list[Strategy]:
    return [cls() for cls in registry().values()]


def ids(strategy: Strategy) -> str:
    return strategy.strategy_id
