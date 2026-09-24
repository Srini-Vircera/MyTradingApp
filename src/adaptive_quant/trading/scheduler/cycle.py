"""One trading cycle per session (``cyc-<env>-<date>``), run step by step.

Steps (``schedule.CYCLE_STEPS``) run when due; each outcome is persisted in
``cycle_steps`` so a restarted process **resumes the same cycle**: finished
steps are skipped, the stored decision / target / reference prices are reused,
and the order planner's idempotency plus deterministic client order ids mean
nothing is sent twice.

Fail closed: a refusal or failure in any step up to ``portfolio_target`` skips
order submission and fill monitoring (recorded as ``skipped``); reconciliation
still runs after the close when the cycle began with confirmed broker state.
Every refusal is persisted and notified.
"""

from __future__ import annotations

import contextlib
import math
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from adaptive_quant.config.schema import Settings
from adaptive_quant.core.clock import MARKET_TZ, Clock
from adaptive_quant.core.enums import OrderSide, TradingMode
from adaptive_quant.core.errors import AQError, DatabaseUnavailableError
from adaptive_quant.core.ids import new_run_id, trading_cycle_id
from adaptive_quant.core.models import StrategySignal
from adaptive_quant.notifications.base import EventType, Notification, NotificationRouter
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.records import CycleRecord
from adaptive_quant.persistence.repositories import (
    CycleRepository,
    MonitoringRepository,
    ReferenceRepository,
)
from adaptive_quant.quant.data.bars import Frequency
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.quant.data.validation import BarValidator
from adaptive_quant.quant.data.view import MarketDataView
from adaptive_quant.quant.ensemble.engine import EnsembleEngine, Member
from adaptive_quant.quant.ensemble.shadow import ShadowBook
from adaptive_quant.quant.portfolio.manager import PortfolioManager
from adaptive_quant.quant.portfolio.policy import AllocationPolicy
from adaptive_quant.quant.risk.engine import RiskEngine
from adaptive_quant.quant.risk.models import RiskCalculationError, RiskContext
from adaptive_quant.quant.strategies.base import Strategy
from adaptive_quant.quant.strategies.catalog import StrategyVersion
from adaptive_quant.quant.strategies.runner import run_strategies
from adaptive_quant.trading.audit import (
    config_record,
    decision_record,
    order_repository,
    strategy_version_record,
)
from adaptive_quant.trading.brokers.base import BrokerAdapter
from adaptive_quant.trading.orders.manager import OrderManager
from adaptive_quant.trading.orders.planner import OrderPlan, OrderPlanner, PlanningInput, Skipped
from adaptive_quant.trading.reconciliation import reconciler as rc
from adaptive_quant.trading.safety.broker_checks import BrokerStateCheck, ReconciliationCheck
from adaptive_quant.trading.safety.data_checks import MarketDataCheck
from adaptive_quant.trading.safety.db_checks import DatabaseCheck
from adaptive_quant.trading.safety.kill_switch import KillSwitch
from adaptive_quant.trading.safety.preflight import (
    BlockScope,
    CheckResult,
    KillSwitchCheck,
    PreflightCheck,
    PreflightGate,
    RefusalReason,
)
from adaptive_quant.trading.scheduler.data import CycleData
from adaptive_quant.trading.scheduler.schedule import CYCLE_STEPS, SessionPlan, plan_for
from adaptive_quant.trading.scheduler.summary import end_of_day_summary

UNDERLYING = "QQQ"
FINISHED = ("done", "refused", "failed", "skipped")
_OPEN_STATES = ("created", "validated", "submitted", "acknowledged", "partially_filled", "unknown")

QuoteSource = Callable[[Sequence[str]], dict[str, Decimal]]


@dataclass
class CycleDeps:
    settings: Settings
    config_version: str
    config_record_source: Any  # LoadedConfig (for the config-version audit row)
    calendar: TradingCalendar
    clock: Clock
    db: Database
    broker: BrokerAdapter
    data: CycleData
    strategies: Sequence[Strategy]
    versions: dict[str, StrategyVersion]
    kill_switch: KillSwitch
    validator: BarValidator
    notifier: NotificationRouter | None = None
    quotes: QuoteSource | None = None  # current prices; default: last known closes


