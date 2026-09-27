"""The control plane: every mutation authenticated, strictly validated, audited, bounded."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adaptive_quant.api.app import MUTATING_ROUTES, create_app
from adaptive_quant.control import state as ctl
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database
from adaptive_quant.services import data as data_service
from tests.api.conftest import AUTH, NOW, TOKEN, services
from tests.data_helpers import daily_bars

pytestmark = pytest.mark.postgres
ACTOR = "Jane Operator"
WHY = "reviewed the walk-forward evidence and cost sensitivity in detail"
ZERO = "0" * 32


@pytest.fixture
def api(tmp_path: Path, db: Database) -> TestClient:
    return TestClient(create_app(services(tmp_path, db)))


@pytest.fixture
def repo(db: Database) -> ControlRepository:
    return ControlRepository(db)


def post(api: TestClient, path: str, body: dict[str, Any] | None = None) -> Any:
    return api.post(f"/api/v1/{path}", headers=AUTH, json=body or {})


def events(repo: ControlRepository, prefix: str) -> list[dict[str, Any]]:
    return repo.events(100, prefix)


def csv_bytes(start: date = date(2023, 1, 3), end: date = date(2024, 7, 1)) -> bytes:
    df = daily_bars(start, end, start_price=300, vol=0.01, seed=5).reset_index()
    df.insert(0, "date", df.pop("timestamp").dt.tz_convert("America/New_York").dt.date)
    return (
        df.drop(columns=[c for c in ("is_synthetic",) if c in df.columns])
        .to_csv(index=False)
        .encode()
    )


def upload(api: TestClient, content: bytes, **query: str) -> Any:
    q = {"symbol": "QQQ", "actor": ACTOR, "filename": "qqq.csv", **query}
    return api.post(
        "/api/v1/data/uploads",
        params=q,
        content=content,
        headers={**AUTH, "Content-Type": query.pop("ctype", "text/csv")},
    )


def worker_online(repo: ControlRepository, **status: Any) -> None:
    repo.beat("w1", NOW, NOW, {"master_gate": False, "credentials_configured": {}, **status})


# ================================================================ auth
def test_every_mutation_needs_the_token(api: TestClient) -> None:
    for method, path in MUTATING_ROUTES:
        concrete = path.replace("{job_id}", ZERO).replace("{upload_id}", ZERO)
        concrete = concrete.replace("{strategy_id}", "baseline_buy_hold")
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Basic {TOKEN}"}):
            r = api.request(method, concrete, headers=headers, json={"actor": ACTOR})
            assert r.status_code == 401, (path, headers)


# ================================================================ jobs
def test_backtest_job_is_queued_audited_and_deduplicated(
    api: TestClient, repo: ControlRepository
) -> None:
    body = {"actor": ACTOR, "params": {"strategies": ["baseline_buy_hold"], "source": "file"}}
    r = post(api, "jobs/backtest", body)
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["status"] == "queued" and job["job_type"] == "backtest.run"
    assert job["requested_by"] == ACTOR and job["retryable"] is False
    assert "No worker is online" in r.json()["message"]
    again = post(api, "jobs/backtest", body).json()
    assert again["job"]["id"] == job["id"] and "already" in again["message"]
    (ev, _dup) = events(repo, "job.backtest")
    assert ev["actor"] == ACTOR and ev["outcome"] == "accepted"
    listed = api.get("/api/v1/jobs", headers=AUTH).json()
    assert [j["id"] for j in listed["jobs"]] == [job["id"]] and listed["worker"] is None
    detail = api.get(f"/api/v1/jobs/{job['id']}", headers=AUTH).json()
    assert detail["logs"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"strategies": []},
        {"strategies": ["baseline_buy_hold"], "initial_capital": -1},
        {"strategies": ["baseline_buy_hold"], "initial_capital": 10**12},
        {"strategies": ["baseline_buy_hold"], "execution": "whenever"},
        {"strategies": ["baseline_buy_hold"], "execution_delay_bars": 99},
        {"strategies": ["baseline_buy_hold"], "costs": {"shell": 1}},
        {"strategies": ["baseline_buy_hold"], "command": "rm -rf /"},
        {"strategies": ["baseline_buy_hold"], "out_dir": "/etc"},
        {"strategies": ["baseline_buy_hold"], "start": "2020-05-01", "end": "2019-01-01"},
    ],
)
def test_backtest_parameters_are_strictly_validated(
    api: TestClient, params: dict[str, Any]
) -> None:
    assert post(api, "jobs/backtest", {"actor": ACTOR, "params": params}).status_code == 422


def test_unknown_strategy_is_refused_and_audited(api: TestClient, repo: ControlRepository) -> None:
    r = post(api, "jobs/backtest", {"actor": ACTOR, "params": {"strategies": ["evil"]}})
    assert r.status_code == 400 and "evil" in r.json()["detail"]
    assert events(repo, "job.backtest")[0]["outcome"] == "refused"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"actor": ""},
        {"actor": "x"},
        {"actor": "a\nb"},
        {"actor": ACTOR, "extra": 1},
        {"actor": ACTOR, "params": {"provider": "ftp"}},
        {"actor": ACTOR, "params": {"symbols": ["../etc"]}},
    ],
)
def test_download_requests_are_strict(api: TestClient, body: dict[str, Any]) -> None:
    assert post(api, "jobs/data/download", body).status_code == 422


def test_other_job_types_queue(api: TestClient) -> None:
    for path, params in [
        ("jobs/data/download", {"provider": "file", "symbols": ["QQQ"]}),
        ("jobs/data/validate", {}),
        ("jobs/data/synthesize", {}),
        ("jobs/data/inventory", {}),
        ("jobs/research", {"strategies": ["baseline_buy_hold"]}),
        ("jobs/broker/verify", {}),
    ]:
        r = post(api, path, {"actor": ACTOR, "params": params})
        assert r.status_code == 200, (path, r.text)


def test_cancel_and_retry_rules(api: TestClient, repo: ControlRepository) -> None:
    job = post(api, "jobs/data/inventory", {"actor": ACTOR}).json()["job"]
    assert post(api, f"jobs/{job['id']}/retry", {"actor": ACTOR}).status_code == 409  # active
    r = post(api, f"jobs/{job['id']}/cancel", {"actor": ACTOR, "reason": "not needed"})
    assert r.json()["job"]["status"] == "cancelled"
    assert post(api, f"jobs/{job['id']}/cancel", {"actor": ACTOR, "reason": "again!"}).status_code
    retried = post(api, f"jobs/{job['id']}/retry", {"actor": ACTOR}).json()["job"]
    assert retried["retry_of"] == job["id"] and retried["status"] == "queued"
    imp, _ = repo.create_job("data.import_upload", {"upload_id": ZERO}, ACTOR, NOW)
    repo.request_cancel(imp["id"], NOW)
    r = post(api, f"jobs/{imp['id']}/retry", {"actor": ACTOR})
    assert r.status_code == 409 and "not safe to retry" in r.json()["detail"]
    assert (
        post(api, "jobs/not-a-job-id/cancel", {"actor": ACTOR, "reason": "xxxxx"}).status_code
        == 422
    )


def test_job_queue_is_rate_limited(api: TestClient) -> None:
    codes = [
        post(
            api, "jobs/data/validate", {"actor": ACTOR, "params": {"symbols": [f"S{i}"]}}
        ).status_code
        for i in range(32)
    ]
    assert codes[:30] == [200] * 30 and codes[30:] == [429, 429]


def test_scheduler_stop_is_never_rate_limited(api: TestClient) -> None:
    for _ in range(30):
        post(
            api,
            "settings",
            {
                "actor": ACTOR,
                "reason": "flood test",
                "expected_revision": 99,
                "changes": [{"path": "backtest.initial_capital", "value": 1}],
            },
        )
    assert (
        post(
            api,
            "trading/mode",
            {"actor": ACTOR, "reason": "flood", "target": "shadow", "confirm": "x"},
        ).status_code
        == 429
    )
    r = post(api, "trading/scheduler/stop", {"actor": ACTOR, "reason": "stop now please"})
    assert r.status_code == 200


# ================================================================ uploads
@pytest.mark.parametrize(
    "name",
    [
        "../qqq.csv",
        "..\\qqq.csv",
        "/etc/passwd.csv",
        "a/b.csv",
        "qqq.exe",
        "q q?.csv",
        "qqq.csv.sh",
        "..",
    ],
)
def test_upload_names_are_never_paths(api: TestClient, name: str) -> None:
    r = upload(api, csv_bytes(), filename=name)
    assert r.status_code in (400, 422), name


def test_upload_content_type_size_and_symbol(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert upload(api, csv_bytes(), ctype="application/x-sh").status_code == 415
    assert upload(api, csv_bytes(), symbol="../QQQ").status_code == 422
    assert upload(api, csv_bytes(), symbol="qqq").status_code == 422
    assert upload(api, csv_bytes(), kind="script").status_code == 422
    monkeypatch.setattr(data_service, "MAX_UPLOAD_BYTES", 1000)
    assert upload(api, csv_bytes()).status_code == 413


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (b"", "empty"),
        (b"\x00\x01binary", "binary"),
        (b"date,\xff\xfebad\n", "UTF-8"),
        (b"date,open,high,low,close,volume\n2024-01-02,1,2,0.5,x,100\n", ""),
        (b"when,price\n2024-01-02,1\n", ""),
    ],
)
def test_malformed_uploads_are_rejected_or_marked_invalid(
    api: TestClient, content: bytes, problem: str
) -> None:
    r = upload(api, content)
    if r.status_code == 200:
        assert r.json()["ok"] is False and r.json()["detail"]["upload"]["status"] == "invalid"
    else:
        assert r.status_code == 400 and problem.lower() in r.json()["detail"].lower()


def test_valid_upload_previews_then_imports_through_a_job(
    api: TestClient, repo: ControlRepository
) -> None:
    content = csv_bytes()
    r = upload(api, content)
    assert r.status_code == 200, r.text
    up = r.json()["detail"]["upload"]
    assert r.json()["ok"] is True and up["status"] == "validated"
    assert up["preview"]["rows"] > 300 and up["preview"]["head"]
    assert "content" not in up and up["size_bytes"] == len(content)
    assert not any("/" in str(v) for k, v in up.items() if k == "filename_hint")
    r = post(api, f"data/uploads/{up['id']}/import", {"actor": ACTOR})
    assert r.status_code == 200 and r.json()["job"]["job_type"] == "data.import_upload"
    assert repo.get_upload(up["id"])["status"] == "queued"  # type: ignore[index]
    again = post(api, f"data/uploads/{up['id']}/import", {"actor": ACTOR})
    assert again.status_code == 409  # only a validated upload can be imported (once)


def test_duplicate_and_ohlc_problems_block_import(api: TestClient) -> None:
    rows = csv_bytes().decode().splitlines()
    dup = "\n".join([*rows, rows[-1]]).encode()
    r = upload(api, dup)
    up = r.json()["detail"]["upload"]
    assert up["status"] == "invalid"
    assert post(api, f"data/uploads/{up['id']}/import", {"actor": ACTOR}).status_code == 409
    d = post(api, f"data/uploads/{up['id']}/discard", {"actor": ACTOR, "reason": "bad file"})
    assert d.status_code == 200


# ================================================================ strategies
def test_lifecycle_promotion_needs_confirmation_justification_and_governance(
    api: TestClient, repo: ControlRepository, db: Database
) -> None:
    path = "strategies/baseline_buy_hold/lifecycle"
    base = {"actor": ACTOR, "reason": WHY, "target": "validated"}
    assert post(api, path, base).status_code == 400  # no confirmation
    short = {**base, "reason": "looks good", "confirm": ctl.PROMOTION_CONFIRM}
    r = post(api, path, short)
    assert r.status_code == 400 and "justification" in r.json()["detail"]
    skip = {**base, "target": "shadow", "confirm": ctl.PROMOTION_CONFIRM}
    assert post(api, path, skip).status_code == 400  # research -> shadow skips steps
    assert post(api, path, {**base, "confirm": ctl.PROMOTION_CONFIRM}).status_code == 200
    r = post(api, path, {**base, "target": "paper", "confirm": ctl.PROMOTION_CONFIRM})
    assert r.status_code == 200, r.text
    rows = {
        s["strategy_id"]: s
        for s in api.get("/api/v1/strategies/manager", headers=AUTH).json()["strategies"]
    }
    assert rows["baseline_buy_hold"]["lifecycle"] == "paper"
    assert rows["baseline_buy_hold"]["eligible_for_paper_or_shadow"] is True
    from adaptive_quant.persistence.reads import AuditReads

    ledger = AuditReads(db).lifecycle_events(10)
    assert sorted(e["to_state"] for e in ledger) == ["paper", "validated"]
    assert all(e["actor_kind"] == "human" for e in ledger)
    assert len(repo.runtime_changes(10)) == 2


@pytest.mark.parametrize("target", ["live_approved", "LIVE_APPROVED", "live"])
def test_live_approved_is_never_grantable(api: TestClient, target: str) -> None:
    body = {"actor": ACTOR, "reason": WHY, "target": target, "confirm": ctl.PROMOTION_CONFIRM}
    assert post(api, "strategies/baseline_buy_hold/lifecycle", body).status_code == 422


def test_params_editable_only_in_research(api: TestClient) -> None:
    sid = "mom_time_series"
    ok = post(
        api,
        f"strategies/{sid}/params",
        {"actor": ACTOR, "reason": "try a longer window", "params": {"window": 315}},
    )
    assert ok.status_code == 200, ok.text
    bad = post(
        api,
        f"strategies/{sid}/params",
        {"actor": ACTOR, "reason": "invalid parameter", "params": {"nope": 1}},
    )
    assert bad.status_code in (400, 409)
    post(
        api,
        f"strategies/{sid}/lifecycle",
        {"actor": ACTOR, "reason": WHY, "target": "validated", "confirm": ctl.PROMOTION_CONFIRM},
    )
    locked = post(
        api,
        f"strategies/{sid}/params",
        {"actor": ACTOR, "reason": "tweak after validation", "params": {"window": 252}},
    )
    assert locked.status_code == 409


def test_enabled_is_separate_from_eligibility(api: TestClient) -> None:
    r = post(
        api,
        "strategies/baseline_cash/enabled",
        {"actor": ACTOR, "reason": "not needed now", "enabled": False},
    )
    assert r.status_code == 200 and "eligibility" in r.json()["message"]
    opts = api.get("/api/v1/backtests/options", headers=AUTH).json()
    assert "baseline_cash" not in {s["strategy_id"] for s in opts["strategies"]}
    assert (
        post(
            api, "jobs/backtest", {"actor": ACTOR, "params": {"strategies": ["baseline_cash"]}}
        ).status_code
        == 400
    )


# ================================================================ settings
def change(api: TestClient, rev: int, *changes: dict[str, Any], confirm: str = "") -> Any:
    return post(
        api,
        "settings",
        {
            "actor": ACTOR,
            "reason": "operator change",
            "confirm": confirm,
            "expected_revision": rev,
            "changes": list(changes),
        },
    )


def test_settings_changes_are_validated_versioned_and_audited(
    api: TestClient, repo: ControlRepository
) -> None:
    view = api.get("/api/v1/settings", headers=AUTH).json()
    assert view["runtime"]["revision"] == 0
    r = change(api, 0, {"path": "backtest.initial_capital", "value": 50000})
    assert r.status_code == 200, r.text
    assert r.json()["detail"]["revision"] == 1
    assert change(api, 0, {"path": "backtest.initial_capital", "value": 1}).status_code == 409
    (c,) = repo.runtime_changes(10)
    assert (c["path"], c["old"], c["new"], c["actor"]) == (
        "backtest.initial_capital",
        100000.0,
        50000,
        ACTOR,
    )
    assert c["config_version_before"] != c["config_version_after"]
    opts = api.get("/api/v1/backtests/options", headers=AUTH).json()
    assert opts["defaults"]["initial_capital"] == 50000
    reset = change(api, 1, {"path": "backtest.initial_capital", "reset": True})
    assert reset.status_code == 200
    assert api.get("/api/v1/settings", headers=AUTH).json()["runtime"]["overlay"] == {}


@pytest.mark.parametrize(
    "path",
    [
        "trading.mode",
        "trading.live_trading.enabled",
        "broker.alpaca.paper_base_url",
        "research.gates.min_dsr",
        "risk.drawdown_hysteresis",
        "data.staleness.max_age_sessions",
    ],
)
def test_protected_settings_cannot_be_changed(api: TestClient, path: str) -> None:
    assert change(api, 0, {"path": path, "value": "live"}).status_code == 400


def test_risk_limits_tighten_only_with_confirmation(api: TestClient) -> None:
    tighter = {"path": "risk.max_daily_loss", "value": 0.03}
    assert change(api, 0, tighter).status_code == 400  # no confirmation
    looser = {"path": "risk.max_daily_loss", "value": 0.5}
    r = change(api, 0, looser, confirm=ctl.RISK_CONFIRM)
    assert r.status_code == 400 and "loosen" in r.json()["detail"]
    assert change(api, 0, tighter, confirm=ctl.RISK_CONFIRM).status_code == 200


def test_settings_view_has_no_secret_values(api: TestClient, repo: ControlRepository) -> None:
    worker_online(repo, credentials_configured={"ALPACA_API_KEY_ID": True, "DATABASE_URL": True})
    text = json.dumps(api.get("/api/v1/settings", headers=AUTH).json())
    for bad in (TOKEN, "postgresql", "sk-", "AQ_API_TOKEN"):
        assert bad not in text
    view = json.loads(text)
    assert all(s["configured"] in (True, False, None) for s in view["secrets"])
    assert {s["class"] for s in view["settings"]} >= {"runtime", "runtime_confirm", "immutable"}


# ================================================================ trading control
def promote_to_paper(api: TestClient) -> None:
    for target in ("validated", "paper"):
        r = post(
            api,
            "strategies/baseline_buy_hold/lifecycle",
            {"actor": ACTOR, "reason": WHY, "target": target, "confirm": ctl.PROMOTION_CONFIRM},
        )
        assert r.status_code == 200, r.text


def verified(repo: ControlRepository, *, age: timedelta = timedelta(0), paper: bool = True) -> None:
    repo.set_state(
        ctl.BROKER_KEY,
        {
            "ok": True,
            "is_paper": paper,
            "paper_endpoint": True,
            "verified_at": (NOW - age).isoformat(),
        },
        "worker",
        NOW,
    )


def test_trading_control_view_is_locked_to_simulated_money(
    api: TestClient, repo: ControlRepository
) -> None:
    v = api.get("/api/v1/trading/control", headers=AUTH).json()
    assert v["mode"] == "shadow" and v["real_money_possible"] is False
    assert v["uses_real_money"] is False and v["kill_switch"]["engaged"] is True
    assert v["scheduler"]["desired"] == "stopped" and v["scheduler"]["master_gate"] is False
    assert v["broker"]["paper_endpoint"] is True and v["broker"]["live_adapter_available"] is False
    assert v["eligible_strategies"] == []


def test_scheduler_start_requires_phrase_and_eligible_strategies(
    api: TestClient, repo: ControlRepository
) -> None:
    body = {"actor": ACTOR, "reason": "begin shadow run", "confirm": "yes"}
    assert post(api, "trading/scheduler/start", body).status_code == 400
    body["confirm"] = ctl.START_SHADOW_CONFIRM
    r = post(api, "trading/scheduler/start", body)
    assert r.status_code == 409 and "eligible" in r.json()["detail"]
    promote_to_paper(api)
    r = post(api, "trading/scheduler/start", body)
    assert r.status_code == 200 and "AQ_SCHEDULER_ENABLED is off" in r.json()["message"]
    assert ctl.desired_running(repo.get_state(ctl.SCHEDULER_KEY))
    r = post(api, "trading/scheduler/stop", {"actor": ACTOR, "reason": "end of test"})
    assert r.status_code == 200 and not ctl.desired_running(repo.get_state(ctl.SCHEDULER_KEY))
    actions = [e["action"] for e in events(repo, "scheduler")]
    assert actions[:3] == ["scheduler.stop", "scheduler.start", "scheduler.start"]


def test_paper_mode_requires_a_fresh_verified_paper_account(
    api: TestClient, repo: ControlRepository
) -> None:
    body = {
        "actor": ACTOR,
        "reason": "move to paper",
        "target": "paper",
        "confirm": ctl.PAPER_MODE_CONFIRM,
    }
    assert post(api, "trading/mode", {**body, "confirm": "paper"}).status_code == 400
    r = post(api, "trading/mode", body)
    assert r.status_code == 409 and "Verify broker" in r.json()["detail"]
    verified(repo, paper=False)
    assert post(api, "trading/mode", body).status_code == 409
    verified(repo, age=timedelta(hours=2))
    r = post(api, "trading/mode", body)
    assert r.status_code == 409 and "older" in r.json()["detail"]
    verified(repo)
    r = post(api, "trading/mode", body)
    assert r.status_code == 200 and "simulated funds" in r.json()["message"]
    v = api.get("/api/v1/trading/control", headers=AUTH).json()
    assert v["mode"] == "paper" and v["uses_real_money"] is False
    back = {
        "actor": ACTOR,
        "reason": "back to shadow",
        "target": "shadow",
        "confirm": ctl.SHADOW_MODE_CONFIRM,
    }
    assert post(api, "trading/mode", back).status_code == 200


def test_mode_cannot_change_while_the_scheduler_runs(
    api: TestClient, repo: ControlRepository
) -> None:
    verified(repo)
    repo.set_state(ctl.SCHEDULER_KEY, {"desired": "running"}, ACTOR, NOW)
    body = {
        "actor": ACTOR,
        "reason": "move to paper",
        "target": "paper",
        "confirm": ctl.PAPER_MODE_CONFIRM,
    }
    r = post(api, "trading/mode", body)
    assert r.status_code == 409 and "stop the scheduler" in r.json()["detail"]
    repo.set_state(ctl.SCHEDULER_KEY, {"desired": "stopped"}, ACTOR, NOW)
    worker_online(repo, scheduler={"state": "running"})
    assert post(api, "trading/mode", body).status_code == 409


@pytest.mark.parametrize("target", ["live", "LIVE", "backtest"])
def test_live_mode_is_not_an_option(api: TestClient, target: str) -> None:
    body = {"actor": ACTOR, "reason": "go live now", "target": target, "confirm": "GO LIVE"}
    assert post(api, "trading/mode", body).status_code == 422


def test_live_readiness_is_informational_and_locked(api: TestClient) -> None:
    v = api.get("/api/v1/trading/live-readiness", headers=AUTH).json()
    assert v["live_possible"] is False
    assert not all(i["satisfied"] for i in v["items"])
    assert any("AQ_LIVE_TRADING_CONFIRM" in i["requirement"] for i in v["items"])


def test_kill_switch_changes_are_audited_in_the_control_log(
    api: TestClient, repo: ControlRepository
) -> None:
    from adaptive_quant.api.app import ENGAGE_CONFIRMATION

    body = {"actor": ACTOR, "reason": "precaution", "confirm": ENGAGE_CONFIRMATION}
    assert post(api, "kill-switch/engage", body).status_code == 200
    assert events(repo, "kill_switch")[0]["outcome"] == "accepted"


def test_audit_events_contain_no_credentials(api: TestClient) -> None:
    post(api, "jobs/data/inventory", {"actor": ACTOR})
    text = api.get("/api/v1/audit/events", headers=AUTH).text
    assert TOKEN not in text and "Bearer" not in text
