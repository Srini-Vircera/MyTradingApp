"""golden_death_cross in the control-plane API: options, per-run parameters, run modes,
Strategy Manager - and none of it can start automation or touch live trading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adaptive_quant.api.app import create_app
from adaptive_quant.control import state as ctl
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database
from tests.api.conftest import AUTH, services

pytestmark = pytest.mark.postgres
ACTOR = "Jane Operator"


@pytest.fixture
def api(tmp_path: Path, db: Database) -> TestClient:
    return TestClient(create_app(services(tmp_path, db)))


def post(api: TestClient, path: str, body: dict[str, Any]) -> Any:
    return api.post(f"/api/v1/{path}", headers=AUTH, json=body)


def backtest(api: TestClient, **params: Any) -> Any:
    return post(
        api,
        "jobs/backtest",
        {"actor": ACTOR, "params": {"strategies": ["golden_death_cross"], **params}},
    )


def test_backtest_options_list_the_strategy_with_its_parameters(api: TestClient) -> None:
    opts = api.get("/api/v1/backtests/options", headers=AUTH).json()
    gdc = next(s for s in opts["strategies"] if s["strategy_id"] == "golden_death_cross")
    assert gdc["title"] == "Golden Cross / Death Cross" and gdc["long_only_1x"] is True
    assert gdc["params"]["fast_period"] == 50 and gdc["params"]["slow_period"] == 200
    assert gdc["params"]["ma_type"] == "SMA" and gdc["params"]["bearish_action"] == "cash"
    schema = {p["name"]: p for p in gdc["param_schema"]}
    assert schema["ma_type"]["choices"] == ["SMA", "EMA"]
    assert schema["bearish_action"]["choices"] == ["cash", "qqq_reduced", "sqqq"]
    assert schema["fast_period"]["minimum"] == 2
    assert set(opts["run_modes"]) == {"ensemble", "independent"}
    assert "ONE backtest" in opts["run_modes"]["ensemble"]
    assert "PER strategy" in opts["run_modes"]["independent"]


def test_per_run_parameters_are_validated_before_queueing(api: TestClient) -> None:
    ok = backtest(
        api,
        strategy_params={
            "golden_death_cross": {"fast_period": 20, "slow_period": 100, "ma_type": "EMA"}
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["job"]["params"]["strategy_params"]["golden_death_cross"]["ma_type"] == "EMA"
    bad = backtest(
        api, strategy_params={"golden_death_cross": {"fast_period": 200, "slow_period": 50}}
    )
    assert bad.status_code == 400 and "slow_period" in bad.json()["detail"]
    assert (
        backtest(api, strategy_params={"golden_death_cross": {"ma_type": "WMA"}}).status_code == 400
    )
    # structural problems are 422 (strict model)
    assert (
        backtest(api, strategy_params={"st_ema_cross": {"fast": 5}}).status_code == 422
    )  # not selected
    assert backtest(api, strategy_params={"golden_death_cross": {"Bad-Name": 1}}).status_code == 422
    assert (
        backtest(api, strategy_params={"golden_death_cross": {"x": "../../etc"}}).status_code == 422
    )
    assert (
        backtest(api, strategy_params={"golden_death_cross": {"x": {"nested": 1}}}).status_code
        == 422
    )


def test_run_mode_is_explicit(api: TestClient) -> None:
    r = post(
        api,
        "jobs/backtest",
        {
            "actor": ACTOR,
            "params": {
                "strategies": ["golden_death_cross", "baseline_buy_hold"],
                "run_mode": "independent",
            },
        },
    )
    assert r.status_code == 200 and r.json()["job"]["params"]["run_mode"] == "independent"
    default = backtest(api)
    assert default.json()["job"]["params"]["run_mode"] == "ensemble"  # unchanged default
    assert backtest(api, run_mode="blend").status_code == 422


def test_strategy_manager_shows_it_in_research(api: TestClient) -> None:
    rows = api.get("/api/v1/strategies/manager", headers=AUTH).json()["strategies"]
    gdc = next(s for s in rows if s["strategy_id"] == "golden_death_cross")
    assert gdc["title"] == "Golden Cross / Death Cross" and "50-day SMA" in gdc["summary"]
    assert gdc["lifecycle"] == "research" and gdc["eligible_modes"] == ["backtest"]
    assert gdc["eligible_for_paper_or_shadow"] is False and gdc["params_editable"] is True
    assert gdc["params"]["fast_period"] == 50 and gdc["warmup_bars"] == 201
    assert {t["to"] for t in gdc["allowed_transitions"]} == {"validated", "disabled"}


def test_saving_parameters_or_promoting_never_starts_automation(
    api: TestClient, db: Database
) -> None:
    repo = ControlRepository(db)
    r = post(
        api,
        "strategies/golden_death_cross/params",
        {"actor": ACTOR, "reason": "try 20/100", "params": {"fast_period": 20, "slow_period": 100}},
    )
    assert r.status_code == 200, r.text
    bad = post(
        api,
        "strategies/golden_death_cross/params",
        {"actor": ACTOR, "reason": "invalid pair", "params": {"fast_period": 150}},
    )
    assert bad.status_code in (400, 409)  # 150 >= slow 100: fails closed
    why = "reviewed the walk-forward evidence and cost sensitivity in detail"
    for target in ("validated", "paper"):
        r = post(
            api,
            "strategies/golden_death_cross/lifecycle",
            {"actor": ACTOR, "reason": why, "target": target, "confirm": ctl.PROMOTION_CONFIRM},
        )
        assert r.status_code == 200, r.text
    for target in ("live_approved", "LIVE_APPROVED"):
        r = post(
            api,
            "strategies/golden_death_cross/lifecycle",
            {"actor": ACTOR, "reason": why, "target": target, "confirm": ctl.PROMOTION_CONFIRM},
        )
        assert r.status_code == 422
    backtest(api)
    assert not ctl.desired_running(repo.get_state(ctl.SCHEDULER_KEY))  # automation untouched
    v = api.get("/api/v1/trading/control", headers=AUTH).json()
    assert v["kill_switch"]["engaged"] is True and v["mode"] == "shadow"
    assert v["real_money_possible"] is False and v["live_trading"] == "LOCKED / NOT AVAILABLE"
