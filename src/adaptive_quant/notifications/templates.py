"""Message templates: one subject/body per event type.

Templates use ``str.format`` fields taken from the notification context. A
missing field never suppresses an alert: it is rendered as ``<missing: name>``.
Every message states the environment and mode, and every trading message says
whether real money is involved (it never is in paper/shadow).
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from adaptive_quant.notifications.base import EventType, Notification


@dataclass(frozen=True)
class Template:
    subject: str
    body: str


FOOTER = (
    "\n\nEnvironment: {environment} | mode: {mode} | config: {config_version}\n"
    "Automated message from Adaptive Quant. Historical and simulated results are hypothetical."
)

TEMPLATES: dict[EventType, Template] = {
    EventType.ORDER_PLACED: Template(
        "[{mode}] Order placed: {side} {quantity} {symbol}",
        "Order {client_order_id} ({side} {quantity} {symbol}) was transmitted; state {state}.\n"
        "Risk decision: {decision_id}. Risk-increasing: {risk_increasing}.",
    ),
    EventType.ORDER_REJECTED: Template(
        "[{mode}] ORDER REJECTED: {side} {quantity} {symbol}",
        "Order {client_order_id} was rejected by the broker: {reason}\nIt will NOT be retried automatically.",
    ),
    EventType.PARTIAL_FILL: Template(
        "[{mode}] Partial fill: {symbol} {filled}/{quantity}",
        "Order {client_order_id} is partially filled ({filled} of {quantity}). Remaining quantity is still working.",
    ),
    EventType.RECONCILIATION_FAILURE: Template(
        "[{mode}] RECONCILIATION FAILED ({cycle_id})",
        "Broker state does not match the platform's records:\n{differences}\n\n"
        "Risk-increasing trading is BLOCKED until a person acknowledges this "
        "(`aq trade ack-reconciliation`).",
    ),
    EventType.BROKER_DISCONNECTED: Template(
        "[{mode}] Broker unavailable",
        "The broker could not be reached during {step}: {reason}\nNo orders were sent in this step.",
    ),
    EventType.MARKET_DATA_STALE: Template(
        "[{mode}] Market data problem - trading refused",
        "Market data check failed: {reason}\nThe cycle {cycle_id} placed no orders.",
    ),
    EventType.STRATEGY_FAILURE: Template(
        "[{mode}] Strategy failure - trading refused",
        "One or more strategies failed or were not ready:\n{reason}",
    ),
    EventType.DAILY_LOSS_THRESHOLD: Template(
        "[{mode}] DAILY LOSS LIMIT reached ({daily_loss})",
        "Today's loss {daily_loss} reached the limit {limit}. Risk-increasing orders are blocked.",
    ),
    EventType.DRAWDOWN_THRESHOLD: Template(
        "[{mode}] Drawdown band: {band} ({drawdown})",
        "Drawdown from peak is {drawdown}; the risk engine is in band '{band}'. {detail}",
    ),
    EventType.KILL_SWITCH_ACTIVATED: Template(
        "[{mode}] KILL SWITCH ENGAGED",
        "The kill switch is engaged ({reason}). Risk-increasing orders are blocked until a person releases it.",
    ),
    EventType.SYSTEM_STARTUP: Template(
        "[{mode}] Adaptive Quant started",
        "The trading scheduler started at {time}. Next step: {next_step}.",
    ),
    EventType.SYSTEM_SHUTDOWN: Template(
        "[{mode}] Adaptive Quant stopped",
        "The trading scheduler stopped at {time}: {reason}",
    ),
    EventType.END_OF_DAY_SUMMARY: Template(
        "[{mode}] End of day {session_date}: equity {equity} ({daily_return})",
        "{summary}",
    ),
    EventType.TRADING_REFUSED: Template(
        "[{mode}] Trading refused ({cycle_id})",
        "The pre-trade gate refused trading in step {step}:\n{reason}",
    ),
}


class _Lenient(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return f"<missing: {key}>"


def _fill(text: str, ctx: Mapping[str, Any]) -> str:
    return string.Formatter().vformat(text, (), _Lenient(ctx))


def render(n: Notification) -> tuple[str, str]:
    """(subject, body) for a notification; falls back to its own title/body."""
    t = TEMPLATES.get(n.event)
    ctx = {"mode": "?", "environment": "?", "config_version": "?", **n.context}
    if t is None:
        return n.title, n.body
    subject = _fill(t.subject, ctx)
    body = _fill(t.body, ctx)
    if n.body:
        body = f"{body}\n\n{n.body}"
    return subject, body + _fill(FOOTER, ctx)
