"""Research evidence attached to strategy versions, and the only automated lifecycle
changes the software may make.

* A :class:`ResearchRecord` ties one strategy version to one research run: its
  gates, score, key statistics and the report location.
* :func:`proposed_transition` may return **only**
  - RESEARCH -> VALIDATED, when *every* gate passed (automated promotion), or
  - VALIDATED -> RESEARCH, when a gate failed (automated demotion).
  Everything else (PAPER, SHADOW, LIVE_APPROVED) needs a named human with a
  written justification (``lifecycle.transition`` enforces this independently).
* Transitions are appended to a JSONL governance ledger. ``strategies.yaml``
  is never edited by software; the lifecycle declared there stays the
  human-controlled source of truth for which strategies may trade.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from adaptive_quant.core.clock import ensure_utc
from adaptive_quant.core.enums import StrategyLifecycle
from adaptive_quant.governance.lifecycle import Actor, ActorKind, LifecycleTransition, transition

SYSTEM_ACTOR = Actor("research-pipeline", ActorKind.SYSTEM)
L = StrategyLifecycle


@dataclass(frozen=True)
class ResearchRecord:
    strategy_id: str
    version_id: str
    run_id: str
    recorded_at: str
    data_fingerprint: str
    config_version: str
    gates: dict[str, bool]
    all_gates_passed: bool
    score: float
    rank: int
    statistics: dict[str, float | None]
    report: str


def proposed_transition(
    record: ResearchRecord, current: StrategyLifecycle, at: datetime
) -> LifecycleTransition | None:
    """The automated lifecycle change supported by this evidence, if any."""
    failed = sorted(k for k, ok in record.gates.items() if not ok)
    if current is L.RESEARCH and record.all_gates_passed and not failed:
        target, reason = L.VALIDATED, f"all research gates passed in run {record.run_id}"
    elif current is L.VALIDATED and failed:
        target, reason = L.RESEARCH, f"gates failed in run {record.run_id}: {', '.join(failed)}"
    else:
        return None
    return transition(
        strategy_id=record.strategy_id,
        strategy_version=record.version_id,
        current=current,
        target=target,
        actor=SYSTEM_ACTOR,
        reason=reason,
        at=at,
    )


@dataclass
class GovernanceLedger:
    """Append-only JSONL of research records and automated lifecycle transitions."""

    path: Path

    def append(self, record: ResearchRecord, change: LifecycleTransition | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "record": asdict(record),
            "transition": None
            if change is None
            else {
                "strategy_id": change.strategy_id,
                "strategy_version": change.strategy_version,
                "from": change.from_state.value,
                "to": change.to_state.value,
                "actor": change.actor.id,
                "actor_kind": change.actor.kind.value,
                "reason": change.reason,
                "at": ensure_utc(change.at).isoformat(),
            },
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def entries(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        return [
            json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()
        ]

    def validated_versions(self) -> dict[str, str]:
        """strategy_id -> version_id whose *latest* automated state is VALIDATED."""
        state: dict[str, tuple[str, str]] = {}
        for e in self.entries():
            t = e.get("transition")
            if isinstance(t, dict):
                state[str(t["strategy_id"])] = (str(t["to"]), str(t["strategy_version"]))
        return {sid: ver for sid, (to, ver) in state.items() if to == L.VALIDATED.value}
