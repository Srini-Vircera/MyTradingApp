"""Ensemble weighting, clustering, family caps, shadow returns and the allocation policy."""

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.config.schema import EnsembleConfig
from adaptive_quant.core.enums import SignalDirection, StrategyFamily
from adaptive_quant.core.models import StrategySignal, direction_for_score
from adaptive_quant.quant.ensemble.engine import EnsembleEngine, Member
from adaptive_quant.quant.ensemble.shadow import ShadowBook
from adaptive_quant.quant.portfolio.policy import AllocationPolicy, exposure_to_weights

F = StrategyFamily
NOW = datetime(2024, 1, 2, 20, tzinfo=UTC)


def cfg(**kw: Any) -> EnsembleConfig:
    base: dict[str, Any] = {"max_family_weight": 1.0, "min_history": 20, "lookback_sessions": 252}
    return EnsembleConfig.model_validate({**base, **kw})


def members(*fams: StrategyFamily) -> list[Member]:
    return [Member(f"s{i}", f) for i, f in enumerate(fams)]


def sig(name: str, exposure: float, score: float = 0.5, conf: float = 0.8) -> StrategySignal:
    return StrategySignal(
        strategy_name=name,
        strategy_version="v",
        timestamp=NOW,
        data_timestamp=NOW,
        direction=direction_for_score(score),
        raw_score=score,
        normalized_score=score,
        confidence=conf,
        suggested_exposure=exposure,
        reason="test",
    )


def test_equal_and_fixed_weights_and_combination() -> None:
    e = EnsembleEngine(members(F.MOMENTUM, F.BREAKOUT), cfg())
    w = e.weights(np.zeros((0, 2)))
    assert w.weights == {"s0": 0.5, "s1": 0.5}
    s = e.combine([sig("s0", 2.0, 0.8), sig("s1", -1.0, -0.4, 0.4)], w)
    assert s.exposure == pytest.approx(0.5) and s.score == pytest.approx(0.2)
    assert s.confidence == pytest.approx(0.6)
    assert s.contributions == {"s0": 1.0, "s1": -0.5}
    fixed = EnsembleEngine(
        members(F.MOMENTUM, F.BREAKOUT), cfg(method="fixed", fixed_weights={"s0": 3.0})
    )
    assert fixed.weights(np.zeros((0, 2))).weights == {"s0": 1.0, "s1": 0.0}
    with pytest.raises(ValueError, match="no signal"):
        e.combine([sig("s0", 1.0)], w)
    assert SignalDirection.BEARISH is direction_for_score(-0.4)


def test_risk_adjusted_is_inverse_volatility_with_fallback() -> None:
    rng = np.random.default_rng(0)
    h = np.column_stack([rng.normal(0, 0.01, 100), rng.normal(0, 0.02, 100), np.zeros(100)])
    e = EnsembleEngine(
        members(F.MOMENTUM, F.BREAKOUT, F.VOLATILITY_REGIME),
        cfg(method="risk_adjusted", vol_window=100, max_pairwise_correlation=1.0),
    )
    w = e.weights(h).weights
    inv = 1 / h[:, :2].std(axis=0, ddof=1)
    floor = 1 / np.median(h[:, :2].std(axis=0, ddof=1))
    raw = np.array([*inv, floor])
    assert list(w.values()) == pytest.approx((raw / raw.sum()).tolist())
    short = e.weights(h[:5])
    assert list(short.weights.values()) == pytest.approx([1 / 3] * 3)
    assert "equal weight" in short.adjustments[0]


def test_walk_forward_weights_shrink_and_refit_only_on_schedule() -> None:
    rng = np.random.default_rng(1)
    good = rng.normal(0.002, 0.01, 200)
    bad = rng.normal(-0.002, 0.01, 200)
    h = np.column_stack([good, bad])
    e = EnsembleEngine(
        members(F.MOMENTUM, F.BREAKOUT),
        cfg(method="walk_forward", shrinkage=0.5, refit_sessions=10, max_pairwise_correlation=1.0),
    )
    w = e.weights(h[:100]).weights
    assert w == pytest.approx({"s0": 0.75, "s1": 0.25})  # fit (1, 0) shrunk halfway to equal
    # 5 more rows: no refit even though the new data would change the fit
    flipped = np.vstack([h[:100], np.column_stack([bad[:5] * 50, good[:5] * 50])])
    assert e.weights(flipped).weights == pytest.approx(w)


