"""``aq trade`` - the scheduled trading cycle (paper and shadow modes only).

    aq --env paper trade status                 today's schedule and cycle state
    aq --env paper trade run [--once]           run the scheduler (--once: due steps, then exit)
    aq --env paper trade ack-reconciliation --actor NAME --reason TEXT

Refuses to start unless: the mode is paper or shadow, strategies have been
promoted by a human to at least ``paper``, and the database and broker
credentials are configured. Live trading is not available.
"""

from __future__ import annotations

import argparse
import signal

from adaptive_quant.cli_db import open_database
from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.schema import Settings
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import MARKET_TZ, Clock
from adaptive_quant.core.enums import TradingMode
from adaptive_quant.core.errors import ConfigurationError, SafetyViolation
from adaptive_quant.notifications.base import NotificationRouter, Notifier
from adaptive_quant.notifications.email_channel import EmailNotifier
from adaptive_quant.persistence.locks import AdvisoryLock
from adaptive_quant.persistence.repositories import CycleRepository
from adaptive_quant.quant.data.calendar import TradingCalendar, nyse_calendar
from adaptive_quant.quant.data.factory import build_validator
from adaptive_quant.quant.strategies.catalog import StrategyCatalog
from adaptive_quant.trading.brokers.factory import build_broker
from adaptive_quant.trading.reconciliation.reconciler import acknowledge
from adaptive_quant.trading.safety.kill_switch_store import build_kill_switch
from adaptive_quant.trading.scheduler.cycle import CycleDeps
from adaptive_quant.trading.scheduler.data import StoreCycleData
from adaptive_quant.trading.scheduler.runner import Scheduler
from adaptive_quant.trading.scheduler.schedule import plan_at, validate_schedule

EXIT_OK = 0
SINGLE_INSTANCE_WAIT_SECONDS = 120.0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    tr = sub.add_parser("trade", help="scheduled trading cycle (paper / shadow)")
    t = tr.add_subparsers(dest="action", required=True)
    t.add_parser("status", help="today's schedule and cycle state")
    run = t.add_parser("run", help="run the market-aware scheduler")
    run.add_argument(
        "--once", action="store_true", help="run the steps that are due now, then exit"
    )
    ack = t.add_parser("ack-reconciliation", help="a person accepts the last failed reconciliation")
    ack.add_argument("--actor", required=True)
    ack.add_argument("--reason", required=True)


def run(args: argparse.Namespace, loaded: LoadedConfig, secrets: Secrets, clock: Clock) -> int:
    s = loaded.settings
    validate_schedule(s.schedule)
    calendar = nyse_calendar()
    if args.action == "status":
        return _status(loaded, secrets, clock)
    if args.action == "ack-reconciliation":
        db = open_database(loaded, secrets)
        try:
            cycles = CycleRepository(db)
            last = cycles.latest_reconciliation()
            acknowledge(cycles, last.cycle_id if last else "", args.actor, args.reason, clock.now())
        finally:
            db.dispose()
        print(f"reconciliation failure acknowledged by {args.actor}")
        return EXIT_OK
    deps = build_deps(loaded, secrets, clock)
    # Never two schedulers at once (e.g. an overlapping redeploy): wait briefly for
    # a previous instance to shut down, then refuse.
    lock = AdvisoryLock(deps.db, f"aq-scheduler-{s.app.environment.value}")
    if not lock.acquire(wait_seconds=SINGLE_INSTANCE_WAIT_SECONDS):
        raise SafetyViolation(
            "another scheduler instance holds the single-instance lock",
            hint="only one worker may run the trading cycle; check for a duplicate "
            "service or replica (numReplicas must be 1)",
        )
    try:
        return _run_scheduler(args, Scheduler(deps), clock, calendar, s)
    finally:
        lock.release()


def _run_scheduler(
    args: argparse.Namespace,
    scheduler: Scheduler,
    clock: Clock,
    calendar: TradingCalendar,
    s: Settings,
) -> int:
    if args.once:
        ran = scheduler.tick()
        for step, status in ran.items():
            print(f"{step:<20} {status}")
        if not ran:
            print(
                "nothing due" if plan_at(clock.now(), calendar, s.schedule) else "no session today"
            )
        return EXIT_OK
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    print(
        f"scheduler running in {s.trading.mode} mode; "
        f"next wake {scheduler.next_wake().astimezone(MARKET_TZ)}"
    )
    scheduler.run(lambda: stop["flag"])
    return EXIT_OK


def build_deps(loaded: LoadedConfig, secrets: Secrets, clock: Clock) -> CycleDeps:
    s = loaded.settings
    mode = s.trading.mode
    if mode not in (TradingMode.PAPER, TradingMode.SHADOW):
        raise SafetyViolation(
            f"the trading scheduler runs in paper or shadow mode only (configured: {mode})",
            hint="set trading.mode to paper or shadow; "
            "live trading is not available in this version",
        )
    catalog = StrategyCatalog.from_config(s.strategies)
    strategies = catalog.eligible(mode)
    if not strategies:
        raise ConfigurationError(
            f"no strategy is eligible for {mode} mode",
            hint="a person must promote strategies to lifecycle 'paper' (or later) in "
            "strategies.yaml after reviewing the research evidence; software never does this",
        )
    calendar = nyse_calendar()
    ids = {x.strategy_id for x in strategies}
    symbols = sorted({*s.universe.tradeable_symbols, *(x.signal_symbol for x in strategies)})
    db = open_database(loaded, secrets)
    return CycleDeps(
        settings=s,
        config_version=loaded.config_version,
        config_record_source=loaded,
        calendar=calendar,
        clock=clock,
        db=db,
        broker=build_broker(loaded, secrets, clock),
        data=StoreCycleData(loaded, secrets, clock, calendar, symbols),
        strategies=strategies,
        versions={
            e.version.strategy_id: e.version
            for e in catalog.entries
            if e.version.strategy_id in ids
        },
        kill_switch=build_kill_switch(loaded, clock, db),
        validator=build_validator(loaded, calendar),
        notifier=_notifier(loaded, secrets),
    )


def _notifier(loaded: LoadedConfig, secrets: Secrets) -> NotificationRouter:
    n = loaded.settings.notifications
    channels: list[Notifier] = []
    if n.email.enabled:
        channels.append(EmailNotifier(n.email, secrets.smtp_username, secrets.smtp_password))
    return NotificationRouter(channels, n.min_severity)


def _status(loaded: LoadedConfig, secrets: Secrets, clock: Clock) -> int:
    s = loaded.settings
    now = clock.now()
    plan = plan_at(now, nyse_calendar(), s.schedule)
    print(f"mode {s.trading.mode}; now {now.astimezone(MARKET_TZ):%Y-%m-%d %H:%M %Z}")
    if plan is None:
        print("no trading session today")
        return EXIT_OK
    early = " (EARLY CLOSE)" if plan.session.early_close else ""
    print(f"session {plan.date}, close {plan.session.close.astimezone(MARKET_TZ):%H:%M}{early}")
    steps: dict[str, str] = {}
    if secrets.database_url is not None:
        db = open_database(loaded, secrets)
        try:
            cycle_id = f"cyc-{s.app.environment.value}-{plan.date}"
            steps = {k: v.status for k, v in CycleRepository(db).steps(cycle_id).items()}
        finally:
            db.dispose()
    for step in plan.steps:
        print(
            f"  {step.at.astimezone(MARKET_TZ):%H:%M}  {step.name:<20} {steps.get(step.name, '-')}"
        )
    print(f"  no new orders from {plan.order_cutoff.astimezone(MARKET_TZ):%H:%M}")
    return EXIT_OK
