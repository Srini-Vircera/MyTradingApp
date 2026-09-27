"""``aq worker run`` - the control-plane worker (jobs + scheduler supervision).

Executes jobs queued through the operator API (data, backtests, research,
read-only broker verification) and supervises the trading scheduler, which runs
only while the deployment master gate ``AQ_SCHEDULER_ENABLED`` *and* the audited
operator switch both allow it. ``aq trade run`` remains available as an
emergency/admin fallback.
"""

from __future__ import annotations

import argparse
import os
import signal

from adaptive_quant.cli_db import open_database
from adaptive_quant.cli_trade import build_deps
from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import Clock
from adaptive_quant.quant.data.calendar import nyse_calendar
from adaptive_quant.worker.runner import Worker
from adaptive_quant.worker.scheduler import SchedulerSupervisor

EXIT_OK = 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    w = sub.add_parser("worker", help="control-plane worker (jobs + scheduler supervision)")
    a = w.add_subparsers(dest="action", required=True)
    a.add_parser("run", help="run the worker loop until SIGTERM")


def run(args: argparse.Namespace, loaded: LoadedConfig, secrets: Secrets, clock: Clock) -> int:
    db = open_database(loaded, secrets)
    environ = dict(os.environ)
    supervisor = SchedulerSupervisor(loaded, secrets, clock, db, environ, build_deps)
    worker = Worker(
        loaded, secrets, clock, db, nyse_calendar(), environ=environ, supervisor=supervisor
    )
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    print(f"worker {worker.id} running (config {loaded.config_version}); jobs from PostgreSQL")
    try:
        worker.run(lambda: stop["flag"])
    finally:
        db.dispose()
    return EXIT_OK
