""" "Why is TQQQ at 40%?" - the full, stored decision chain of one trading cycle.

cycle -> config version -> pre-trade report -> target portfolio -> risk decision
(every adjustment) -> proposal -> ensemble -> strategy signals (scores, reasons,
indicator values, strategy versions) -> indicator snapshots (data snapshots)
-> orders (state history, executions) and broker/expected snapshots.
The result is JSON-serialisable.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from adaptive_quant.core.errors import PersistenceError
from adaptive_quant.persistence import models as m
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.repositories import jsonable


def _row(obj: Any, *skip: str) -> dict[str, Any]:
    out = {c.key: getattr(obj, c.key) for c in obj.__table__.columns if c.key not in skip}
    return jsonable(out)  # type: ignore[no-any-return]


def explain_decision(db: Database, cycle_id: str) -> dict[str, Any]:
    with db.session() as s:
        cycle = s.get(m.TradingCycle, cycle_id)
        if cycle is None:
            raise PersistenceError(f"unknown trading cycle {cycle_id}")
        cfg = s.get(m.ConfigVersion, cycle.config_version)

        def rows(model: Any, *order: Any) -> list[Any]:
            return list(s.scalars(select(model).where(model.cycle_id == cycle_id).order_by(*order)))

        risks = rows(m.RiskDecisionRow, m.RiskDecisionRow.created_at)
        decisions = []
        for r in risks:
            target = s.get(m.TargetPortfolioRow, r.decision_id)
            proposal = s.get(m.PortfolioProposalRow, r.proposal_id)
            decisions.append(
                {
                    "decision_id": r.decision_id,
                    "target": None if target is None else _row(target),
                    "risk": _row(r),
                    "proposal": None if proposal is None else _row(proposal),
                }
            )
        versions = {
            v.id: v
            for v in s.scalars(
                select(m.StrategyVersionRow)
                .join(
                    m.StrategySignalRow,
                    m.StrategySignalRow.strategy_version_id == m.StrategyVersionRow.id,
                )
                .where(m.StrategySignalRow.cycle_id == cycle_id)
            )
        }
        signals = []
        for sig in rows(m.StrategySignalRow, m.StrategySignalRow.strategy_id):
            v = versions.get(sig.strategy_version_id)
            signals.append(
                {
                    **_row(sig),
                    "strategy_version": None if v is None else _row(v, "created_at"),
                }
            )
        indicators = []
        for ind in rows(m.IndicatorSnapshot, m.IndicatorSnapshot.symbol):
            snap = (
                None
                if ind.data_snapshot_id is None
                else s.get(m.DataSnapshot, ind.data_snapshot_id)
            )
            indicators.append({**_row(ind), "data_snapshot": None if snap is None else _row(snap)})
        orders = []
        for o in rows(m.OrderIntent, m.OrderIntent.created_at):
            events = s.scalars(
                select(m.OrderEvent)
                .where(m.OrderEvent.client_order_id == o.client_order_id)
                .order_by(m.OrderEvent.at, m.OrderEvent.id)
            )
            execs = s.scalars(
                select(m.Execution)
                .where(m.Execution.client_order_id == o.client_order_id)
                .order_by(m.Execution.at)
            )
            orders.append(
                {
                    **_row(o),
                    "events": [_row(e) for e in events],
                    "executions": [_row(x) for x in execs],
                }
            )
        return {
            "cycle": _row(cycle),
            "config": None
            if cfg is None
            else {
                "config_version": cfg.config_version,
                "environment": cfg.environment,
                "digest": cfg.digest,
            },
            "preflight": [
                _row(p) for p in rows(m.PreflightReportRow, m.PreflightReportRow.checked_at)
            ],
            "decisions": decisions,
            "ensemble": [_row(e) for e in rows(m.EnsembleDecisionRow, m.EnsembleDecisionRow.id)],
            "signals": signals,
            "indicators": indicators,
            "orders": orders,
            "shadow_orders": [_row(x) for x in rows(m.ShadowOrder, m.ShadowOrder.id)],
            "account": [_row(a) for a in rows(m.AccountSnapshotRow, m.AccountSnapshotRow.as_of)],
            "positions": [_row(p) for p in rows(m.PositionSnapshot, m.PositionSnapshot.id)],
            "reconciliation": [
                _row(x) for x in rows(m.ReconciliationReportRow, m.ReconciliationReportRow.at)
            ],
        }
