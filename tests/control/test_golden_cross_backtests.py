"""golden_death_cross through the ONE backtest engine, on the QQQ-only store, and the
explicit ensemble vs independent run modes. Generated data only."""

from __future__ import annotations

import dataclasses
import itertools
from pathlib import Path

import pytest

from adaptive_quant.config.loader import load_config
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.errors import ConfigurationError, MissingDataError
from adaptive_quant.services import backtests
from tests.control.conftest import QNOW

pytestmark = pytest.mark.integration
COMPARE = [
    "golden_death_cross",
    "baseline_buy_hold",
    "st_ema_cross",
    "it_ma_stack",
    "ltt_sma_distance",
]


def run(project: Path, **kw: object) -> backtests.BacktestOutcome:
    loaded = load_config("development", config_dir=project)
    req = backtests.BacktestRequest(strategies=["golden_death_cross"], source="file", **kw)  # type: ignore[arg-type]
    return backtests.run(loaded, FrozenClock(QNOW), req)


def test_classic_cross_runs_on_qqq_only_through_the_engine(qqq_project: Path) -> None:
    outcome = run(qqq_project)
    assert outcome.unpriced == ["TQQQ", "SQQQ"]  # the default QQQ/CASH version needs QQQ only
    s = backtests.summarize(outcome)
    x = s["crossover"]["golden_death_cross"]
    assert (x["fast_period"], x["slow_period"], x["ma_type"], x["classic"]) == (
        50,
        200,
        "SMA",
        True,
    )
    assert (
        x["first_signal"] and x["regime_periods"] and x["current_regime"] in ("bullish", "bearish")
    )
    assert s["first_decision"] == x["first_signal"] or s["first_decision"] >= x["first_signal"]
    # every recorded cross is a genuine transition and appears exactly once
    dates = x["golden_cross_dates"] + x["death_cross_dates"]
    assert len(dates) == len(set(dates)) == len(x["events"])
    kinds = [e["type"] for e in x["events"]]
    assert all(a != b for a, b in itertools.pairwise(kinds))  # alternate
    # costs and the risk engine stay active; results are hypothetical
    implicit = s["metadata"]["implicit_costs"]
    assert not implicit.startswith("0.00") and s["fills"] > 0
    assert "HYPOTHETICAL" in s["disclaimer"] and s["mode"] == "ensemble"
    daily = outcome.analysed.result.daily
    assert (daily["w_TQQQ"] == 0).all() and (daily["w_SQQQ"] == 0).all()
    assert (daily["w_QQQ"] >= 0).all()  # bearish regime -> cash, never short


def test_regime_change_trades_are_linked_to_their_cross(qqq_project: Path) -> None:
    s = backtests.summarize(run(qqq_project))
    events = s["crossover"]["golden_death_cross"]["events"]
    traded = [e for e in events if e["orders"]]
    assert traded, events
    for e in traded:
        sides = {o["side"] for o in e["orders"]}
        assert sides == ({"buy"} if e["type"] == "golden" else {"sell"})
        assert all(o["symbol"] == "QQQ" and o["date"] >= e["date"] for o in e["orders"])


def test_inverse_variant_fails_closed_without_sqqq_data(qqq_project: Path) -> None:
    with pytest.raises(MissingDataError, match=r"SQQQ|TQQQ"):
        run(
            qqq_project,
            strategy_params={
                "golden_death_cross": {"bearish_action": "sqqq", "max_short_exposure": 0.5}
            },
        )


