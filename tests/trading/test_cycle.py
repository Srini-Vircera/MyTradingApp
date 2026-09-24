"""The trading cycle end-to-end on real PostgreSQL: schedule, restart, shadow, halts, refusals."""

from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.core.enums import TradingMode
from adaptive_quant.core.models import MarketClock
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.explain import explain_decision
from adaptive_quant.persistence.repositories import CycleRepository
from adaptive_quant.trading.brokers.simulated import SimulatedBroker
from adaptive_quant.trading.scheduler.cycle import TradingCycle, cycle_for
from adaptive_quant.trading.scheduler.runner import Scheduler
from adaptive_quant.trading.scheduler.schedule import plan_for
from tests.trading.cycle_helpers import SpyBroker, build, et

pytestmark = pytest.mark.postgres
TUE = date(2024, 7, 2)  # regular session (16:00 ET)
EARLY = date(2024, 7, 3)  # early close 13:00 ET
HOLIDAY = date(2024, 7, 4)
SAT = date(2024, 7, 6)


def count(db: Database, table: str) -> int:
    with db.engine.connect() as c:
        return int(c.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)


def run_all(deps, clock, day: date) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Advance to every step time of the day and run what is due."""
    plan = plan_for(day, deps.calendar, deps.settings.schedule)
    assert plan is not None
    out: dict[str, str] = {}
    for step in plan.steps:
        clock.set(step.at + timedelta(seconds=5))
        cycle = cycle_for(deps, clock.now())
        assert cycle is not None
        out.update(cycle.run_due())
    return out


def test_full_paper_cycle(db: Database, tmp_path: Path) -> None:
    deps, broker, cap, clock = build(db, tmp_path, TUE)
    ran = run_all(deps, clock, TUE)
    assert list(ran) == [
        "health_check", "market_data_update", "indicators", "strategies", "risk",
        "portfolio_target", "order_submission", "fill_monitoring", "reconciliation",
    ]  # fmt: skip
    assert set(ran.values()) == {"done"}, ran
    cycles = CycleRepository(db)
    cyc = cycles.get("cyc-development-2024-07-02")
    assert cyc is not None and cyc.status == "completed"
    assert broker.submit_calls == count(db, "order_intents") >= 1
    assert "end_of_day_summary" in cap.events() and "order_placed" in cap.events()
    eod = next(n for n in cap.sent if n.event.value == "end_of_day_summary")
    assert "Reconciliation    passed" in eod.context["summary"]
    chain = explain_decision(db, "cyc-development-2024-07-02")
    assert chain["decisions"] and chain["orders"] and chain["preflight"][0]["passed"]
    assert count(db, "daily_performance") == 1
    assert count(db, "notifications_sent") == len(cap.sent)
    # the cycle is finished: running again changes nothing
    clock.advance(timedelta(hours=1))
    assert cycle_for(deps, clock.now()).run_due() == {}  # type: ignore[union-attr]


def test_restart_resumes_the_same_cycle_without_duplicates(db: Database, tmp_path: Path) -> None:
    deps, broker, _, clock = build(db, tmp_path, TUE)
    plan = plan_for(TUE, deps.calendar, deps.settings.schedule)
    assert plan is not None
    clock.set(plan.steps[6].at + timedelta(seconds=5))  # up to and including order submission
    first = TradingCycle(deps, plan).run_due()
    assert first["order_submission"] == "done"
    sent = broker.submit_calls
    # "crash" and restart: a brand-new cycle object (new repositories, new manager)
    deps2, _, _, _ = build(db, tmp_path, TUE)
    deps2.broker, deps2.clock = broker, clock
    clock.set(plan.steps[-1].at + timedelta(seconds=5))
    second = TradingCycle(deps2, plan).run_due()
    assert list(second) == ["fill_monitoring", "reconciliation"]  # finished steps skipped
    assert broker.submit_calls == sent
    assert count(db, "risk_decisions") == 1 and count(db, "trading_cycles") == 1


def test_shadow_mode_never_calls_submit_order(db: Database, tmp_path: Path) -> None:
    deps, broker, _cap, clock = build(
        db, tmp_path, TUE, mode=TradingMode.SHADOW, broker_cls=SpyBroker
    )
    ran = run_all(deps, clock, TUE)
    assert set(ran.values()) == {"done"}, ran
    assert count(db, "shadow_orders") >= 1 and count(db, "order_intents") == 0
    assert broker.submit_calls == 0


@pytest.mark.parametrize("day", [SAT, HOLIDAY])
def test_weekends_and_holidays_have_no_cycle(db: Database, tmp_path: Path, day: date) -> None:
    deps, _, _, clock = build(db, tmp_path, TUE)
    clock.set(et(day, 15, 55))
    assert plan_for(day, deps.calendar, deps.settings.schedule) is None
    assert Scheduler(deps).tick() == {}
    assert count(db, "trading_cycles") == 0
    wake = Scheduler(deps).next_wake().astimezone(MARKET_TZ)
    assert wake.date() == date(2024, 7, 8 if day == SAT else 5) and (wake.hour, wake.minute) == (
        15,
        40,
    )


def test_early_close_shifts_every_step(db: Database, tmp_path: Path) -> None:
    deps, _, _, clock = build(db, tmp_path, EARLY)
    plan = plan_for(EARLY, deps.calendar, deps.settings.schedule)
    assert plan is not None
    local = {s.name: s.at.astimezone(MARKET_TZ).strftime("%H:%M") for s in plan.steps}
    assert local["health_check"] == "12:40" and local["order_submission"] == "12:50"
    assert plan.order_cutoff.astimezone(MARKET_TZ).strftime("%H:%M") == "12:55"
    ran = run_all(deps, clock, EARLY)
    assert ran["order_submission"] == "done"


def test_missed_order_window_after_restart_places_nothing(db: Database, tmp_path: Path) -> None:
    deps, broker, _, clock = build(db, tmp_path, TUE)
    clock.set(et(TUE, 15, 56))  # process started late: past the 15:55 no-new-orders cutoff
    ran = cycle_for(deps, clock.now()).run_due()  # type: ignore[union-attr]
    assert ran["order_submission"] == "skipped" and broker.submit_calls == 0


class HaltedBroker(SimulatedBroker):
    def get_market_clock(self) -> MarketClock:
        c = super().get_market_clock()
        return c.model_copy(update={"is_open": False})


def test_market_halt_refuses_submission(db: Database, tmp_path: Path) -> None:
    deps, broker, cap, clock = build(db, tmp_path, TUE, broker_cls=HaltedBroker)
    ran = run_all(deps, clock, TUE)
    assert ran["order_submission"] == "refused" and ran["fill_monitoring"] == "skipped"
    assert ran["reconciliation"] == "done" and broker.submit_calls == 0
    assert "trading_refused" in cap.events()
    assert CycleRepository(db).get("cyc-development-2024-07-02").status == "refused"  # type: ignore[union-attr]


def test_halted_instrument_is_not_traded(db: Database, tmp_path: Path) -> None:
    deps, broker, _, clock = build(db, tmp_path, TUE, tradeable=frozenset({"SQQQ"}))
    ran = run_all(deps, clock, TUE)
    assert ran["order_submission"] == "done" and broker.submit_calls == 0
    step = CycleRepository(db).steps("cyc-development-2024-07-02")["order_submission"]
    assert "QQQ" in step.payload["halted"]
    assert any("halted" in n for n in step.payload["notes"])


def test_stale_market_data_refuses_and_skips_trading(db: Database, tmp_path: Path) -> None:
    deps, broker, cap, clock = build(db, tmp_path, TUE, data_end=date(2024, 6, 21))
    ran = run_all(deps, clock, TUE)
    assert ran["market_data_update"] == "refused"
    assert all(
        ran[s] == "skipped" for s in ("indicators", "strategies", "risk", "order_submission")
    )
    assert ran["reconciliation"] == "done" and broker.submit_calls == 0
    assert "market_data_stale" in cap.events()


def test_data_provider_failure_refuses(db: Database, tmp_path: Path) -> None:
    deps, broker, _cap, clock = build(db, tmp_path, TUE, data_fail=True)
    ran = run_all(deps, clock, TUE)
    assert ran["market_data_update"] == "refused" and broker.submit_calls == 0


def test_kill_switch_allows_only_risk_reducing_orders(db: Database, tmp_path: Path) -> None:
    from decimal import Decimal

    deps, _broker, cap, clock = build(
        db, tmp_path, TUE, positions={"TQQQ": Decimal(300)}, cash=Decimal(80_000)
    )
    deps.kill_switch.engage("tester", "drill")
    run_all(deps, clock, TUE)
    with db.engine.connect() as c:
        sides = {r[0] for r in c.execute(text("SELECT side FROM order_intents")).all()}
    assert sides <= {"sell"}
    assert "kill_switch_activated" in cap.events()


def test_broker_down_at_health_check(db: Database, tmp_path: Path) -> None:
    deps, broker, cap, clock = build(db, tmp_path, TUE)
    broker.set_unavailable()
    ran = run_all(deps, clock, TUE)
    assert ran["health_check"] == "refused" and ran["reconciliation"] == "skipped"
    assert "broker_disconnected" in cap.events()
