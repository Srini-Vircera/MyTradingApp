"""Read-only queries for the operator API and dashboard (JSON-ready dicts).

Nothing here writes. Every list is bounded by ``limit``; ordering is newest
first unless stated otherwise.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, select

from adaptive_quant.persistence import models as m
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.repositories import jsonable

Row = dict[str, Any]


def _row(obj: Any, *skip: str) -> Row:
    data = {c.key: getattr(obj, c.key) for c in obj.__table__.columns if c.key not in skip}
    out: Row = jsonable(data)
    return out


class AuditReads:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------------------------------------------------------------- cycles
    def cycles(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.TradingCycle).order_by(desc(m.TradingCycle.session_date)).limit(limit)
            return [_row(c) for c in s.scalars(q)]

    def latest_cycle(self) -> Row | None:
        rows = self.cycles(1)
        if not rows:
            return None
        cycle = rows[0]
        with self.db.session() as s:
            steps = s.scalars(
                select(m.CycleStep)
                .where(m.CycleStep.cycle_id == cycle["cycle_id"])
                .order_by(m.CycleStep.id)
            )
            cycle["steps"] = [_row(x, "payload_json") for x in steps]
        return cycle

    # ---------------------------------------------------------------- portfolio
    def latest_account(self) -> Row | None:
        with self.db.session() as s:
            a = s.scalars(
                select(m.AccountSnapshotRow).order_by(desc(m.AccountSnapshotRow.as_of)).limit(1)
            ).first()
            return None if a is None else _row(a)

    def latest_positions(self, source: str = "broker") -> list[Row]:
        with self.db.session() as s:
            last = s.scalar(
                select(func.max(m.PositionSnapshot.as_of)).where(
                    m.PositionSnapshot.source == source
                )
            )
            if last is None:
                return []
            q = (
                select(m.PositionSnapshot)
                .where(m.PositionSnapshot.source == source, m.PositionSnapshot.as_of == last)
                .order_by(m.PositionSnapshot.symbol)
            )
            return [_row(p) for p in s.scalars(q)]

    def latest_target(self) -> Row | None:
        with self.db.session() as s:
            t = s.scalars(
                select(m.TargetPortfolioRow).order_by(desc(m.TargetPortfolioRow.as_of)).limit(1)
            ).first()
            return None if t is None else _row(t)

    # ---------------------------------------------------------------- decisions
    def signals(self, limit: int, cycle_id: str | None = None) -> list[Row]:
        with self.db.session() as s:
            q = select(m.StrategySignalRow).order_by(
                desc(m.StrategySignalRow.timestamp), m.StrategySignalRow.strategy_id
            )
            if cycle_id:
                q = q.where(m.StrategySignalRow.cycle_id == cycle_id)
            return [_row(x) for x in s.scalars(q.limit(limit))]

    def risk_decisions(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.RiskDecisionRow).order_by(desc(m.RiskDecisionRow.created_at)).limit(limit)
            return [_row(x) for x in s.scalars(q)]

    def ensemble_decisions(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.EnsembleDecisionRow).order_by(desc(m.EnsembleDecisionRow.id)).limit(limit)
            return [_row(x) for x in s.scalars(q)]

    def lifecycle_events(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = (
                select(
                    m.StrategyLifecycleEvent,
                    m.StrategyVersionRow.strategy_id,
                    m.StrategyVersionRow.version,
                )
                .join(
                    m.StrategyVersionRow,
                    m.StrategyVersionRow.id == m.StrategyLifecycleEvent.strategy_version_id,
                )
                .order_by(desc(m.StrategyLifecycleEvent.at))
                .limit(limit)
            )
            return [{**_row(e), "strategy_id": sid, "version": v} for e, sid, v in s.execute(q)]

    # ---------------------------------------------------------------- execution
    def orders(
        self, limit: int, state: str | None = None, cycle_id: str | None = None
    ) -> list[Row]:
        with self.db.session() as s:
            q = select(m.OrderIntent).order_by(desc(m.OrderIntent.created_at))
            if state:
                q = q.where(m.OrderIntent.state == state)
            if cycle_id:
                q = q.where(m.OrderIntent.cycle_id == cycle_id)
            out = []
            for o in s.scalars(q.limit(limit)):
                events = s.scalars(
                    select(m.OrderEvent)
                    .where(m.OrderEvent.client_order_id == o.client_order_id)
                    .order_by(m.OrderEvent.id)
                )
                out.append({**_row(o), "events": [_row(e, "raw_json") for e in events]})
            return out

    def shadow_orders(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.ShadowOrder).order_by(desc(m.ShadowOrder.created_at)).limit(limit)
            return [_row(x) for x in s.scalars(q)]

    def executions(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = (
                select(m.Execution, m.OrderIntent.symbol, m.OrderIntent.side)
                .join(m.OrderIntent, m.OrderIntent.client_order_id == m.Execution.client_order_id)
                .order_by(desc(m.Execution.at))
                .limit(limit)
            )
            return [{**_row(e), "symbol": sym, "side": side} for e, sym, side in s.execute(q)]

    def reconciliations(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.ReconciliationReportRow).order_by(
                desc(m.ReconciliationReportRow.at), desc(m.ReconciliationReportRow.id)
            )
            return [_row(x) for x in s.scalars(q.limit(limit))]

    # ---------------------------------------------------------------- monitoring
    def performance(self, environment: str, limit: int) -> list[Row]:
        """Oldest first (a time series)."""
        with self.db.session() as s:
            q = (
                select(m.DailyPerformance)
                .where(m.DailyPerformance.environment == environment)
                .order_by(desc(m.DailyPerformance.session_date))
                .limit(limit)
            )
            return list(reversed([_row(x) for x in s.scalars(q)]))

    def errors(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.ErrorRow).order_by(desc(m.ErrorRow.at)).limit(limit)
            return [_row(x, "traceback") for x in s.scalars(q)]

    def system_events(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.SystemEvent).order_by(desc(m.SystemEvent.at)).limit(limit)
            return [_row(x) for x in s.scalars(q)]

    def notifications(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.NotificationSent).order_by(desc(m.NotificationSent.at)).limit(limit)
            return [_row(x) for x in s.scalars(q)]

    def kill_switch_events(self, limit: int) -> list[Row]:
        with self.db.session() as s:
            q = select(m.KillSwitchEvent).order_by(desc(m.KillSwitchEvent.at)).limit(limit)
            return [_row(x) for x in s.scalars(q)]
