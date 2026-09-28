"""golden_death_cross in the existing research pipeline (walk-forward, DSR, PBO, bootstrap,
FDR, robustness, gates). Research never promotes beyond what governance allows."""

from __future__ import annotations

from pathlib import Path

import pytest

from adaptive_quant.config.loader import load_config
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.errors import StrategyError
from adaptive_quant.quant.research.robustness import ParamGrid
from adaptive_quant.quant.strategies.catalog import StrategyCatalog
from adaptive_quant.quant.strategies.catalogue.crossover import GoldenDeathCross
from adaptive_quant.services import research
from tests.conftest import REPO_CONFIG
from tests.control.conftest import QNOW

pytestmark = pytest.mark.integration


def test_configured_research_grid_enforces_fast_below_slow() -> None:
    loaded = load_config("development", config_dir=REPO_CONFIG)
    entry = StrategyCatalog.from_config(loaded.settings.strategies).get("golden_death_cross")
    grid = ParamGrid.build(entry.version.params, entry.version.param_grid, 64)
    assert grid.dims == ("fast_period", "ma_type", "slow_period")
    assert grid.shape == (4, 2, 4)  # 32 counted trials
    valid, invalid = [], []
    for c in grid.coords():
        p = grid.params(c)
        try:
            GoldenDeathCross("golden_death_cross", p)
            valid.append(p)
        except StrategyError:
            invalid.append(p)
    assert all(int(p["fast_period"]) >= int(p["slow_period"]) for p in invalid)
    assert len(invalid) == 2 and len(valid) == 30  # 100/100 SMA and EMA
    assert {p["ma_type"] for p in valid} == {"SMA", "EMA"}
    assert entry.version.lifecycle.value == "research"  # never pre-promoted


def test_research_run_evaluates_the_grid_with_the_existing_methodology(full_project: Path) -> None:
    loaded = load_config("development", config_dir=full_project)
    outcome = research.run(
        loaded,
        FrozenClock(QNOW),
        research.ResearchRequest(strategies=["golden_death_cross"], source="file", simulations=20),
    )
    s = research.summarize(outcome)
    # 32 grid points; the two fast >= slow points (100/100 SMA and EMA) are invalid trials
    assert "HYPOTHETICAL" in s["disclaimer"] and s["trials_this_run"] == 30
    ranked = s["ranked"]
    assert ranked and ranked[0]["strategy_id"] == "golden_death_cross"
    assert len(ranked[0]["gates"]) >= 5  # the existing promotion gates, unchanged
    for key in ("dsr", "pbo", "p_value", "bh_adjusted_p", "robustness"):
        assert key in ranked[0]
    # research may at most record research -> validated in the ledger; never paper or beyond
    assert all(t["to"] in ("validated", "research") for t in s["governance_transitions"])
    assert s["promotion"].startswith("No strategy is promoted")
