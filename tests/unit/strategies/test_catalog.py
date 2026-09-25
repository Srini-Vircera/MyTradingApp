from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from adaptive_quant.config.loader import load_config
from adaptive_quant.config.schema import StrategiesConfig
from adaptive_quant.core.enums import StrategyLifecycle, TradingMode
from adaptive_quant.core.errors import ConfigurationError
from adaptive_quant.quant.strategies.catalog import ELIGIBLE_LIFECYCLES, StrategyCatalog
from adaptive_quant.quant.strategies.registry import registry
from tests.conftest import REPO_CONFIG

APPROVAL = {
    "approved_by": "Srini",
    "approved_on": "2026-01-15",
    "justification": "12 walk-forward folds positive; paper-traded 3 months; reviewed report",
}


def config(*entries: dict[str, Any]) -> StrategiesConfig:
    return StrategiesConfig.model_validate({"strategies": list(entries)})


def entry(sid: str, lifecycle: str = "research", **kw: Any) -> dict[str, Any]:
    impl = kw.pop("implementation", "ltt_sma_distance")
    family = registry()[impl].family.value
    return {"id": sid, "implementation": impl, "family": family, "lifecycle": lifecycle, **kw}


def test_shipped_catalogue_is_valid_and_complete() -> None:
    settings = load_config("development", config_dir=REPO_CONFIG).settings
    cat = StrategyCatalog.from_config(settings.strategies)
    assert len(cat) == len(registry())
    assert {e.version.implementation for e in cat.entries} == set(registry())
    assert all(e.version.lifecycle is StrategyLifecycle.RESEARCH for e in cat.entries)
    assert cat.symbols == (["QQQ"], ["NDX"])


def test_shipped_config_runs_nothing_outside_research() -> None:
    """Nothing is promoted: paper, shadow and live modes have no eligible strategies."""
    settings = load_config("paper", config_dir=REPO_CONFIG).settings
    cat = StrategyCatalog.from_config(settings.strategies)
    for mode in (TradingMode.SHADOW, TradingMode.PAPER, TradingMode.LIVE):
        assert cat.eligible(mode) == []


def test_live_mode_only_ever_uses_live_approved() -> None:
    assert ELIGIBLE_LIFECYCLES[TradingMode.LIVE] == {StrategyLifecycle.LIVE_APPROVED}
    cat = StrategyCatalog.from_config(
        config(
            entry("a", "research"),
            entry("b", "validated"),
            entry("c", "paper"),
            entry("d", "shadow"),
            entry("e", "live_approved", approval=APPROVAL),
            entry("f", "disabled"),
            entry("g", "paper", enabled=False),
        )
    )
    ids = {m: [s.strategy_id for s in cat.eligible(m)] for m in TradingMode}
    assert ids[TradingMode.LIVE] == ["e"]
    assert ids[TradingMode.PAPER] == ["c", "d", "e"]
    assert ids[TradingMode.SHADOW] == ["c", "d", "e"]
    assert ids[TradingMode.BACKTEST] == ["a", "b", "c", "d", "e"]  # never disabled ones


def test_live_approved_requires_written_human_approval() -> None:
    with pytest.raises(ValidationError, match="approval"):
        config(entry("e", "live_approved"))
    short = {**APPROVAL, "justification": "looks good"}
    with pytest.raises(ValidationError, match="justification"):
        config(entry("e", "live_approved", approval=short))
    ok = StrategyCatalog.from_config(config(entry("e", "live_approved", approval=APPROVAL)))
    approval = ok.get("e").version.approval
    assert approval is not None
    assert approval.approved_on == date(2026, 1, 15)


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        (entry("x", implementation="ltt_sma_distance", params={"windw": 200}), "unknown parameter"),
        (entry("x", params={"window": 5}), "minimum"),
        (entry("x", param_grid={"window": [200, 9999]}), "param_grid"),
        (entry("x", param_grid={"nope": [1]}), "param_grid: unknown parameter"),
        ({"id": "x", "implementation": "nope", "family": "momentum"}, "unknown implementation"),
        ({"id": "x", "implementation": "bo_donchian", "family": "momentum"}, "does not match"),
    ],
)
def test_invalid_entries(bad: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigurationError, match=match):
        StrategyCatalog.from_config(config(bad))


def test_all_errors_listed_together() -> None:
    with pytest.raises(ConfigurationError) as info:
        StrategyCatalog.from_config(
            config(entry("a", params={"window": 1}), entry("b", params={"scale": -1}))
        )
    assert "a:" in str(info.value) and "b:" in str(info.value)


def test_versions_record_governance_data() -> None:
    cat = StrategyCatalog.from_config(
        config(entry("a", notes="n", param_grid={"window": [200, 220]}))
    )
    v = cat.get("a").version
    assert v.version_id == cat.get("a").strategy.version_id
    assert v.params["window"] == 200
    assert v.param_grid == {"window": [200, 220]}
    assert v.warmup_bars == 200
    assert v.notes == "n"
    with pytest.raises(KeyError):
        cat.get("zzz")
