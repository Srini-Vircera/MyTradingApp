"""Scorecard ranking (never CAGR) and fail-closed validation gates."""

import dataclasses
import math

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.research.scorecard import (
    CRITERIA,
    Evidence,
    GateSettings,
    diversification,
    gates,
    score,
)

WEIGHTS = {"oos_sharpe": 3.0, "robustness": 3.0, "turnover": 1.0}


def ev(sid: str, **kw: object) -> Evidence:
    base: dict[str, object] = {
        "strategy_id": sid,
        "version_id": f"{sid}@1#x",
        "params_label": "p",
        "oos_sharpe": 1.0,
        "oos_sortino": 1.4,
        "oos_max_drawdown": -0.2,
        "oos_calmar": 0.6,
        "oos_sessions": 1000,
        "oos_synthetic_sessions": 0,
        "robustness_score": 0.8,
        "potentially_overfit": False,
        "regime_consistency": 0.8,
        "annual_turnover": 5.0,
        "execution_feasibility": 1.0,
        "dsr": 0.99,
        "pbo": 0.1,
        "sharpe_p_value": 0.001,
        "bh_adjusted_p": 0.01,
        "oos_benchmark_sharpe": 0.7,
    }
    return Evidence(**{**base, **kw})  # type: ignore[arg-type]


def test_cagr_is_not_a_criterion() -> None:
    assert not any("cagr" in k or "cagr" in a for k, (a, _) in CRITERIA.items())


def test_all_gates_pass_for_strong_real_evidence() -> None:
    g = gates(ev("a"), GateSettings())
    assert all(x.passed for x in g)
    assert {x.name for x in g} == {
        "deflated_sharpe", "pbo", "robustness", "oos_sharpe", "fdr", "oos_length",
        "vs_benchmark", "real_data",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("field", "value", "gate"),
    [
        ("dsr", 0.90, "deflated_sharpe"),
        ("dsr", math.nan, "deflated_sharpe"),
        ("pbo", 0.5, "pbo"),
        ("pbo", math.nan, "pbo"),
        ("robustness_score", 0.3, "robustness"),
        ("potentially_overfit", True, "robustness"),
        ("oos_sharpe", -0.1, "oos_sharpe"),
        ("oos_sharpe", math.nan, "oos_sharpe"),
        ("bh_adjusted_p", 0.2, "fdr"),
        ("oos_sessions", 100, "oos_length"),
        ("oos_synthetic_sessions", 1, "real_data"),
        ("oos_benchmark_sharpe", 1.2, "vs_benchmark"),
        ("oos_benchmark_sharpe", math.nan, "vs_benchmark"),
    ],
)
def test_each_gate_fails_closed(field: str, value: object, gate: str) -> None:
    res = {x.name: x.passed for x in gates(ev("a", **{field: value}), GateSettings())}
    assert res[gate] is False


def test_synthetic_gate_can_be_disabled_but_is_on_by_default() -> None:
    e = ev("a", oos_synthetic_sessions=10)
    assert "real_data" not in {x.name for x in gates(e, GateSettings(real_data_only=False))}
    assert GateSettings().real_data_only


def test_ranking_uses_percentile_ranks_and_puts_passing_first() -> None:
    a = ev("a", oos_sharpe=1.5, robustness_score=0.9, annual_turnover=2.0)
    b = ev("b", oos_sharpe=1.2, robustness_score=0.7, annual_turnover=10.0)
    c = ev("c", oos_sharpe=2.5, robustness_score=0.95, annual_turnover=1.0, dsr=0.5)  # fails DSR
    ranked = score([b, c, a], WEIGHTS, GateSettings())
    assert [x.evidence.strategy_id for x in ranked] == ["a", "b", "c"]
    top = ranked[0]
    assert top.components["oos_sharpe"] == pytest.approx(0.5)  # middle of three
    assert top.components["turnover"] == pytest.approx(0.5)  # lower turnover is better
    assert ranked[2].score == pytest.approx(1.0) and not ranked[2].passed
    single = score([a], WEIGHTS, GateSettings())[0]
    assert single.components["oos_sharpe"] == 0.5


def test_undefined_values_rank_last() -> None:
    a, b = ev("a", oos_sharpe=math.nan), ev("b", oos_sharpe=0.1)
    ranked = {x.evidence.strategy_id: x for x in score([a, b], WEIGHTS, GateSettings())}
    assert ranked["a"].components["oos_sharpe"] == 0.0
    assert ranked["b"].components["oos_sharpe"] == 0.5  # only defined value


def test_weights_are_validated() -> None:
    with pytest.raises(ValueError, match="unknown"):
        score([ev("a")], {"cagr": 1.0}, GateSettings())
    with pytest.raises(ValueError, match="positive"):
        score([ev("a")], {"oos_sharpe": 0.0}, GateSettings())


def test_diversification_penalises_correlated_candidates() -> None:
    idx = pd.date_range("2020-01-01", periods=300, freq="B", tz="UTC")
    rng = np.random.default_rng(0)
    x = pd.Series(rng.normal(0, 0.01, 300), index=idx)
    a = ev("a", oos_returns=x)
    b = ev("b", oos_returns=x * 2 + rng.normal(0, 0.001, 300))
    c = ev("c", oos_returns=pd.Series(rng.normal(0, 0.01, 300), index=idx))
    d = diversification([a, b, c])
    assert d["c"] > d["a"] and d["c"] > d["b"]
    assert math.isnan(diversification([a])["a"])
    assert dataclasses.is_dataclass(a)
