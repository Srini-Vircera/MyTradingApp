"""Order lifecycle state machine.

::

    CREATED -> VALIDATED -> SUBMITTED -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED
       |           |            |              |                 |
       v           v            v              v                 v
    REJECTED   CANCELLED   REJECTED/UNKNOWN  CANCELLED/REJECTED  CANCELLED/UNKNOWN

Key safety property: **UNKNOWN never leads back to SUBMITTED.** An order whose
fate is unknown (timeout, disconnect, restart mid-submit) must be *investigated*
by querying the broker with its ``client_order_id``; it may then resolve to any
broker-confirmed state, but it is never blindly resubmitted.

Repeating the current state is always allowed (idempotent replays of broker
events), which matters because brokers can deliver the same update twice.
"""

from __future__ import annotations

from types import MappingProxyType

from adaptive_quant.core.enums import OrderState
from adaptive_quant.core.errors import OrderStateError

S = OrderState

_BROKER_OUTCOMES = frozenset(
    {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED}
)

TRANSITIONS: MappingProxyType[OrderState, frozenset[OrderState]] = MappingProxyType(
    {
        S.CREATED: frozenset({S.VALIDATED, S.REJECTED}),
        S.VALIDATED: frozenset({S.SUBMITTED, S.CANCELLED}),
        S.SUBMITTED: _BROKER_OUTCOMES | {S.UNKNOWN},
        S.ACKNOWLEDGED: frozenset(
            {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.UNKNOWN}
        ),
        S.PARTIALLY_FILLED: frozenset({S.FILLED, S.CANCELLED, S.UNKNOWN}),
        S.UNKNOWN: _BROKER_OUTCOMES,
        S.FILLED: frozenset(),
        S.CANCELLED: frozenset(),
        S.REJECTED: frozenset(),
    }
)

TERMINAL_STATES = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)

#: States in which the order may still change our exposure. UNKNOWN counts:
#: an order we cannot confirm must be assumed live.
EXPOSURE_PENDING_STATES = frozenset({S.SUBMITTED, S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.UNKNOWN})


def can_transition(current: OrderState, target: OrderState) -> bool:
    return current == target or target in TRANSITIONS[current]


def assert_transition(current: OrderState, target: OrderState) -> None:
    if not can_transition(current, target):
        hint = None
        if current is S.UNKNOWN:
            hint = "query the broker by client_order_id to learn the real state; never resubmit"
        raise OrderStateError(f"illegal order transition {current} -> {target}", hint=hint)


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL_STATES


def may_change_exposure(state: OrderState) -> bool:
    return state in EXPOSURE_PENDING_STATES
