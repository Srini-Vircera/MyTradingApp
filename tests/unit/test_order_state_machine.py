import itertools

import pytest

from adaptive_quant.core.enums import OrderState as S
from adaptive_quant.core.errors import OrderStateError
from adaptive_quant.trading.orders.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    assert_transition,
    can_transition,
    is_terminal,
    may_change_exposure,
)

HAPPY_PATH = [S.CREATED, S.VALIDATED, S.SUBMITTED, S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED]


def test_every_state_has_a_rule() -> None:
    assert set(TRANSITIONS) == set(S)


def test_happy_path() -> None:
    for a, b in itertools.pairwise(HAPPY_PATH):
        assert_transition(a, b)


def test_unknown_can_never_be_resubmitted() -> None:
    with pytest.raises(OrderStateError, match="never resubmit"):
        assert_transition(S.UNKNOWN, S.SUBMITTED)
    assert not can_transition(S.UNKNOWN, S.VALIDATED)
    assert not can_transition(S.UNKNOWN, S.CREATED)


@pytest.mark.parametrize(
    "outcome", [S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED]
)
def test_unknown_resolves_to_broker_confirmed_state(outcome: S) -> None:
    assert can_transition(S.UNKNOWN, outcome)


def test_timeouts_lead_to_unknown() -> None:
    for state in (S.SUBMITTED, S.ACKNOWLEDGED, S.PARTIALLY_FILLED):
        assert can_transition(state, S.UNKNOWN)


def test_terminal_states_are_final() -> None:
    assert {S.FILLED, S.CANCELLED, S.REJECTED} == TERMINAL_STATES
    for terminal, other in itertools.product(TERMINAL_STATES, S):
        if other != terminal:
            assert not can_transition(terminal, other), (terminal, other)
        assert is_terminal(terminal)


def test_repeated_events_are_idempotent() -> None:
    for state in S:
        assert can_transition(state, state)


def test_no_skipping_validation() -> None:
    assert not can_transition(S.CREATED, S.SUBMITTED)


def test_partial_fill_cannot_regress() -> None:
    assert not can_transition(S.PARTIALLY_FILLED, S.ACKNOWLEDGED)
    assert not can_transition(S.PARTIALLY_FILLED, S.REJECTED)


def test_exposure_pending_includes_unknown() -> None:
    assert may_change_exposure(S.UNKNOWN)
    assert may_change_exposure(S.PARTIALLY_FILLED)
    assert not may_change_exposure(S.CREATED)
    assert not may_change_exposure(S.FILLED)
