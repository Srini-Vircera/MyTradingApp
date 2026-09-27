"""Control-state keys, confirmation phrases and the scheduler master gate.

Two independent switches must *both* allow the scheduler to run:

1. the deployment-level master gate ``AQ_SCHEDULER_ENABLED`` (environment of the
   worker service; the UI cannot change it), and
2. the audited operator switch in PostgreSQL (``control_state['scheduler']``),
   which defaults to *stopped*.

The kill switch is a third, independent mechanism checked by every trading
cycle. None of these can enable live trading.
"""

from __future__ import annotations

from collections.abc import Mapping

SCHEDULER_KEY = "scheduler"
BROKER_KEY = "broker_verification"
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# typed confirmation phrases (the API checks them exactly)
START_SHADOW_CONFIRM = "START SHADOW TRADING"
START_PAPER_CONFIRM = "START PAPER TRADING"
PAPER_MODE_CONFIRM = "ENABLE PAPER TRADING"
SHADOW_MODE_CONFIRM = "USE SHADOW MODE"
RISK_CONFIRM = "TIGHTEN RISK LIMITS"
PROMOTION_CONFIRM = "APPROVE PROMOTION"

#: how old a successful broker verification may be when enabling paper trading
BROKER_VERIFICATION_MAX_AGE_MINUTES = 60


def master_gate(environ: Mapping[str, str]) -> bool:
    """The deployment-level master gate (``AQ_SCHEDULER_ENABLED``); default off."""
    return environ.get("AQ_SCHEDULER_ENABLED", "").strip().lower() in TRUE_VALUES


def desired_running(state: Mapping[str, object] | None) -> bool:
    """The operator switch; anything but an explicit ``running`` means stopped."""
    if not state:
        return False
    value = state.get("value")
    return isinstance(value, Mapping) and value.get("desired") == "running"
