"""The backtester runs through the M7 decision chain: ensemble -> policy -> risk engine."""

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.backtest.engine import BacktestEngine, EngineSettings
from adaptive_quant.quant.risk.engine import RiskEngine
from adaptive_quant.quant.strategies.base import Strategy
from tests.data_helpers import calendar
from tests.unit.backtest.helpers import (
    SETTINGS,
    ScriptedExposure,
    SignOfLastReturn,
    backtest_config,
    frames_from_returns,
    random_frames,
    session_list,
)

S, E = date(2018, 1, 2), date(2020, 12, 31)
BT = date(2019, 3, 1)  # > 272 sessions of QQQ history: the risk engine can estimate


def engine(
    frames: dict[str, pd.DataFrame], strategies: list[Strategy], **cfg: Any
) -> BacktestEngine:
    return BacktestEngine(
        frames=frames,
        strategies=strategies,
        instruments=SETTINGS.universe.by_symbol,
        calendar=calendar(),
        settings=EngineSettings(
            backtest_config(**cfg),
            SETTINGS.trading.rebalance_threshold_weight,
            True,
            SETTINGS.risk,
            SETTINGS.strategies.ensemble,
        ),
    )


def test_every_decision_is_approved_by_the_risk_engine_within_limits() -> None:
    fr = random_frames(5, vol=0.015, start=S, end=E)
    eng = engine(fr, [ScriptedExposure(base=3.0)])
    assert eng.manager is not None and eng.risk_warmup_bars == 252 + 20 - 1 + 1
    res = eng.run(BT, E)
    assert all(d.risk is not None and d.refused is None for d in res.decisions)
    lim = SETTINGS.risk
    rk = RiskEngine(lim, SETTINGS.universe.by_symbol)
    for d in res.decisions:
        w = {s: float(v) for s, v in d.allocation.weights.items()}
        assert rk.net(w) <= lim.max_net_underlying_exposure + 1e-5
        assert w.get("TQQQ", 0) <= lim.position_cap("TQQQ") + 1e-5
        assert d.risk.vol_scale <= 1.0  # type: ignore[union-attr]
    assert any("volatility_target" in a for d in res.decisions for a in d.allocation.adjustments)


def test_insufficient_history_refuses_instead_of_trading() -> None:
    fr = random_frames(5, start=date(2020, 1, 2), end=date(2020, 12, 31))
    res = engine(fr, [ScriptedExposure(base=1.0)]).run(date(2020, 1, 10), date(2020, 3, 31))
    assert res.decisions and all(d.refused for d in res.decisions)
    assert res.fills == [] and any("risk engine refused" in w for w in res.warnings)
    assert all("underlying returns" in (d.refused or "") for d in res.decisions)


def test_crash_escalates_drawdown_bands_to_cash() -> None:
    n = len(session_list(S, E))
    r = np.full(n, 0.0004)
    crash = len(session_list(S, date(2019, 9, 30)))
    r[crash : crash + 25] = -0.03
    fr = frames_from_returns(r, S, E)
    res = engine(
        fr, [ScriptedExposure(base=2.0)], **{"costs": {"half_spread_bps": {"default": 0.0}}}
    ).run(BT, E)
    bands = [d.risk.band for d in res.decisions if d.risk]
    assert "caution" in bands and "defensive" in bands
    emergency = [d for d in res.decisions if d.risk and d.risk.band == "emergency"]
    if emergency:  # deepest band: flat and risk-increasing changes blocked
        assert all(sum(d.allocation.weights.values()) == 0 for d in emergency)
        assert all(d.risk.risk_increasing_blocked for d in emergency)  # type: ignore[union-attr]
    dd = 1 - res.daily["equity"] / res.daily["equity"].cummax()
    assert dd.max() < 0.45  # bands limited the loss of a 2x strategy in a -53% underlying crash


def test_future_poisoning_cannot_change_the_past_with_risk_engine() -> None:
    fr = random_frames(6, vol=0.012, start=S, end=E)
    base = engine(fr, [SignOfLastReturn(), ScriptedExposure(base=1.5)]).run(BT, E)
    cut = session_list(S, E).index(
        next(s for s in session_list(S, E) if s.date >= date(2020, 3, 2))
    )
    poisoned = {}
    rng = np.random.default_rng(9)
    for sym, df in fr.items():
        p = df.copy()
        k = rng.uniform(0.3, 3.0, len(p) - cut)
        for c in ("open", "high", "low", "close"):
            p[c] = np.concatenate([p[c].to_numpy()[:cut], p[c].to_numpy()[cut:] * k])
        p["high"] = p[["open", "high", "low", "close"]].max(axis=1)
        p["low"] = p[["open", "high", "low", "close"]].min(axis=1)
        poisoned[sym] = p
    other = engine(poisoned, [SignOfLastReturn(), ScriptedExposure(base=1.5)]).run(BT, E)
    t = fr["QQQ"].index[cut - 1]
    a, b = base.daily.loc[:t], other.daily.loc[:t]
    assert len(a) > 100
    assert (a["equity"].to_numpy() == b["equity"].to_numpy()).all()
    da = [d for d in base.decisions if d.decision_time <= t]
    db = [d for d in other.decisions if d.decision_time <= t]
    assert [d.allocation.weights for d in da] == [d.allocation.weights for d in db]


def test_two_strategies_are_combined_by_the_ensemble() -> None:
    fr = random_frames(7, start=S, end=E)
    res = engine(fr, [SignOfLastReturn(), ScriptedExposure(base=1.0)]).run(BT, date(2019, 6, 28))
    d = res.decisions[-1]
    assert d.ensemble is not None
    # both test strategies are BENCHMARK family: the 40% family cap leaves 60% unallocated
    cap = SETTINGS.strategies.ensemble.max_family_weight
    assert sum(d.ensemble.weights.weights.values()) == pytest.approx(cap)
    sig = {s.strategy_name: s.suggested_exposure for s in d.signals}
    expected = sum(d.ensemble.weights.weights[k] * v for k, v in sig.items())
    assert d.net_exposure == pytest.approx(expected)


def test_interim_mode_is_still_available_for_comparison() -> None:
    fr = random_frames(5, start=S, end=E)
    cfg = backtest_config()
    cfg = cfg.model_copy(
        update={"allocation": cfg.allocation.model_copy(update={"risk_engine": False})}
    )
    eng = BacktestEngine(
        frames=fr,
        strategies=[ScriptedExposure(base=3.0)],
        instruments=SETTINGS.universe.by_symbol,
        calendar=calendar(),
        settings=EngineSettings(cfg, 0.02, True, SETTINGS.risk),
    )
    assert eng.manager is None and eng.risk_warmup_bars == 0
    d = eng.run(BT, date(2019, 4, 30)).decisions[-1]
    assert d.risk is None and d.ensemble is None