class TradingCycle:
    def __init__(self, deps: CycleDeps, plan: SessionPlan) -> None:
        self.d = deps
        s = deps.settings
        self.plan = plan
        self.mode = s.trading.mode
        self.env = s.app.environment.value
        self.cycle_id = trading_cycle_id(plan.date.isoformat(), self.env)
        self.cycles = CycleRepository(deps.db)
        self.orders = order_repository(deps.db)
        self.monitor = MonitoringRepository(deps.db)
        self.manager = OrderManager(deps.broker, self.orders, self.cycles, deps.clock, self.mode)
        self.planner = OrderPlanner(
            s.trading, s.risk, s.universe.by_symbol, deps.broker.capabilities.supports_fractional
        )
        self.portfolio = PortfolioManager(
            EnsembleEngine(
                [Member(x.strategy_id, x.family) for x in deps.strategies], s.strategies.ensemble
            ),
            AllocationPolicy(
                s.backtest.allocation.long_mode, s.backtest.allocation.min_abs_exposure
            ),
            RiskEngine(s.risk, s.universe.by_symbol),
        )
        self.tradeable = list(s.universe.tradeable_symbols)
        self._handlers: dict[str, Callable[[], tuple[str, str, dict[str, Any]]]] = {
            "health_check": self._health_check,
            "market_data_update": self._market_data_update,
            "indicators": self._indicators,
            "strategies": self._strategies,
            "risk": self._risk,
            "portfolio_target": self._portfolio_target,
            "order_submission": self._order_submission,
            "fill_monitoring": self._fill_monitoring,
            "reconciliation": self._reconciliation,
        }

    # ================================================================== driver
    def run_due(self) -> dict[str, str]:
        """Run every step that is due and not yet finished. Returns step -> status."""
        now = self.d.clock.now()
        self._ensure_started(now)
        cycle = self.cycles.get(self.cycle_id)
        if cycle is not None and cycle.status != "running":
            return {}
        ran: dict[str, str] = {}
        for step in self.plan.steps:
            if step.at > now:
                break
            done = self.cycles.steps(self.cycle_id)
            if step.name in done and done[step.name].status in FINISHED:
                continue
            status, detail, payload = self._run_step(step.name, done)
            self.cycles.record_step(
                self.cycle_id, step.name, status, self.d.clock.now(), detail, payload
            )
            ran[step.name] = status
        if "reconciliation" in ran:
            steps = self.cycles.steps(self.cycle_id)
            final = "completed" if all(v.status in ("done",) for v in steps.values()) else "refused"
            self.cycles.finish(self.cycle_id, final, self.d.clock.now(), _summary_line(steps))
        return ran

    def _run_step(self, name: str, done: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        if name == "reconciliation":
            hc = done.get("health_check")
            if hc is None or hc.status != "done":
                return "skipped", "no confirmed start-of-cycle broker state", {}
        elif name != "health_check":
            prior = CYCLE_STEPS[: CYCLE_STEPS.index(name)]
            not_ok = [p for p in prior if p not in done or done[p].status != "done"]
            if not_ok:
                return "skipped", f"earlier steps not done: {not_ok}", {}
        try:
            return self._handlers[name]()
        except DatabaseUnavailableError:
            raise  # cannot record anything: the caller must stop
        except RiskCalculationError as exc:
            self._notify(EventType.TRADING_REFUSED, step=name, reason=str(exc))
            return "refused", str(exc), {"reason": RefusalReason.RISK_CALCULATION_FAILURE.value}
        except Exception as exc:  # noqa: BLE001 - any failure refuses the step, never crashes the loop
            self.monitor.record_error(
                f"cycle:{name}", exc, self.d.clock.now(), self.cycle_id, traceback.format_exc()
            )
            self._notify(
                EventType.TRADING_REFUSED, step=name, reason=f"{type(exc).__name__}: {exc}"
            )
            return "failed", f"{type(exc).__name__}: {exc}", {}

    def _ensure_started(self, now: datetime) -> None:
        if self.cycles.get(self.cycle_id) is not None:
            return
        ref = ReferenceRepository(self.d.db)
        ref.record_config(config_record(self.d.config_record_source))
        ref.sync_instruments(self.d.settings.universe.instruments)
        for v in self.d.versions.values():
            ref.ensure_strategy_version(strategy_version_record(v))
        self.cycles.start(
            CycleRecord(
                self.cycle_id,
                self.plan.date,
                self.env,
                self.mode.value,
                self.d.config_version,
                new_run_id(now),
                now,
            )
        )

    # ================================================================== steps
    def _health_check(self) -> tuple[str, str, dict[str, Any]]:
        now = self.d.clock.now()
        self.d.db.ping()
        try:
            account = self.d.broker.get_account()
            positions = self.d.broker.get_positions()
        except AQError as exc:
            self._notify(EventType.BROKER_DISCONNECTED, step="health_check", reason=exc.message)
            return (
                "refused",
                f"broker unavailable: {exc.message}",
                {"reason": RefusalReason.BROKER_UNAVAILABLE.value},
            )
        self.cycles.record_account(
            self.cycle_id, account.equity, account.cash, account.buying_power, account.is_paper, now
        )
        self.cycles.record_positions(
            self.cycle_id,
            "broker",
            [(p.symbol, p.quantity, p.market_value) for p in positions],
            now,
        )
        ks = self.d.kill_switch.status()
        if ks.engaged:
            self._notify(EventType.KILL_SWITCH_ACTIVATED, reason=ks.reason)
        return (
            "done",
            "database, broker and calendar reachable",
            {
                "start_positions": {p.symbol: str(p.quantity) for p in positions},
                "start_equity": str(account.equity),
                "kill_switch_engaged": ks.engaged,
            },
        )

    def _market_data_update(self) -> tuple[str, str, dict[str, Any]]:
        try:
            notes = self.d.data.refresh()
        except AQError as exc:
            self._notify(EventType.MARKET_DATA_STALE, reason=exc.message)
            return "refused", exc.message, {"reason": RefusalReason.MARKET_DATA_STALE.value}
        result = self._data_check().run()
        if not result.passed:
            self._notify(EventType.MARKET_DATA_STALE, reason=result.detail)
            return (
                "refused",
                result.detail,
                {"reason": result.reason.value if result.reason else ""},
            )
        return "done", result.detail, {"notes": notes}

    def _indicators(self) -> tuple[str, str, dict[str, Any]]:
        view = self._view()
        stamps = {s: view.data_timestamp(s).isoformat() for s in view.symbols}
        return "done", "point-in-time view built", {"data_timestamps": stamps}

    def _strategies(self) -> tuple[str, str, dict[str, Any]]:
        batch = run_strategies(self.d.strategies, self._view())
        if not batch.ok:
            reason = "; ".join(batch.problems()) or "no eligible strategy produced a signal"
            self._notify(EventType.STRATEGY_FAILURE, reason=reason)
            return "refused", reason, {"reason": RefusalReason.SIGNAL_FAILURE.value}
        return (
            "done",
            f"{len(batch.signals)} signal(s)",
            {"signals": [s.model_dump(mode="json") for s in batch.signals]},
        )

    def _risk(self) -> tuple[str, str, dict[str, Any]]:
        steps = self.cycles.steps(self.cycle_id)
        signals = [StrategySignal.model_validate(x) for x in steps["strategies"].payload["signals"]]
        view = self._view()
        now = self.d.clock.now()
        account = self.d.broker.get_account()
        positions = {p.symbol: p.quantity for p in self.d.broker.get_positions()}
        ref_prices = {
            s: Decimal(str(float(view.latest(s)["close"]))) for s in self.tradeable if view.has(s)
        }
        equity = float(account.equity)
        history = [float(e) for _, e in self.monitor.equity_history(self.env)]
        current = {
            s: float(q * ref_prices[s]) / equity for s, q in positions.items() if s in ref_prices
        }
        closes = view.close(UNDERLYING)
        ctx = RiskContext(
            as_of=now,
            equity=equity,
            peak_equity=max([equity, *history]),
            previous_equity=history[-1] if history else equity,
            current_weights=current,
            underlying_returns=closes.pct_change().dropna().to_numpy(dtype=float),
            kill_switch_engaged=self.d.kill_switch.is_engaged(),
            previous_band=self.cycles.last_band(before_cycle=self.cycle_id),
        )
        decision = self.portfolio.decide(signals, self._shadow_returns(closes), ctx)
        decision_id = f"{self.cycle_id}-d1"
        rec = decision_record(
            self.cycle_id,
            decision_id,
            self.d.config_version,
            decision,
            signals,
            self.d.settings.universe.by_symbol,
        )
        self.cycles.record_decision(rec, self.d.config_version)
        r = decision.risk
        if r.band != "normal" and r.band != ctx.previous_band:
            self._notify(
                EventType.DRAWDOWN_THRESHOLD,
                band=r.band,
                drawdown=f"{r.drawdown:.1%}",
                detail="; ".join(r.flags),
            )
        if r.daily_loss >= self.d.settings.risk.max_daily_loss:
            self._notify(
                EventType.DAILY_LOSS_THRESHOLD,
                daily_loss=f"{r.daily_loss:.2%}",
                limit=f"{self.d.settings.risk.max_daily_loss:.2%}",
            )
        return (
            "done",
            f"decision {decision_id}: band {r.band}, vol scale {r.vol_scale:.3f}",
            {
                "decision_id": decision_id,
                "reference_prices": {k: str(v) for k, v in ref_prices.items()},
                "blocked_risk_increasing": r.risk_increasing_blocked,
                "daily_loss": r.daily_loss,
                "band": r.band,
                "flags": list(r.flags),
            },
        )

    def _portfolio_target(self) -> tuple[str, str, dict[str, Any]]:
        steps = self.cycles.steps(self.cycle_id)
        risk = steps["risk"].payload
        checks: list[PreflightCheck] = [
            KillSwitchCheck(
                self.d.kill_switch, self.d.settings.trading.kill_switch.allow_risk_reducing_orders
            ),
            DatabaseCheck(self.d.db),
            BrokerStateCheck(self.d.broker),
            self._data_check(),
            _StoredRiskCheck(risk, self.d.settings.risk.max_daily_loss),
            ReconciliationCheck(self.cycles.latest_reconciliation),
        ]
        report = PreflightGate(checks, self.d.clock).evaluate()
        self.cycles.record_preflight(
            self.cycle_id,
            report.may_increase_risk,
            [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "reason": r.reason,
                    "scope": r.scope,
                    "detail": r.detail,
                }
                for r in report.results
            ],
            report.checked_at,
        )
        if not report.may_reduce_risk:
            self._notify(
                EventType.TRADING_REFUSED, step="portfolio_target", reason=report.summary()
            )
            return "refused", report.summary(), {"risk_increasing_allowed": False}
        if not report.may_increase_risk:
            self._notify(
                EventType.TRADING_REFUSED,
                step="portfolio_target",
                reason="risk-reducing orders only:\n" + report.summary(),
            )
        return "done", report.summary(), {"risk_increasing_allowed": report.may_increase_risk}

    def _order_submission(self) -> tuple[str, str, dict[str, Any]]:
        now = self.d.clock.now()
        if now >= self.plan.order_cutoff:
            return "skipped", "past the no-new-orders cutoff", {}
        clock = self.d.broker.get_market_clock()
        if not clock.is_open:
            self._notify(
                EventType.TRADING_REFUSED,
                step="order_submission",
                reason="broker reports the market is not open (halt?)",
            )
            return "refused", "market not open according to the broker (halt or closure)", {}
        steps = self.cycles.steps(self.cycle_id)
        allow_increase = bool(steps["portfolio_target"].payload.get("risk_increasing_allowed"))
        risk = steps["risk"].payload
        target = self.cycles.load_target(str(risk["decision_id"]))
        reference = {k: Decimal(v) for k, v in risk["reference_prices"].items()}
        assets = self.d.broker.get_tradeable_assets(self.tradeable)
        halted = {s for s in self.tradeable if s not in assets or not assets[s].tradable}
        transmitted, notes = 0, []
        for side in (OrderSide.SELL, OrderSide.BUY):  # sells first; buys re-planned after sells
            if side is OrderSide.BUY:
                self.manager.sync(self.cycle_id)
            plan = self._filtered(self._plan(target, reference), allow_increase, halted)
            if not plan.ok:
                self._notify(
                    EventType.TRADING_REFUSED,
                    step="order_submission",
                    reason=f"{plan.refused}: {plan.refusal_detail}",
                )
                return (
                    "refused",
                    f"{plan.refused}: {plan.refusal_detail}",
                    {"transmitted": transmitted},
                )
            notes += [f"{s.symbol}: {s.reason}" for s in plan.skipped] + list(plan.notes)
            report = self.manager.execute(self.cycle_id, plan, only_side=side)
            if report.halted:
                return "refused", report.halted, {"transmitted": transmitted}
            transmitted += report.transmitted
            for o in report.outcomes:
                self._order_event(o)
        return (
            "done",
            f"{transmitted} order(s) transmitted",
            {"transmitted": transmitted, "notes": notes, "halted": sorted(halted)},
        )

    def _fill_monitoring(self) -> tuple[str, str, dict[str, Any]]:
        outcomes = self.manager.sync(self.cycle_id)
        for o in outcomes:
            if o.state == "partially_filled":
                self._notify(
                    EventType.PARTIAL_FILL,
                    client_order_id=o.client_order_id,
                    symbol=o.symbol,
                    quantity=o.quantity,
                    filled="?",
                )
        return "done", f"{len(outcomes)} working order(s) checked", {}

    def _reconciliation(self) -> tuple[str, str, dict[str, Any]]:
        now = self.d.clock.now()
        self.manager.recover()
        steps = self.cycles.steps(self.cycle_id)
        start = {k: Decimal(v) for k, v in steps["health_check"].payload["start_positions"].items()}
        expected = rc.expected_positions(start, self.orders.executions_for_cycle(self.cycle_id))
        account = self.d.broker.get_account()
        positions = self.d.broker.get_positions()
        result = rc.Reconciler(
            self.d.settings.trading.reconciliation_qty_tolerance,
            self.d.settings.trading.reconciliation_cash_tolerance,
        ).reconcile(
            expected=expected,
            broker_positions=positions,
            internal_open=self.orders.intents(states=_OPEN_STATES),
            broker_open=self.d.broker.get_open_orders(),
        )
        rc.record(self.cycles, self.cycle_id, result, now)
        self.cycles.record_positions(
            self.cycle_id, "expected", [(s, q, Decimal(0)) for s, q in expected.items()], now
        )
        if not result.passed:
            self._notify(
                EventType.RECONCILIATION_FAILURE,
                differences="\n".join(str(d) for d in result.differences),
            )
        summary = self._daily_performance(account.equity, now, steps, result.passed)
        self._notify(EventType.END_OF_DAY_SUMMARY, **summary)
        return (
            ("done" if result.passed else "refused"),
            ("reconciled" if result.passed else f"{len(result.differences)} discrepancy(ies)"),
            {"passed": result.passed, "differences": result.differences},
        )

    # ================================================================== helpers
    def _view(self) -> MarketDataView:
        return MarketDataView(self.d.data.frames(), self.d.clock.now())

    def _data_check(self) -> MarketDataCheck:
        symbols = sorted({*self.tradeable, *(s.signal_symbol for s in self.d.strategies)})
        return MarketDataCheck(
            symbols=symbols,
            frequency=Frequency.DAILY,
            loader=self.d.data.bars,
            validator=self.d.validator,
            calendar=self.d.calendar,
            staleness=self.d.settings.data.staleness,
            clock=self.d.clock,
        )

    def _shadow_returns(self, closes: pd.Series) -> np.ndarray:
        ids = [s.strategy_id for s in self.d.strategies]
        book = ShadowBook(ids)
        for ts, exposures in self.cycles.signal_history(
            ids, self.d.settings.strategies.ensemble.lookback_sessions + 1
        ):
            book.record(ts, exposures)
        try:
            return book.returns(closes, self.d.clock.now())
        except KeyError:  # a stored timestamp without a matching bar: weight from what we have
            return np.zeros((0, len(ids)))

    def _plan(self, target: Any, reference: dict[str, Decimal]) -> OrderPlan:
        b = self.d.broker
        quotes = self.d.quotes(self.tradeable) if self.d.quotes else dict(reference)
        inp = PlanningInput(
            cycle_id=self.cycle_id,
            target=target,
            account=b.get_account(),
            positions=b.get_positions(),
            broker_open_orders=b.get_open_orders(),
            internal_open=self.orders.intents(states=_OPEN_STATES),
            prices=quotes,
            reference_prices=reference,
        )
        return self.planner.plan(
            inp, lambda sym, side: self.orders.next_sequence(self.cycle_id, sym, side.value)
        )

    @staticmethod
    def _filtered(plan: OrderPlan, allow_increase: bool, halted: set[str]) -> OrderPlan:
        if not plan.ok:
            return plan
        keep, skipped = [], list(plan.skipped)
        for o in plan.orders:
            if o.symbol in halted:
                skipped.append(
                    Skipped(o.symbol, "trading halted / not tradeable at the broker", o.quantity)
                )
            elif o.risk_increasing and not allow_increase:
                skipped.append(
                    Skipped(
                        o.symbol, "risk-increasing orders blocked by the pre-trade gate", o.quantity
                    )
                )
            else:
                keep.append(o)
        return replace(plan, orders=tuple(keep), skipped=tuple(skipped))

    def _order_event(self, o: Any) -> None:
        if o.state == "rejected":
            self._notify(
                EventType.ORDER_REJECTED,
                client_order_id=o.client_order_id,
                side=o.side,
                quantity=o.quantity,
                symbol=o.symbol,
                reason=o.detail,
            )
        elif o.state == "shadow":
            return
        else:
            self._notify(
                EventType.ORDER_PLACED,
                client_order_id=o.client_order_id,
                side=o.side,
                quantity=o.quantity,
                symbol=o.symbol,
                state=o.state,
                decision_id="",
                risk_increasing="",
            )
            if o.state == "partially_filled":
                self._notify(
                    EventType.PARTIAL_FILL,
                    client_order_id=o.client_order_id,
                    symbol=o.symbol,
                    quantity=o.quantity,
                    filled="partial",
                )

    def _daily_performance(
        self, equity: Decimal, now: datetime, steps: dict[str, Any], reconciled: bool
    ) -> dict[str, Any]:
        history = self.monitor.equity_history(self.env)
        previous = [e for d, e in history if d < self.plan.date]
        prev = (
            previous[-1]
            if previous
            else Decimal(str(steps["health_check"].payload["start_equity"]))
        )
        peak = max([equity, *(e for _, e in history)])
        pnl = equity - prev
        ret = float(pnl / prev) if prev else 0.0
        dd = float(1 - equity / peak) if peak else 0.0
        fills = self.orders.executions_for_cycle(self.cycle_id)
        self.monitor.upsert_daily_performance(
            self.plan.date, self.env, equity, pnl, ret, dd, 0.0, {}
        )
        text = end_of_day_summary(
            session_date=self.plan.date,
            mode=self.mode,
            equity=equity,
            pnl=pnl,
            daily_return=ret,
            drawdown=dd,
            steps={k: (v.status, v.detail) for k, v in self.cycles.steps(self.cycle_id).items()},
            fills=fills,
            positions={p.symbol: p.quantity for p in self.d.broker.get_positions()},
            reconciled=reconciled,
        )
        return {
            "session_date": self.plan.date.isoformat(),
            "equity": f"{equity:,.2f}",
            "daily_return": f"{ret:+.2%}",
            "summary": text,
        }

    def _notify(self, event: EventType, **ctx: Any) -> None:
        router = self.d.notifier
        if router is None:
            return
        now = self.d.clock.now()
        base = {
            "mode": self.mode.value,
            "environment": self.env,
            "config_version": self.d.config_version,
            "cycle_id": self.cycle_id,
            "time": now.isoformat(),
        }
        n = Notification(event=event, title=event.value, body="", at=now, context={**base, **ctx})
        for r in router.publish(n):
            with contextlib.suppress(AQError):  # an audit write failure must not hide the alert
                self.monitor.record_notification(
                    event.value, n.effective_severity.value, r.channel, r.delivered, now, r.error
                )


