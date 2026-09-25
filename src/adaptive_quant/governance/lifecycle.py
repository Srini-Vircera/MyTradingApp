"""Strategy lifecycle transitions.

::

    RESEARCH -> VALIDATED -> PAPER -> SHADOW -> LIVE_APPROVED
        ^           |          |        |            |
        +-----------+----------+--------+----> DISABLED -> RESEARCH

Rules
-----
* Automated (``SYSTEM``) actors may only promote RESEARCH -> VALIDATED (after the
  validation pipeline passes) and may always *demote* or *disable*.
* Every other promotion - and in particular any move to LIVE_APPROVED - requires
  a ``HUMAN`` actor and a written justification. The software never promotes a
  strategy to LIVE_APPROVED on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from adaptive_quant.core.clock import ensure_utc
from adaptive_quant.core.enums import StrategyLifecycle
from adaptive_quant.core.errors import GovernanceError

L = StrategyLifecycle

ALLOWED: MappingProxyType[StrategyLifecycle, frozenset[StrategyLifecycle]] = MappingProxyType(
    {
        L.RESEARCH: frozenset({L.VALIDATED, L.DISABLED}),
        L.VALIDATED: frozenset({L.PAPER, L.RESEARCH, L.DISABLED}),
        L.PAPER: frozenset({L.SHADOW, L.VALIDATED, L.DISABLED}),
        L.SHADOW: frozenset({L.LIVE_APPROVED, L.PAPER, L.DISABLED}),
        L.LIVE_APPROVED: frozenset({L.SHADOW, L.DISABLED}),
        L.DISABLED: frozenset({L.RESEARCH}),
    }
)

_RANK = {
    L.DISABLED: -1,
    L.RESEARCH: 0,
    L.VALIDATED: 1,
    L.PAPER: 2,
    L.SHADOW: 3,
    L.LIVE_APPROVED: 4,
}

#: The only promotion an automated pipeline may perform.
SYSTEM_PROMOTIONS = frozenset({(L.RESEARCH, L.VALIDATED)})

MIN_JUSTIFICATION_CHARS = 20


class ActorKind(StrEnum):
    HUMAN = "human"
    SYSTEM = "system"


@dataclass(frozen=True)
class Actor:
    id: str
    kind: ActorKind


@dataclass(frozen=True)
class LifecycleTransition:
    strategy_id: str
    strategy_version: str
    from_state: StrategyLifecycle
    to_state: StrategyLifecycle
    actor: Actor
    reason: str
    at: datetime


def is_promotion(current: StrategyLifecycle, target: StrategyLifecycle) -> bool:
    """DISABLED -> RESEARCH counts as a promotion (re-activation)."""
    return _RANK[target] > _RANK[current]


def transition(
    *,
    strategy_id: str,
    strategy_version: str,
    current: StrategyLifecycle,
    target: StrategyLifecycle,
    actor: Actor,
    reason: str,
    at: datetime,
) -> LifecycleTransition:
    """Validate a lifecycle change and return the auditable record of it."""
    if target not in ALLOWED[current]:
        raise GovernanceError(
            f"{strategy_id}: transition {current} -> {target} is not allowed",
            hint=f"allowed from {current}: {sorted(s.value for s in ALLOWED[current])}",
        )
    if not actor.id.strip():
        raise GovernanceError("lifecycle transitions require an identified actor")
    promotion = is_promotion(current, target)
    if promotion and actor.kind is ActorKind.SYSTEM and (current, target) not in SYSTEM_PROMOTIONS:
        raise GovernanceError(
            f"{strategy_id}: automated promotion {current} -> {target} is forbidden",
            hint="a human must review the evidence and approve this promotion",
        )
    too_short = len(reason.strip()) < MIN_JUSTIFICATION_CHARS
    if promotion and actor.kind is ActorKind.HUMAN and too_short:
        raise GovernanceError(
            f"{strategy_id}: promotions require a written justification "
            f"(at least {MIN_JUSTIFICATION_CHARS} characters)"
        )
    if not reason.strip():
        raise GovernanceError("lifecycle transitions require a reason")
    return LifecycleTransition(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        from_state=current,
        to_state=target,
        actor=actor,
        reason=reason.strip(),
        at=ensure_utc(at),
    )
