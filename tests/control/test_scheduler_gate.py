"""The scheduler runs only while the deployment master gate AND the operator switch allow it."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from adaptive_quant.config.loader import LoadedConfig, load_config
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.control.state import SCHEDULER_KEY, desired_running, master_gate
from adaptive_quant.core.clock import Clock, FrozenClock
from adaptive_quant.core.errors import SafetyViolation
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.locks import AdvisoryLock
from adaptive_quant.worker import scheduler as sched_mod
from adaptive_quant.worker.scheduler import SchedulerSupervisor
from tests.api.conftest import NOW
from tests.conftest import REPO_CONFIG

pytestmark = pytest.mark.postgres
ON = {"AQ_SCHEDULER_ENABLED": "true"}


class FakeKillSwitch:
    def __init__(self, engaged: bool) -> None:
        self.engaged = engaged

    def is_engaged(self) -> bool:
        return self.engaged


class FakeDeps:
    engaged = False

    def __init__(self, db: Database) -> None:
        self.db = db
        self.kill_switch = FakeKillSwitch(FakeDeps.engaged)
        self.transmit_guard: Any = None


class FakeScheduler:
    ticks = 0

    def __init__(self, deps: FakeDeps) -> None:
        self.deps = deps

    def tick(self) -> dict[str, str]:
        FakeScheduler.ticks += 1
        return {"step": "noop"}

    def next_wake(self) -> datetime:
        return NOW + timedelta(minutes=5)


@pytest.fixture
def base() -> LoadedConfig:
    return load_config("development", config_dir=REPO_CONFIG)


@pytest.fixture(autouse=True)
def fake_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeScheduler.ticks = 0
    FakeDeps.engaged = False
    monkeypatch.setattr(sched_mod, "Scheduler", FakeScheduler)


def supervisor(base: LoadedConfig, db: Database, environ: dict[str, str]) -> SchedulerSupervisor:
    built: list[str] = []

    def build(cfg: LoadedConfig, _s: Secrets, _c: Clock) -> Any:
        built.append(cfg.settings.trading.mode.value)
        return FakeDeps(Database(db.url.render_as_string(hide_password=False), pool_size=1))

    sup = SchedulerSupervisor(base, Secrets(), FrozenClock(NOW), db, environ, build)
    sup.built = built  # type: ignore[attr-defined]
    return sup


def want(db: Database, desired: str) -> None:
    ControlRepository(db).set_state(SCHEDULER_KEY, {"desired": desired}, "op", NOW)


@pytest.mark.parametrize(
    ("value", "on"),
    [
        ("true", True),
        ("1", True),
        (" YES ", True),
        ("on", True),
        ("", False),
        ("false", False),
        ("0", False),
        ("enabled", False),
    ],
)
def test_master_gate_is_default_off(value: str, on: bool) -> None:
    assert master_gate({"AQ_SCHEDULER_ENABLED": value}) is on
    assert master_gate({}) is False


def test_operator_switch_defaults_to_stopped() -> None:
    assert not desired_running(None)
    assert not desired_running({"value": {"desired": "RUNNING"}})
    assert not desired_running({"value": "running"})
    assert desired_running({"value": {"desired": "running"}})


def test_master_gate_off_wins_over_the_ui(base: LoadedConfig, db: Database) -> None:
    want(db, "running")
    sup = supervisor(base, db, {"AQ_SCHEDULER_ENABLED": "false"})
    sup.step()
    assert not sup.running and sup.built == []  # type: ignore[attr-defined]
    assert sup.status.state == "blocked" and "AQ_SCHEDULER_ENABLED" in sup.status.detail


def test_default_state_never_starts(base: LoadedConfig, db: Database) -> None:
    sup = supervisor(base, db, ON)
    sup.step()
    assert not sup.running and sup.status.state == "stopped" and FakeScheduler.ticks == 0


def test_both_switches_start_and_either_stops(base: LoadedConfig, db: Database) -> None:
    environ = dict(ON)
    sup = supervisor(base, db, environ)
    want(db, "running")
    sup.step()
    assert sup.running and sup.status.state == "running" and FakeScheduler.ticks == 1
    assert sup.status.mode == "shadow"
    sup.step()  # not due yet
    assert FakeScheduler.ticks == 1
    want(db, "stopped")
    sup.step()
    assert not sup.running and sup.status.state == "stopped"
    want(db, "running")
    sup._retry_at = None
    sup.step()
    assert sup.running
    environ["AQ_SCHEDULER_ENABLED"] = "false"  # deployment turns the gate off
    sup.step()
    assert not sup.running and sup.status.state == "blocked"


def test_single_instance_lock_blocks_a_second_scheduler(base: LoadedConfig, db: Database) -> None:
    holder = AdvisoryLock(db, f"aq-scheduler-{base.settings.app.environment.value}")
    assert holder.acquire(wait_seconds=0)
    try:
        want(db, "running")
        sup = supervisor(base, db, ON)
        sup.step()
        assert not sup.running and sup.status.state == "blocked"
        assert "single-instance lock" in sup.status.detail
    finally:
        holder.release()


def test_start_failure_is_reported_and_retried_later(base: LoadedConfig, db: Database) -> None:
    def refuse(*_a: object) -> Any:
        raise SafetyViolation("no human-promoted strategy is eligible for shadow mode")

    want(db, "running")
    sup = SchedulerSupervisor(base, Secrets(), FrozenClock(NOW), db, ON, refuse)
    sup.step()
    assert not sup.running and "eligible" in sup.status.detail
    assert sup._retry_at is not None and sup._retry_at > NOW


def test_live_mode_is_refused_before_building(
    base: LoadedConfig, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Live:
        uses_real_money = True
        value = "live"

    from adaptive_quant.services.runtime import effective_config as real

    def live_config(b: LoadedConfig, d: Database) -> Any:
        cfg, row = real(b, d)
        trading = cfg.settings.trading.model_copy(update={"mode": Live()})
        settings = cfg.settings.model_copy(update={"trading": trading})
        return type(cfg)(**{**vars(cfg), "settings": settings}), row

    monkeypatch.setattr(sched_mod, "effective_config", live_config)
    want(db, "running")
    sup = supervisor(base, db, ON)
    sup.step()
    assert not sup.running and sup.built == []  # type: ignore[attr-defined]
    assert "live" in sup.status.detail


# ------------------------------------------------------------------ runtime PAPER mode
def paper_ready(db: Database, *, verified: bool = True) -> None:
    from adaptive_quant.control.readiness import PREFLIGHT_CHECKS, SWITCH_CHECKS
    from adaptive_quant.control.state import BROKER_KEY

    repo = ControlRepository(db)
    repo.save_runtime_config(
        expected_revision=0,
        overlay={"trading.mode": "paper"},
        strategy_overrides={},
        actor="op",
        now=NOW,
        changes=[],
        version_before="a",
        version_after="b",
    )
    if verified:
        checks = [{"name": n, "passed": True} for n in {*SWITCH_CHECKS, *PREFLIGHT_CHECKS}]
        repo.set_state(
            BROKER_KEY,
            {
                "ok": True,
                "is_paper": True,
                "endpoint_host": "paper-api.alpaca.markets",
                "verified_at": NOW.isoformat(),
                "checks": checks,
            },
            "worker",
            NOW,
        )
    repo.set_state(SCHEDULER_KEY, {"desired": "running", "mode": "paper"}, "op", NOW)


def test_worker_runs_in_the_persisted_paper_mode(base: LoadedConfig, db: Database) -> None:
    paper_ready(db)
    sup = supervisor(base, db, ON)
    sup.step()
    assert sup.running and sup.status.mode == "paper"
    assert sup.built == ["paper"]  # type: ignore[attr-defined]
    guard: Any = sup._scheduler.deps.transmit_guard  # type: ignore[union-attr]
    assert guard() is None
    sup.stop("end of test")


def test_paper_automation_refused_without_verification(base: LoadedConfig, db: Database) -> None:
    paper_ready(db, verified=False)
    sup = supervisor(base, db, ON)
    sup.step()
    assert not sup.running and "verification" in sup.status.detail
    assert sup.built == []  # type: ignore[attr-defined]


def test_paper_automation_refused_with_kill_switch_engaged(
    base: LoadedConfig, db: Database
) -> None:
    paper_ready(db)
    FakeDeps.engaged = True
    sup = supervisor(base, db, ON)
    sup.step()
    assert not sup.running and "kill switch is engaged" in sup.status.detail


def test_start_requested_for_another_mode_is_refused(base: LoadedConfig, db: Database) -> None:
    paper_ready(db)
    ControlRepository(db).set_state(
        SCHEDULER_KEY, {"desired": "running", "mode": "shadow"}, "op", NOW
    )
    sup = supervisor(base, db, ON)
    sup.step()
    assert not sup.running and "requested for shadow" in sup.status.detail


def test_switch_to_shadow_stops_the_running_paper_scheduler(
    base: LoadedConfig, db: Database
) -> None:
    paper_ready(db)
    sup = supervisor(base, db, ON)
    sup.step()
    assert sup.running
    guard: Any = sup._scheduler.deps.transmit_guard  # type: ignore[union-attr]
    repo = ControlRepository(db)
    repo.save_runtime_config(
        expected_revision=1,
        overlay={"trading.mode": "shadow"},
        strategy_overrides={},
        actor="op",
        now=NOW,
        changes=[],
        version_before="b",
        version_after="c",
    )
    reason = guard()
    assert reason is not None and "shadow" in reason  # mid-step: transmission refused
    sup.step()
    assert not sup.running and "mode changed to shadow" in sup.status.detail


def test_guard_refuses_after_the_master_gate_closes(base: LoadedConfig, db: Database) -> None:
    from adaptive_quant.worker.scheduler import make_transmit_guard

    paper_ready(db)
    environ = dict(ON)
    guard = make_transmit_guard(base, db, environ, "paper")
    assert guard() is None
    environ["AQ_SCHEDULER_ENABLED"] = "false"
    assert guard() == "the scheduler master gate is off"
