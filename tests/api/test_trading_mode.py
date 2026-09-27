"""Runtime trading mode (SHADOW/PAPER) and automation, driven from the dashboard API.

Mode, automation, kill switch and the deployment master gate are separate controls:
selecting PAPER never starts automation, releases the kill switch or promotes a
strategy; starting automation needs every gate; PAPER -> SHADOW fails closed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adaptive_quant.api.app import create_app
from adaptive_quant.control import state as ctl
from adaptive_quant.control.readiness import PREFLIGHT_CHECKS, SWITCH_CHECKS
from adaptive_quant.core.errors import SafetyViolation
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database
from adaptive_quant.services.runtime import effective_config
from adaptive_quant.trading.safety.kill_switch import RELEASE_CONFIRMATION
from tests.api.conftest import AUTH, NOW, TOKEN, services

pytestmark = pytest.mark.postgres
ACTOR = "Jane Operator"
WHY = "reviewed the walk-forward evidence and cost sensitivity in detail"
TO_PAPER = {
    "actor": ACTOR,
    "reason": "move to paper",
    "target": "paper",
    "confirm": ctl.PAPER_MODE_CONFIRM,
}
TO_SHADOW = {
    "actor": ACTOR,
    "reason": "back to shadow",
    "target": "shadow",
    "confirm": ctl.SHADOW_MODE_CONFIRM,
}
START_PAPER = {"actor": ACTOR, "reason": "start the paper run", "confirm": ctl.START_PAPER_CONFIRM}


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def api(tmp: Path, db: Database) -> TestClient:
    return TestClient(create_app(services(tmp, db)))


@pytest.fixture
def repo(db: Database) -> ControlRepository:
    return ControlRepository(db)


def post(api: TestClient, path: str, body: dict[str, Any]) -> Any:
    return api.post(f"/api/v1/{path}", headers=AUTH, json=body)


def view(api: TestClient) -> dict[str, Any]:
    r = api.get("/api/v1/trading/control", headers=AUTH)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def verified(
    repo: ControlRepository,
    *,
    age: timedelta = timedelta(0),
    paper: bool = True,
    failing: tuple[str, ...] = (),
    host: str = "paper-api.alpaca.markets",
) -> None:
    checks = [
        {"name": n, "label": n, "passed": n not in failing, "detail": "x"}
        for n in dict.fromkeys((*SWITCH_CHECKS, *PREFLIGHT_CHECKS))
    ]
    repo.set_state(
        ctl.BROKER_KEY,
        {
            "ok": paper and not failing,
            "is_paper": paper,
            "paper_endpoint": True,
            "endpoint_host": host,
            "verified_at": (NOW - age).isoformat(),
            "checks": checks,
        },
        "worker:w1",
        NOW,
    )


def worker(repo: ControlRepository, *, gate: bool = True, state: str = "stopped") -> None:
    repo.beat(
        "w1",
        NOW,
        NOW,
        {
            "master_gate": gate,
            "scheduler": {"state": state},
            "credentials_configured": {"ALPACA_API_KEY_ID": True, "ALPACA_API_SECRET_KEY": True},
        },
    )


def data(repo: ControlRepository, *, fresh: bool = True, valid: bool = True) -> None:
    rows = [
        {
            "source": "alpaca",
            "symbol": s,
            "frequency": "1d",
            "adjustment": "raw",
            "validation_passed": valid,
            "fresh": fresh,
            "is_synthetic": False,
            "freshness": "ok" if fresh else "newest bar 5 sessions old",
        }
        for s in ("QQQ", "TQQQ", "SQQQ")
    ]
    repo.replace_inventory(rows, NOW)


def promote(api: TestClient, sid: str = "baseline_buy_hold") -> None:
    for target in ("validated", "paper"):
        r = post(
            api,
            f"strategies/{sid}/lifecycle",
            {"actor": ACTOR, "reason": WHY, "target": target, "confirm": ctl.PROMOTION_CONFIRM},
        )
        assert r.status_code == 200, r.text


def release(api: TestClient) -> None:
    r = post(
        api,
        "kill-switch/release",
        {"actor": ACTOR, "reason": "reviewed readiness", "confirm": RELEASE_CONFIRMATION},
    )
    assert r.status_code == 200, r.text


def to_paper(api: TestClient, repo: ControlRepository) -> None:
    verified(repo)
    worker(repo)
    r = post(api, "trading/mode", TO_PAPER)
    assert r.status_code == 200, r.text


# ================================================================ view
def test_control_view_separates_mode_automation_kill_switch_and_broker(
    api: TestClient, repo: ControlRepository
) -> None:
    v = view(api)
    assert v["mode"] == "shadow" and v["mode_label"] == "SHADOW MODE — NO ORDERS SENT"
    assert v["automation"]["label"] == "STOPPED" and v["automation"]["desired"] == "stopped"
    assert v["automation"]["unavailable_reason"] == (
        "Automation unavailable — deployment scheduler master gate is OFF."
    )
    assert v["kill_switch"]["label"] == "ENGAGED"
    assert v["broker"]["status"] in ("NOT CONFIGURED", "ALPACA PAPER (not verified)")
    assert v["eligible_count"] == 0 and v["live_trading"] == "LOCKED / NOT AVAILABLE"
    assert v["real_money_possible"] is False and v["uses_real_money"] is False
    items = {i["key"]: i for i in v["start_readiness"]["items"]}
    assert not items["master_gate"]["ok"] and "AQ_SCHEDULER_ENABLED" in items["master_gate"]["fix"]
    assert not items["strategies"]["ok"] and items["strategies"]["fix"]
    worker(repo)
    verified(repo)
    assert view(api)["broker"]["status"] == "ALPACA PAPER"
    verified(repo, failing=("authentication",))
    assert view(api)["broker"]["status"] == "UNAVAILABLE"


def test_settings_show_trading_mode_without_live_option(api: TestClient) -> None:
    s = api.get("/api/v1/settings", headers=AUTH).json()["trading_mode"]
    assert s["active"] == "shadow"
    assert [o["value"] for o in s["options"]] == ["shadow", "paper"]
    assert "no orders are sent" in s["options"][0]["explanation"]
    assert "simulated funds" in s["options"][1]["explanation"]
    assert s["live_trading"] == "LOCKED / NOT AVAILABLE"


# ================================================================ shadow -> paper
@pytest.mark.parametrize(
    ("setup", "problem"),
    [
        (lambda r: None, "not been verified"),
        (lambda r: verified(r, paper=False, failing=("paper_account",)), "paper_account"),
        (lambda r: verified(r, age=timedelta(hours=2)), "older than"),
        (lambda r: verified(r, failing=("credentials",)), "credentials"),
        (lambda r: verified(r, failing=("authentication",)), "authentication"),
        (lambda r: verified(r, failing=("broker_connectivity",)), "broker_connectivity"),
        (lambda r: verified(r, failing=("provider",)), "provider"),
        (lambda r: verified(r, host="api.alpaca.markets"), "not the Alpaca paper endpoint"),
        (lambda r: verified(r, failing=("kill_switch_known",)), "kill_switch_known"),
    ],
)
def test_shadow_to_paper_needs_every_paper_prerequisite(
    api: TestClient,
    repo: ControlRepository,
    setup: Callable[[ControlRepository], None],
    problem: str,
) -> None:
    worker(repo)
    setup(repo)
    r = post(api, "trading/mode", TO_PAPER)
    assert r.status_code == 409 and problem in r.json()["detail"], r.text
    assert view(api)["mode"] == "shadow"
    (ev,) = repo.events(10, "trading.mode")
    assert ev["outcome"] == "refused" and ev["actor"] == ACTOR


def test_switch_phrase_is_exact(api: TestClient, repo: ControlRepository) -> None:
    worker(repo)
    verified(repo)
    for wrong in ("switch to paper trading", "ENABLE PAPER TRADING", "yes"):
        assert post(api, "trading/mode", {**TO_PAPER, "confirm": wrong}).status_code == 400


def test_shadow_to_paper_is_refused_while_automation_runs(
    api: TestClient, repo: ControlRepository
) -> None:
    verified(repo)
    worker(repo, state="running")
    r = post(api, "trading/mode", TO_PAPER)
    assert r.status_code == 409 and "stop automation" in r.json()["detail"]


def test_paper_selection_persists_and_changes_nothing_else(
    api: TestClient, repo: ControlRepository, db: Database, tmp: Path
) -> None:
    lifecycles = {
        s["strategy_id"]: s["lifecycle"]
        for s in api.get("/api/v1/strategies/manager", headers=AUTH).json()["strategies"]
    }
    to_paper(api, repo)
    # persisted in PostgreSQL: a brand-new API process (restart) sees PAPER
    fresh = TestClient(create_app(services(tmp, db)))
    v = view(fresh)
    assert v["mode"] == "paper" and v["mode_label"] == "PAPER TRADING — SIMULATED FUNDS"
    assert fresh.get("/api/v1/configuration", headers=AUTH).json()["banner"]["mode"] == "paper"
    assert repo.runtime_config()["overlay"] == {"trading.mode": "paper"}
    # ...and did not start automation, release the kill switch or promote anything
    assert v["automation"]["desired"] == "stopped" and not ctl.desired_running(
        repo.get_state(ctl.SCHEDULER_KEY)
    )
    assert v["kill_switch"]["engaged"] is True
    assert {
        s["strategy_id"]: s["lifecycle"]
        for s in fresh.get("/api/v1/strategies/manager", headers=AUTH).json()["strategies"]
    } == lifecycles
    assert repo.runtime_config()["strategy_overrides"] == {}
    # audited with every required field, and no secret anywhere
    (ev,) = [e for e in repo.events(20, "trading.mode") if e["outcome"] == "accepted"]
    d = ev["detail"]
    assert (d["previous_mode"], d["requested_mode"], d["resulting_mode"]) == (
        "shadow",
        "paper",
        "paper",
    )
    assert d["confirmation"] == "typed" and ev["actor"] == ACTOR and ev["at"]
    assert d["broker_verification"]["is_paper"] is True
    (change,) = repo.runtime_changes(10)
    assert (change["old"], change["new"], change["actor"]) == ("shadow", "paper", ACTOR)
    assert TOKEN not in json.dumps(repo.events(100))


def test_worker_resolves_the_persisted_mode(
    api: TestClient, repo: ControlRepository, tmp: Path, db: Database
) -> None:
    from adaptive_quant.config.loader import load_config
    from tests.conftest import REPO_CONFIG

    to_paper(api, repo)
    base = load_config("development", config_dir=REPO_CONFIG)  # a fresh process, same database
    effective, row = effective_config(base, db)
    assert effective.settings.trading.mode.value == "paper" and row["revision"] == 1
    assert effective.config_version != base.config_version
    assert not effective.settings.trading.mode.uses_real_money


# ================================================================ paper -> shadow
def test_paper_to_shadow_stops_automation_first(
    api: TestClient, repo: ControlRepository, db: Database
) -> None:
    from adaptive_quant.config.loader import load_config
    from adaptive_quant.worker.scheduler import make_transmit_guard
    from tests.conftest import REPO_CONFIG

    to_paper(api, repo)
    repo.set_state(ctl.SCHEDULER_KEY, {"desired": "running", "mode": "paper"}, ACTOR, NOW)
    guard = make_transmit_guard(
        load_config("development", config_dir=REPO_CONFIG),
        db,
        {"AQ_SCHEDULER_ENABLED": "true"},
        "paper",
    )
    assert guard() is None  # paper orders may be transmitted right now
    r = post(api, "trading/mode", TO_SHADOW)
    assert r.status_code == 200, r.text
    assert "no paper order can be transmitted" in r.json()["message"]
    assert not ctl.desired_running(repo.get_state(ctl.SCHEDULER_KEY))
    assert view(api)["mode"] == "shadow"
    assert guard() is not None  # the running worker can no longer transmit a paper order
    actions = [e["action"] for e in repo.events(20) if e["outcome"] == "accepted"]
    assert actions.index("scheduler.stop") > actions.index("trading.mode")  # newest first
    (ev,) = [
        e
        for e in repo.events(20, "trading.mode")
        if e["outcome"] == "accepted" and e["detail"]["requested_mode"] == "shadow"
    ]
    assert ev["detail"]["automation_stopped"] is True


def test_paper_to_shadow_needs_no_broker(api: TestClient, repo: ControlRepository) -> None:
    to_paper(api, repo)
    repo.set_state(ctl.BROKER_KEY, {"ok": False}, "worker", NOW)  # broker now broken
    assert post(api, "trading/mode", TO_SHADOW).status_code == 200  # stopping never blocked


# ================================================================ start / stop paper automation
def ready_for_paper(api: TestClient, repo: ControlRepository) -> None:
    promote(api)
    to_paper(api, repo)
    data(repo)
    release(api)


def test_start_paper_trading_succeeds_only_when_every_gate_passes(
    api: TestClient, repo: ControlRepository
) -> None:
    ready_for_paper(api, repo)
    v = view(api)
    assert v["start_readiness"]["ready"], v["start_readiness"]
    r = post(api, "trading/scheduler/start", START_PAPER)
    assert r.status_code == 200, r.text
    st = repo.get_state(ctl.SCHEDULER_KEY)
    assert ctl.desired_running(st) and st["value"]["mode"] == "paper"  # type: ignore[index]


@pytest.mark.parametrize(
    ("breaker", "item"),
    [
        (lambda api, repo: worker(repo, gate=False), "master gate"),
        (
            lambda api, repo: post(
                api,
                "kill-switch/engage",
                {"actor": ACTOR, "reason": "precaution", "confirm": "STOP AUTOMATED TRADING"},
            ),
            "Kill switch",
        ),
        (
            lambda api, repo: post(
                api,
                "strategies/baseline_buy_hold/lifecycle",
                {"actor": ACTOR, "reason": "not ready yet", "target": "validated", "confirm": ""},
            ),
            "approved for PAPER",
        ),
        (lambda api, repo: data(repo, fresh=False), "stale"),
        (lambda api, repo: data(repo, valid=False), "failed validation"),
        (lambda api, repo: repo.replace_inventory([], NOW), "no stored daily data"),
        (lambda api, repo: verified(repo, age=timedelta(hours=3)), "older than"),
        (lambda api, repo: verified(repo, failing=("reconciliation",)), "reconciliation"),
        (lambda api, repo: verified(repo, failing=("market_data",)), "market_data"),
        (lambda api, repo: worker(repo, state="running"), "already running"),
    ],
)
def test_start_paper_trading_refuses_when_a_gate_fails(
    api: TestClient, repo: ControlRepository, breaker: Callable[..., Any], item: str
) -> None:
    ready_for_paper(api, repo)
    breaker(api, repo)
    r = post(api, "trading/scheduler/start", START_PAPER)
    assert r.status_code == 409 and item in r.json()["detail"], r.text
    assert not ctl.desired_running(repo.get_state(ctl.SCHEDULER_KEY))
    items = view(api)["start_readiness"]["items"]
    bad = [i for i in items if not i["ok"]]
    assert bad and all(i["detail"] for i in bad)  # every failure explained in the checklist


def test_start_needs_the_exact_phrase(api: TestClient, repo: ControlRepository) -> None:
    ready_for_paper(api, repo)
    r = post(api, "trading/scheduler/start", {**START_PAPER, "confirm": "START SHADOW TRADING"})
    assert r.status_code == 400


def test_stop_paper_trading_always_succeeds_and_blocks_transmission(
    api: TestClient, repo: ControlRepository, db: Database
) -> None:
    from adaptive_quant.config.loader import load_config
    from adaptive_quant.worker.scheduler import make_transmit_guard
    from tests.conftest import REPO_CONFIG

    ready_for_paper(api, repo)
    assert post(api, "trading/scheduler/start", START_PAPER).status_code == 200
    guard = make_transmit_guard(
        load_config("development", config_dir=REPO_CONFIG),
        db,
        {"AQ_SCHEDULER_ENABLED": "true"},
        "paper",
    )
    assert guard() is None
    repo.set_state(ctl.BROKER_KEY, {"ok": False}, "worker", NOW)  # even with a broken broker
    r = post(api, "trading/scheduler/stop", {"actor": ACTOR, "reason": "end of test"})
    assert r.status_code == 200 and "no new order is transmitted" in r.json()["message"]
    assert guard() == "automation was stopped by the operator"
    v = view(api)
    assert v["mode"] == "paper" and v["automation"]["desired"] == "stopped"


@pytest.mark.parametrize("target", ["live", "LIVE", "Live", "real", "backtest"])
def test_live_is_never_reachable_through_the_mode_api(api: TestClient, target: str) -> None:
    r = post(api, "trading/mode", {**TO_PAPER, "target": target})
    assert r.status_code == 422


def test_overlay_can_never_hold_live(repo: ControlRepository, db: Database) -> None:
    from adaptive_quant.config.loader import load_config
    from tests.conftest import REPO_CONFIG

    repo.save_runtime_config(
        expected_revision=0,
        overlay={"trading.mode": "live"},
        strategy_overrides={},
        actor="attacker",
        now=NOW,
        changes=[],
        version_before="a",
        version_after="b",
    )
    with pytest.raises(SafetyViolation):
        effective_config(load_config("development", config_dir=REPO_CONFIG), db)


def test_no_secret_reaches_trading_responses(api: TestClient, repo: ControlRepository) -> None:
    ready_for_paper(api, repo)
    for path in ("trading/control", "settings", "audit/events", "jobs"):
        text = api.get(f"/api/v1/{path}", headers=AUTH).text
        assert TOKEN not in text, path
        assert "postgresql://" not in text.lower() and "postgresql+" not in text.lower(), path
