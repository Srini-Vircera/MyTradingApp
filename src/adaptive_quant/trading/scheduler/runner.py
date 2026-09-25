"""The scheduler loop: wake at each due step, run the session's cycle, sleep.

Restart-safe by construction: every tick re-reads the cycle's persisted steps,
so a restarted process continues the same cycle where it stopped.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from adaptive_quant.core.errors import DatabaseUnavailableError
from adaptive_quant.notifications.base import EventType, Notification
from adaptive_quant.observability.logging import get_logger
from adaptive_quant.persistence.repositories import CycleRepository
from adaptive_quant.trading.scheduler.cycle import FINISHED, CycleDeps, cycle_for
from adaptive_quant.trading.scheduler.schedule import next_step_time

_log = get_logger(__name__)


class Scheduler:
    def __init__(
        self,
        deps: CycleDeps,
        sleep: Callable[[float], None] = time.sleep,
        max_sleep_seconds: float = 300.0,
    ) -> None:
        self.deps = deps
        self._sleep = sleep
        self._max_sleep = max_sleep_seconds

    def tick(self) -> dict[str, str]:
        """Run whatever is due now. Weekends/holidays: nothing."""
        now = self.deps.clock.now()
        cycle = cycle_for(self.deps, now)
        if cycle is None:
            return {}
        try:
            return cycle.run_due()
        except DatabaseUnavailableError as exc:
            _log.error("cycle_database_unavailable", error=exc.message)
            self._notify(
                EventType.TRADING_REFUSED,
                step="scheduler",
                reason=f"database unavailable: {exc.message}",
            )
            return {"database": "unavailable"}

    def next_wake(self) -> datetime:
        now = self.deps.clock.now()
        cycle = cycle_for(self.deps, now)
        done: set[str] = set()
        if cycle is not None:
            try:
                steps = CycleRepository(self.deps.db).steps(cycle.cycle_id)
                done = {k for k, v in steps.items() if v.status in FINISHED}
            except DatabaseUnavailableError:
                done = set()
        return next_step_time(now, self.deps.calendar, self.deps.settings.schedule, done)

    def run(self, should_stop: Callable[[], bool]) -> None:
        self._notify(EventType.SYSTEM_STARTUP, next_step=self.next_wake().isoformat())
        reason = "stop requested"
        try:
            while not should_stop():
                self.tick()
                wait = (self.next_wake() - self.deps.clock.now()).total_seconds()
                self._sleep(min(max(wait, 1.0), self._max_sleep))
        except KeyboardInterrupt:
            reason = "interrupted"
        finally:
            self._notify(EventType.SYSTEM_SHUTDOWN, reason=reason)

    def _notify(self, event: EventType, **ctx: object) -> None:
        if self.deps.notifier is None:
            return
        now = self.deps.clock.now()
        base = {
            "mode": self.deps.settings.trading.mode.value,
            "environment": self.deps.settings.app.environment.value,
            "config_version": self.deps.config_version,
            "time": now.isoformat(),
        }
        self.deps.notifier.publish(
            Notification(event, event.value, "", now, context={**base, **ctx})
        )
