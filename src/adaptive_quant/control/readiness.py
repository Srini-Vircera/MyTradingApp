"""Shared readiness rules for the runtime trading mode and automation.

Pure functions over data already stored in PostgreSQL (the worker's read-only
paper-account verification, worker heartbeat, dataset inventory, kill-switch
status, strategy catalogue). The API uses them to accept or refuse a request and
to show the operator a checklist; the worker re-checks the critical ones before
it starts the scheduler (defence in depth). Nothing here talks to a broker.

The layers, from the top (a lower layer can never override a higher one):

1. deployment gates - ``aq deploy check`` (never live), ``AQ_SCHEDULER_ENABLED``
   (master gate for automation), the broker factory (Alpaca *paper* only);
2. runtime trading mode - SHADOW or PAPER, stored in PostgreSQL;
3. kill switch - ENGAGED blocks new risk;
4. automation switch - STOPPED / RUNNING;
5. strategy eligibility - human-promoted lifecycle;
6. pre-flight - run by every trading cycle before any order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from adaptive_quant.control import state as ctl

PAPER_HOST = "paper-api.alpaca.markets"

MODE_EXPLANATIONS = {
    "shadow": "Strategies, signals and orders are calculated and recorded, but no orders are "
    "sent to a broker.",
    "paper": "Orders may be submitted only to the verified Alpaca paper-trading account and "
    "use simulated funds.",
}

#: checks of the worker's read-only verification needed to *select* PAPER mode
SWITCH_CHECKS = (
    "provider",
    "paper_endpoint",
    "credentials",
    "authentication",
    "paper_account",
    "broker_connectivity",
    "database",
    "kill_switch_known",
)
#: additional checks needed to *start* paper automation (the read-only pre-flight)
PREFLIGHT_CHECKS = ("broker_connectivity", "database", "market_data", "reconciliation")


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    ok: bool
    detail: str
    fix: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _checks(verification: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    rows = (verification or {}).get("checks") or []
    return {str(c.get("name")): c for c in rows if isinstance(c, Mapping)}


def verification_age(verification: Mapping[str, Any] | None, now: datetime) -> timedelta | None:
    at = (verification or {}).get("verified_at")
    if not at:
        return None
    try:
        return now - datetime.fromisoformat(str(at))
    except ValueError:
        return None


def verification_problems(
    verification: Mapping[str, Any] | None, now: datetime, names: Iterable[str] = SWITCH_CHECKS
) -> list[str]:
    """Why the latest verification does not allow PAPER (empty list = allowed)."""
    if not verification:
        return ["the Alpaca paper account has not been verified yet"]
    age = verification_age(verification, now)
    limit = timedelta(minutes=ctl.BROKER_VERIFICATION_MAX_AGE_MINUTES)
    if age is None or age > limit or age < timedelta(minutes=-5):
        return [
            f"the last verification is older than {ctl.BROKER_VERIFICATION_MAX_AGE_MINUTES} "
            "minutes; verify again"
        ]
    checks = _checks(verification)
    out = []
    for name in names:
        c = checks.get(name)
        if c is None:
            out.append(f"{name}: not checked")
        elif not c.get("passed"):
            out.append(f"{c.get('label') or name}: {c.get('detail') or 'failed'}")
    if verification.get("endpoint_host") != PAPER_HOST:
        out.append("the broker endpoint is not the Alpaca paper endpoint")
    return out


def broker_label(
    credentials: bool | None, verification: Mapping[str, Any] | None, now: datetime
) -> str:
    """ALPACA PAPER / NOT CONFIGURED / UNAVAILABLE (never 'live')."""
    if credentials is False:
        return "NOT CONFIGURED"
    if verification is None:
        return "ALPACA PAPER (not verified)"
    if verification_problems(verification, now):
        checks = _checks(verification)
        if checks and not all(c.get("passed") for n, c in checks.items() if n in SWITCH_CHECKS):
            return "UNAVAILABLE"
        return "ALPACA PAPER (verification expired)"
    return "ALPACA PAPER"


def data_problems(inventory: Iterable[Mapping[str, Any]], symbols: Iterable[str]) -> list[str]:
    """Required symbols must have valid, fresh, real (not synthetic) daily data."""
    rows = {
        str(r.get("symbol")): r
        for r in inventory
        if r.get("adjustment") == "raw" and not r.get("is_synthetic") and r.get("frequency") == "1d"
    }
    out = []
    for sym in sorted(set(symbols)):
        r = rows.get(sym)
        if r is None:
            out.append(f"{sym}: no stored daily data")
        elif not r.get("validation_passed"):
            out.append(f"{sym}: failed validation")
        elif r.get("fresh") is False:
            out.append(f"{sym}: stale ({r.get('freshness')})")
    return out


def start_checklist(
    *,
    mode: str,
    now: datetime,
    worker: Mapping[str, Any] | None,
    desired: Mapping[str, Any] | None,
    kill_switch: Mapping[str, Any],
    eligible: list[str],
    required_symbols: Iterable[str],
    inventory: Iterable[Mapping[str, Any]],
    reconciliation: Mapping[str, Any] | None,
    verification: Mapping[str, Any] | None,
) -> list[Item]:
    """Everything that must hold before automation may start in ``mode``."""
    status = (worker or {}).get("status") or {}
    sched = status.get("scheduler") or {}
    online = bool((worker or {}).get("online"))
    gate = bool(status.get("master_gate"))
    paper = mode == "paper"
    items = [
        Item(
            "master_gate",
            "Scheduler master gate (AQ_SCHEDULER_ENABLED)",
            gate,
            "ON" if gate else "Automation unavailable — deployment scheduler master gate is OFF.",
            ""
            if gate
            else "A deliberate deployment change: set AQ_SCHEDULER_ENABLED=true on the "
            "worker service in Railway. It cannot be changed from the dashboard.",
        ),
        Item(
            "worker",
            "Worker online",
            online,
            "reporting" if online else "no recent worker heartbeat",
            "" if online else "Check that the worker service is deployed and running.",
        ),
        Item(
            "mode",
            f"Trading mode: {mode.upper()}",
            mode in ("shadow", "paper"),
            "SHADOW — no orders sent" if not paper else "PAPER — simulated funds only",
        ),
    ]
    if paper:
        problems = verification_problems(verification, now, (*SWITCH_CHECKS, *PREFLIGHT_CHECKS))
        items.append(
            Item(
                "paper_account",
                "Alpaca paper account verified and read-only pre-flight passed",
                not problems,
                "verified" if not problems else "; ".join(problems),
                ""
                if not problems
                else "Press 'Verify Alpaca paper account' (runs on the worker; "
                "read-only) and resolve what it reports.",
            )
        )
    engaged = bool(kill_switch.get("engaged"))
    items.append(
        Item(
            "kill_switch",
            "Kill switch released",
            not engaged or not paper,
            "ENGAGED" if engaged else "released",
            (
                "Release the kill switch when appropriate (typed confirmation)."
                if engaged and paper
                else "Shadow mode sends no orders; the kill switch still blocks new risk in the "
                "recorded decisions."
                if engaged
                else ""
            ),
        )
    )
    items.append(
        Item(
            "strategies",
            f"At least one strategy approved for {mode.upper()}",
            bool(eligible),
            ", ".join(eligible) if eligible else f"no strategy is eligible for {mode}",
            ""
            if eligible
            else "In the Strategy Manager, a person reviews the evidence and "
            "advances a strategy to 'paper' (justification + confirmation). Selecting a mode "
            "never promotes a strategy.",
        )
    )
    data = data_problems(inventory, required_symbols)
    items.append(
        Item(
            "market_data",
            "Required market data valid and fresh",
            not data,
            "ok" if not data else "; ".join(data),
            ""
            if not data
            else "On the Data page, download/update and validate the listed symbols.",
        )
    )
    recon_ok = reconciliation is None or bool(reconciliation.get("passed"))
    items.append(
        Item(
            "reconciliation",
            "Reconciliation healthy",
            recon_ok,
            "no discrepancy" if recon_ok else "the last reconciliation found differences",
            "" if recon_ok else "Review and acknowledge the reconciliation on the Orders page.",
        )
    )
    running_elsewhere = sched.get("state") == "running" or ctl.desired_running(desired)
    items.append(
        Item(
            "single_scheduler",
            "No scheduler already running (single-instance lock)",
            not running_elsewhere,
            "free" if not running_elsewhere else "automation is already running or requested",
            "" if not running_elsewhere else "Stop the current automation first.",
        )
    )
    return items


def failed(items: Iterable[Item]) -> list[Item]:
    return [i for i in items if not i.ok]
