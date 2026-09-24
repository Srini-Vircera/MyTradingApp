from datetime import UTC, datetime

import pytest

from adaptive_quant.core.enums import StrategyLifecycle as L
from adaptive_quant.core.errors import GovernanceError
from adaptive_quant.governance.lifecycle import Actor, ActorKind, transition

NOW = datetime(2024, 1, 2, tzinfo=UTC)
HUMAN = Actor("srini", ActorKind.HUMAN)
SYSTEM = Actor("validation-pipeline", ActorKind.SYSTEM)
GOOD_REASON = "walk-forward OOS Sharpe stable across 12 folds; reviewed report #42"


def move(current: L, target: L, actor: Actor, reason: str = GOOD_REASON) -> None:
    transition(
        strategy_id="s1",
        strategy_version="1.0.0",
        current=current,
        target=target,
        actor=actor,
        reason=reason,
        at=NOW,
    )


def test_system_can_never_promote_to_live_approved() -> None:
    with pytest.raises(GovernanceError, match="automated promotion"):
        move(L.SHADOW, L.LIVE_APPROVED, SYSTEM)


@pytest.mark.parametrize(
    ("a", "b"), [(L.VALIDATED, L.PAPER), (L.PAPER, L.SHADOW), (L.DISABLED, L.RESEARCH)]
)
def test_system_cannot_promote_beyond_validated(a: L, b: L) -> None:
    with pytest.raises(GovernanceError, match="automated promotion"):
        move(a, b, SYSTEM)


def test_system_may_validate_research() -> None:
    move(L.RESEARCH, L.VALIDATED, SYSTEM)


@pytest.mark.parametrize("state", [L.RESEARCH, L.VALIDATED, L.PAPER, L.SHADOW, L.LIVE_APPROVED])
def test_system_may_always_disable(state: L) -> None:
    move(state, L.DISABLED, SYSTEM, reason="drawdown breach")


def test_human_can_approve_live_with_justification() -> None:
    record = transition(
        strategy_id="s1",
        strategy_version="1.0.0",
        current=L.SHADOW,
        target=L.LIVE_APPROVED,
        actor=HUMAN,
        reason=GOOD_REASON,
        at=NOW,
    )
    assert record.to_state is L.LIVE_APPROVED
    assert record.actor == HUMAN


def test_human_promotion_requires_justification() -> None:
    with pytest.raises(GovernanceError, match="justification"):
        move(L.SHADOW, L.LIVE_APPROVED, HUMAN, reason="looks good")


def test_cannot_skip_stages() -> None:
    with pytest.raises(GovernanceError, match="not allowed"):
        move(L.RESEARCH, L.LIVE_APPROVED, HUMAN)
    with pytest.raises(GovernanceError, match="not allowed"):
        move(L.VALIDATED, L.SHADOW, HUMAN)


def test_reason_required() -> None:
    with pytest.raises(GovernanceError, match="reason"):
        move(L.PAPER, L.DISABLED, HUMAN, reason="   ")


def test_actor_required() -> None:
    with pytest.raises(GovernanceError, match="actor"):
        move(L.PAPER, L.DISABLED, Actor(" ", ActorKind.HUMAN), reason="x")
