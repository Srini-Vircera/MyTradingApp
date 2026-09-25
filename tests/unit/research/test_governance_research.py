"""Automated lifecycle changes stop at VALIDATED; the ledger is append-only evidence."""

from datetime import UTC, datetime
from itertools import product
from pathlib import Path

import pytest

from adaptive_quant.core.enums import StrategyLifecycle as L
from adaptive_quant.governance.lifecycle import ActorKind
from adaptive_quant.governance.research import (
    GovernanceLedger,
    ResearchRecord,
    proposed_transition,
)

NOW = datetime(2024, 1, 2, tzinfo=UTC)


def record(passed: bool, sid: str = "s", version: str = "s@1#a") -> ResearchRecord:
    return ResearchRecord(
        strategy_id=sid,
        version_id=version,
        run_id="run1",
        recorded_at=NOW.isoformat(),
        data_fingerprint="d",
        config_version="c",
        gates={"deflated_sharpe": True, "pbo": passed},
        all_gates_passed=passed,
        score=0.5,
        rank=1,
        statistics={"oos_sharpe": 1.0},
        report="r.html",
    )


def test_promotes_research_to_validated_only_when_every_gate_passes() -> None:
    t = proposed_transition(record(True), L.RESEARCH, NOW)
    assert t is not None
    assert (t.from_state, t.to_state) == (L.RESEARCH, L.VALIDATED)
    assert t.actor.kind is ActorKind.SYSTEM
    assert proposed_transition(record(False), L.RESEARCH, NOW) is None


def test_inconsistent_record_never_promotes() -> None:
    r = record(True)
    bad = ResearchRecord(**{**r.__dict__, "gates": {"pbo": False}})
    assert proposed_transition(bad, L.RESEARCH, NOW) is None


def test_demotes_validated_when_gates_fail() -> None:
    t = proposed_transition(record(False), L.VALIDATED, NOW)
    assert t is not None and t.to_state is L.RESEARCH
    assert "pbo" in t.reason


@pytest.mark.parametrize(("state", "passed"), list(product(list(L), [True, False])))
def test_software_never_targets_beyond_validated(state: L, passed: bool) -> None:
    t = proposed_transition(record(passed), state, NOW)
    if t is not None:
        assert t.to_state in {L.VALIDATED, L.RESEARCH}
        assert state in {L.RESEARCH, L.VALIDATED}
    if state in {L.PAPER, L.SHADOW, L.LIVE_APPROVED, L.DISABLED}:
        assert t is None


def test_ledger_append_and_validated_versions(tmp_path: Path) -> None:
    led = GovernanceLedger(tmp_path / "g" / "gov.jsonl")
    assert led.entries() == [] and led.validated_versions() == {}
    r = record(True)
    led.append(r, proposed_transition(r, L.RESEARCH, NOW))
    led.append(record(False, sid="other"), None)
    assert led.validated_versions() == {"s": "s@1#a"}
    led.append(record(False), proposed_transition(record(False), L.VALIDATED, NOW))
    assert led.validated_versions() == {}
    assert len(led.entries()) == 3
    assert len((tmp_path / "g" / "gov.jsonl").read_text().splitlines()) == 3
