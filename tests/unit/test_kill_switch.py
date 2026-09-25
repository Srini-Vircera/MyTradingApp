import json
from datetime import timedelta
from pathlib import Path

import pytest

from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.errors import SafetyViolation
from adaptive_quant.trading.safety.kill_switch import (
    RELEASE_CONFIRMATION,
    FileKillSwitchStore,
    KillSwitch,
)


@pytest.fixture
def store(tmp_path: Path) -> FileKillSwitchStore:
    return FileKillSwitchStore(tmp_path / "ks.json", tmp_path / "ks_audit.jsonl")


@pytest.fixture
def switch(store: FileKillSwitchStore, clock: FrozenClock) -> KillSwitch:
    return KillSwitch(store, clock)


def release(ks: KillSwitch) -> None:
    ks.release(actor="srini", reason="initial setup complete", confirmation=RELEASE_CONFIRMATION)


def test_fresh_install_is_engaged(switch: KillSwitch) -> None:
    status = switch.status()
    assert status.engaged and status.fail_safe
    assert "never been released" in status.reason


def test_release_then_engage(switch: KillSwitch, clock: FrozenClock) -> None:
    release(switch)
    assert not switch.is_engaged()
    clock.advance(timedelta(minutes=1))
    switch.engage(actor="srini", reason="unexpected position")
    status = switch.status()
    assert status.engaged and not status.fail_safe
    assert status.reason == "unexpected position"
    assert status.changed_at == clock.now()


def test_release_requires_exact_confirmation(switch: KillSwitch) -> None:
    with pytest.raises(SafetyViolation, match="not confirmed"):
        switch.release(actor="srini", reason="ok", confirmation="re-enable trading")
    assert switch.is_engaged()


def test_system_actor_cannot_release(switch: KillSwitch) -> None:
    with pytest.raises(SafetyViolation, match="never release"):
        switch.release(actor="system:scheduler", reason="auto", confirmation=RELEASE_CONFIRMATION)


def test_system_actor_can_engage(switch: KillSwitch) -> None:
    release(switch)
    switch.engage(actor="system:risk-engine", reason="daily loss limit")
    assert switch.is_engaged()


@pytest.mark.parametrize(("actor", "reason"), [("", "x"), ("srini", " ")])
def test_blank_actor_or_reason_rejected(switch: KillSwitch, actor: str, reason: str) -> None:
    with pytest.raises(SafetyViolation, match="must not be empty"):
        switch.engage(actor=actor, reason=reason)


def test_corrupt_state_fails_closed(switch: KillSwitch, store: FileKillSwitchStore) -> None:
    release(switch)
    store.state_file.write_text("{not json")
    status = switch.status()
    assert status.engaged and status.fail_safe
    assert "unreadable" in status.reason


def test_deleted_state_fails_closed(switch: KillSwitch, store: FileKillSwitchStore) -> None:
    release(switch)
    store.state_file.unlink()
    assert switch.is_engaged()


def test_every_change_is_audited(switch: KillSwitch, store: FileKillSwitchStore) -> None:
    release(switch)
    switch.engage(actor="srini", reason="test")
    lines = [json.loads(line) for line in store.audit_file.read_text().splitlines()]
    assert [r["action"] for r in lines] == ["release", "engage"]
    assert lines[1]["actor"] == "srini"


def test_engage_never_deletes_history(switch: KillSwitch, store: FileKillSwitchStore) -> None:
    for i in range(3):
        switch.engage(actor="srini", reason=f"r{i}")
    assert len(store.audit_file.read_text().splitlines()) == 3


def test_atomic_write_leaves_no_temp_files(switch: KillSwitch, store: FileKillSwitchStore) -> None:
    release(switch)
    leftovers = [p for p in store.state_file.parent.iterdir() if p.name.startswith(".killswitch-")]
    assert leftovers == []