class _StoredRiskCheck:
    """The pre-trade view of the stored risk decision (same semantics as RiskEngineCheck)."""

    name = "risk_engine"

    def __init__(self, payload: dict[str, Any], max_daily_loss: float) -> None:
        self._p = payload
        self._limit = max_daily_loss

    def run(self) -> CheckResult:
        loss = float(self._p.get("daily_loss", math.nan))
        if not math.isfinite(loss):
            return CheckResult.fail(
                self.name, RefusalReason.RISK_CALCULATION_FAILURE, "no stored risk decision"
            )
        if loss >= self._limit:
            return CheckResult.fail(
                self.name,
                RefusalReason.DAILY_LOSS_LIMIT,
                f"daily loss {loss:.2%}",
                BlockScope.RISK_INCREASING,
            )
        if self._p.get("blocked_risk_increasing"):
            return CheckResult.fail(
                self.name,
                RefusalReason.DRAWDOWN_EMERGENCY,
                f"blocked: {self._p.get('flags')}",
                BlockScope.RISK_INCREASING,
            )
        return CheckResult.ok(self.name, f"band {self._p.get('band')}")


def _summary_line(steps: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v.status}" for k, v in steps.items())


def cycle_for(deps: CycleDeps, now: datetime) -> TradingCycle | None:
    """The cycle for the session containing ``now`` (``None`` on weekends / holidays)."""
    plan = plan_for(now.astimezone(MARKET_TZ).date(), deps.calendar, deps.settings.schedule)
    return None if plan is None else TradingCycle(deps, plan)


__all__ = ["CycleDeps", "TradingCycle", "TradingMode", "cycle_for"]