def test_correlated_strategies_share_one_cluster_weight() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(0, 0.01, 200)
    h = np.column_stack([x, x * 1.01 + rng.normal(0, 1e-4, 200), rng.normal(0, 0.01, 200)])
    e = EnsembleEngine(
        members(F.MOMENTUM, F.BREAKOUT, F.VOLATILITY_REGIME), cfg(max_pairwise_correlation=0.85)
    )
    w = e.weights(h)
    assert ("s0", "s1") in w.clusters
    assert w.weights["s0"] + w.weights["s1"] == pytest.approx(0.5)  # a cluster counts once
    assert w.weights["s2"] == pytest.approx(0.5)


def test_family_caps_redistribute_or_leave_cash() -> None:
    e = EnsembleEngine(
        members(F.MOMENTUM, F.MOMENTUM, F.MOMENTUM, F.BREAKOUT), cfg(max_family_weight=0.5)
    )
    w = e.weights(np.zeros((0, 4)))
    assert sum(w.weights[i] for i in ("s0", "s1", "s2")) == pytest.approx(0.5)
    assert w.weights["s3"] == pytest.approx(0.5)
    solo = EnsembleEngine(members(F.MOMENTUM, F.MOMENTUM), cfg(max_family_weight=0.4))
    ws = solo.weights(np.zeros((0, 2)))
    assert sum(ws.weights.values()) == pytest.approx(0.4) and ws.unallocated == pytest.approx(0.6)
    s = solo.combine([sig("s0", 2.0), sig("s1", 2.0)], ws)
    assert s.exposure == pytest.approx(0.8)  # the unallocated 60% stays in cash


def test_shadow_returns_are_point_in_time() -> None:
    idx = pd.date_range("2024-01-01 21:00", periods=6, freq="D", tz="UTC")
    closes = pd.Series([100, 110, 99, 99, 120, 60], index=idx, dtype=float)
    book = ShadowBook(["a", "b"])
    for k in range(4):
        book.record(idx[k].to_pydatetime(), {"a": 1.0, "b": -0.5 * k})
    book.record(idx[2].to_pydatetime(), {"a": 9.0, "b": 9.0})  # repeated data: ignored
    r = book.returns(closes, idx[3].to_pydatetime())
    assert r == pytest.approx(np.array([[0.1, 0.0], [-0.1, 0.05], [0.0, 0.0]]))
    poisoned = closes.copy()
    poisoned = pd.Series([100, 110, 99, 99, 1e9, 1e-9], index=idx, dtype=float)
    assert book.returns(poisoned, idx[3].to_pydatetime()) == pytest.approx(r)
    assert book.returns(closes, (idx[0] + timedelta(hours=1)).to_pydatetime()).shape == (0, 2)


def test_policy_mapping_and_dead_band() -> None:
    assert exposure_to_weights(2.0) == pytest.approx({"QQQ": 0.5, "TQQQ": 0.5, "SQQQ": 0.0})
    assert exposure_to_weights(-0.3)["SQQQ"] == pytest.approx(0.1)
    p = AllocationPolicy(min_abs_exposure=0.1).propose(0.05, NOW)
    assert sum(p.weights.values()) == 0.0 and p.requested_exposure == 0.05
    with pytest.raises(ValueError, match="long_mode"):
        AllocationPolicy("yolo")
    with pytest.raises(ValueError, match="finite"):
        exposure_to_weights(float("nan"))


def test_config_validation() -> None:
    with pytest.raises(ValueError, match="min_history"):
        EnsembleConfig(min_history=300, lookback_sessions=100)
    with pytest.raises(ValueError, match="duplicate"):
        EnsembleEngine([Member("a", F.MOMENTUM), Member("a", F.BREAKOUT)], cfg())