def test_per_run_variant_applies_to_that_run_only(qqq_project: Path) -> None:
    variant = {"fast_period": 20, "slow_period": 100, "ma_type": "EMA"}
    outcome = run(qqq_project, strategy_params={"golden_death_cross": variant})
    s = backtests.summarize(outcome)
    x = s["crossover"]["golden_death_cross"]
    assert (x["fast_period"], x["slow_period"], x["ma_type"], x["classic"]) == (
        20,
        100,
        "EMA",
        False,
    )
    assert any("per-run strategy parameters" in n for n in s["notes"])
    base = backtests.summarize(run(qqq_project))
    assert s["strategy_versions"] != base["strategy_versions"]  # a distinct version id
    loaded = load_config("development", config_dir=qqq_project)
    configured = next(
        e for e in loaded.settings.strategies.strategies if e.id == "golden_death_cross"
    )
    assert configured.params["fast_period"] == 50  # the configured strategy is unchanged


@pytest.mark.parametrize(
    "params",
    [{"fast_period": 200, "slow_period": 50}, {"ma_type": "WMA"}, {"nope": 1}],
)
def test_invalid_per_run_parameters_fail_closed(
    qqq_project: Path, params: dict[str, object]
) -> None:
    with pytest.raises(ConfigurationError, match="golden_death_cross"):
        run(qqq_project, strategy_params={"golden_death_cross": params})


def test_several_strategies_default_to_one_combined_ensemble(qqq_project: Path) -> None:
    loaded = load_config("development", config_dir=qqq_project)
    req = backtests.BacktestRequest(
        strategies=["golden_death_cross", "baseline_buy_hold"], source="file"
    )
    s = backtests.summarize(backtests.run(loaded, FrozenClock(QNOW), req))
    assert s["mode"] == "ensemble" and s["strategies"] == [
        "golden_death_cross",
        "baseline_buy_hold",
    ]
    assert "comparison" not in s  # one portfolio, one set of metrics


def test_independent_comparison_uses_an_identical_period(qqq_project: Path) -> None:
    loaded = load_config("development", config_dir=qqq_project)
    req = backtests.BacktestRequest(
        strategies=COMPARE, source="file", run_mode="independent", initial_capital=100_000
    )
    outcomes = backtests.run_independent(loaded, FrozenClock(QNOW), req)
    assert [o.request.strategies for o in outcomes] == [[s] for s in COMPARE]
    assert len({(o.start, o.end) for o in outcomes}) == 1  # identical period for every run
    s = backtests.summarize_comparison(outcomes)
    assert s["mode"] == "independent" and s["strategies"] == COMPARE
    assert [r["strategy"] for r in s["comparison"]] == COMPARE
    firsts = {r["curve"][0]["date"] for r in s["runs"]}
    lasts = {r["curve"][-1]["date"] for r in s["runs"]}
    assert len(firsts) == len(lasts) == 1
    assert all(r["initial_capital"] == 100_000 for r in s["runs"])
    assert s["benchmarks"] and "HYPOTHETICAL" in s["disclaimer"]
    assert "golden_death_cross" in s["runs"][0]["crossover"]
    assert any("not an ensemble" in n for n in s["notes"])


def test_unknown_run_mode_is_refused(qqq_project: Path) -> None:
    with pytest.raises(ConfigurationError, match="run_mode"):
        run(qqq_project, run_mode="blend")


def test_a_start_before_the_warm_up_begins_at_the_first_valid_signal(qqq_project: Path) -> None:
    """Asking for the first day of data starts when every strategy can first signal."""
    from tests.control.conftest import QS

    loaded = load_config("development", config_dir=qqq_project)
    req = backtests.BacktestRequest(
        strategies=["golden_death_cross", "baseline_buy_hold"],
        source="file",
        start=QS,
        run_mode="independent",
    )
    outcomes = backtests.run_independent(loaded, FrozenClock(QNOW), req)
    auto = backtests.run_independent(
        loaded, FrozenClock(QNOW), dataclasses.replace(req, start=None)
    )
    assert outcomes[0].start == auto[0].start > QS  # the same warm-up-complete first session
    assert any("before the warm-up is complete" in n for n in outcomes[0].notes)
    s = backtests.summarize(outcomes[0])
    assert s["crossover"]["golden_death_cross"]["first_signal"] <= s["period"]["start"]
