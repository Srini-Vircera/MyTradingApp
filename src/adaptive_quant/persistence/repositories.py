"""Repositories over the audit database.

Every public method is one transaction (``Database.session``): it either commits
completely or raises (``DatabaseUnavailableError`` / ``PersistenceError``) and
leaves nothing behind. In particular :meth:`OrderRepository.create_intent`
returns only after the intent is durably committed - if it raises, the order
must not be transmitted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from adaptive_quant.core.clock import ensure_utc
from adaptive_quant.core.enums import OrderState
from adaptive_quant.core.errors import PersistenceError
from adaptive_quant.core.models import Instrument, OrderRequest, TargetPortfolio
from adaptive_quant.persistence import models as m
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.records import (
    ConfigRecord,
    CycleRecord,
    CycleView,
    DataSnapshotRecord,
    DecisionRecord,
    ExecutionRecord,
    IntentView,
    Json,
    LifecycleEventRecord,
    OrderUpdate,
    ReconciliationView,
    ResearchRunRecord,
    StepView,
    StrategyVersionRecord,
)

#: ``guard(current, target)`` raises if the transition is not allowed. The trading
#: layer passes ``trading.orders.state_machine.assert_transition``.
TransitionGuard = Callable[[OrderState, OrderState], None]


def jsonable(obj: Any) -> Any:
    """Decimals as strings (exact), datetimes as ISO-8601 UTC, recursively."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return ensure_utc(obj).isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


class _Repo:
    def __init__(self, db: Database) -> None:
        self.db = db


# ================================================================== reference data
class ReferenceRepository(_Repo):
    def record_config(self, rec: ConfigRecord) -> None:
        """Insert a configuration version once (idempotent on the fingerprint)."""
        with self.db.session() as s:
            s.execute(
                insert(m.ConfigVersion)
                .values(
                    config_version=rec.config_version,
                    environment=rec.environment,
                    digest=rec.digest,
                    resolved_json=jsonable(rec.resolved),
                    source_hashes=rec.source_hashes,
                )
                .on_conflict_do_nothing(index_elements=["config_version"])
            )

    def sync_instruments(self, instruments: Iterable[Instrument]) -> None:
        with self.db.session() as s:
            for i in instruments:
                values = {
                    "asset_class": i.asset_class.value,
                    "leverage": float(i.leverage),
                    "underlying": i.underlying,
                    "tradeable": i.tradeable,
                }
                s.execute(
                    insert(m.InstrumentRow)
                    .values(symbol=i.symbol, **values)
                    .on_conflict_do_update(index_elements=["symbol"], set_=values)
                )

    def ensure_strategy_version(self, rec: StrategyVersionRecord) -> int:
        with self.db.session() as s:
            return self._strategy_version_id(s, rec)

    @staticmethod
    def _strategy_version_id(s: Session, rec: StrategyVersionRecord) -> int:
        s.execute(
            insert(m.StrategyVersionRow)
            .values(
                strategy_id=rec.strategy_id,
                version=rec.version,
                implementation=rec.implementation,
                family=rec.family,
                params_json=jsonable(rec.params),
                code_hash=rec.code_hash,
                research_notes=rec.notes,
            )
            .on_conflict_do_nothing(constraint="uq_strategy_versions_strategy_version")
        )
        return int(
            s.scalars(
                select(m.StrategyVersionRow.id).where(
                    m.StrategyVersionRow.strategy_id == rec.strategy_id,
                    m.StrategyVersionRow.version == rec.version,
                )
            ).one()
        )

    def record_lifecycle_event(self, rec: LifecycleEventRecord) -> None:
        with self.db.session() as s:
            sv = _strategy_version(s, rec.strategy_id, rec.version)
            s.add(
                m.StrategyLifecycleEvent(
                    strategy_version_id=sv,
                    from_state=rec.from_state,
                    to_state=rec.to_state,
                    actor_id=rec.actor_id,
                    actor_kind=rec.actor_kind,
                    reason=rec.reason,
                    at=ensure_utc(rec.at),
                )
            )

    def current_lifecycle(self, strategy_id: str, version: str) -> str | None:
        """Latest recorded state of a strategy version (append-only history)."""
        with self.db.session() as s:
            sv = _strategy_version(s, strategy_id, version)
            return s.scalars(
                select(m.StrategyLifecycleEvent.to_state)
                .where(m.StrategyLifecycleEvent.strategy_version_id == sv)
                .order_by(m.StrategyLifecycleEvent.at.desc(), m.StrategyLifecycleEvent.id.desc())
                .limit(1)
            ).first()

    def record_research_runs(self, runs: Sequence[ResearchRunRecord]) -> int:
        with self.db.session() as s:
            for r in runs:
                sv = None
                if r.strategy_id and r.version:
                    sv = s.scalars(
                        select(m.StrategyVersionRow.id).where(
                            m.StrategyVersionRow.strategy_id == r.strategy_id,
                            m.StrategyVersionRow.version == r.version,
                        )
                    ).first()
                s.add(
                    m.ResearchRun(
                        kind=r.kind,
                        run_id=r.run_id,
                        trial_id=r.trial_id,
                        strategy_version_id=sv,
                        config_version=r.config_version,
                        data_fingerprint=r.data_fingerprint,
                        metrics_json=jsonable(r.metrics),
                        artifact_uri=r.artifact_uri,
                    )
                )
            return len(runs)

    def record_data_snapshot(self, rec: DataSnapshotRecord) -> int:
        with self.db.session() as s:
            s.execute(
                insert(m.DataSnapshot)
                .values(**{k: getattr(rec, k) for k in rec.__dataclass_fields__})
                .on_conflict_do_nothing(constraint="uq_data_snapshots_content")
            )
            return int(
                s.scalars(
                    select(m.DataSnapshot.id).where(
                        m.DataSnapshot.provider == rec.provider,
                        m.DataSnapshot.symbol == rec.symbol,
                        m.DataSnapshot.frequency == rec.frequency,
                        m.DataSnapshot.adjustment == rec.adjustment,
                        m.DataSnapshot.content_hash == rec.content_hash,
                    )
                ).one()
            )

    def record_quality_report(
        self, snapshot_id: int, passed: bool, issues: list[Any], at: datetime
    ) -> None:
        with self.db.session() as s:
            s.add(
                m.DataQualityReport(
                    snapshot_id=snapshot_id,
                    passed=passed,
                    issues_json={"issues": jsonable(issues)},
                    checked_at=ensure_utc(at),
                )
            )


def _strategy_version(s: Session, strategy_id: str, version: str) -> int:
    sv = s.scalars(
        select(m.StrategyVersionRow.id).where(
            m.StrategyVersionRow.strategy_id == strategy_id,
            m.StrategyVersionRow.version == version,
        )
    ).first()
    if sv is None:
        raise PersistenceError(
            f"strategy version {strategy_id} {version} is not registered",
            hint="register it with ReferenceRepository.ensure_strategy_version first",
        )
    return int(sv)


# ================================================================== trading cycles
class CycleRepository(_Repo):
    def start(self, rec: CycleRecord) -> None:
        with self.db.session() as s:
            if s.get(m.ConfigVersion, rec.config_version) is None:
                raise PersistenceError(f"config version {rec.config_version} is not recorded")
            if s.get(m.TradingCycle, rec.cycle_id) is not None:
                raise PersistenceError(f"trading cycle {rec.cycle_id} already exists")
            s.add(
                m.TradingCycle(
                    cycle_id=rec.cycle_id,
                    session_date=rec.session_date,
                    environment=rec.environment,
                    mode=rec.mode,
                    config_version=rec.config_version,
                    run_id=rec.run_id,
                    status="running",
                    started_at=ensure_utc(rec.started_at),
                )
            )

    def get(self, cycle_id: str) -> CycleView | None:
        with self.db.session() as s:
            c = s.get(m.TradingCycle, cycle_id)
            if c is None:
                return None
            return CycleView(
                c.cycle_id, c.session_date, c.environment, c.mode, c.status, c.config_version
            )

    def record_step(
        self,
        cycle_id: str,
        step: str,
        status: str,
        at: datetime,
        detail: str = "",
        payload: Any = None,
    ) -> None:
        if status not in ("done", "refused", "failed", "skipped"):
            raise ValueError(f"invalid step status {status!r}")
        with self.db.session() as s:
            s.add(
                m.CycleStep(
                    cycle_id=cycle_id,
                    step=step,
                    status=status,
                    detail=detail,
                    payload_json=jsonable(payload or {}),
                    at=ensure_utc(at),
                )
            )

    def steps(self, cycle_id: str) -> dict[str, StepView]:
        """Latest record per step of a cycle."""
        with self.db.session() as s:
            rows = s.scalars(
                select(m.CycleStep).where(m.CycleStep.cycle_id == cycle_id).order_by(m.CycleStep.id)
            )
            return {
                r.step: StepView(r.step, r.status, r.detail, dict(r.payload_json), r.at)
                for r in rows
            }

    def load_target(self, decision_id: str) -> TargetPortfolio:
        with self.db.session() as s:
            t = s.get(m.TargetPortfolioRow, decision_id)
            if t is None:
                raise PersistenceError(f"no target portfolio for decision {decision_id}")
            return TargetPortfolio(
                as_of=t.as_of,
                weights={k: Decimal(v) for k, v in t.weights_json.items()},
                decision_id=t.decision_id,
                config_version=t.config_version,
            )

    def last_band(self, before_cycle: str | None = None) -> str | None:
        """Drawdown band of the most recent stored risk decision (hysteresis input)."""
        with self.db.session() as s:
            q = select(m.RiskDecisionRow.drawdown_band).order_by(
                m.RiskDecisionRow.created_at.desc()
            )
            if before_cycle is not None:
                q = q.where(m.RiskDecisionRow.cycle_id != before_cycle)
            return s.scalars(q.limit(1)).first()

    def signal_history(
        self, strategy_ids: Sequence[str], limit: int
    ) -> list[tuple[datetime, dict[str, float]]]:
        """Per completed decision: (data timestamp, {strategy: suggested exposure}), oldest first.

        Only decisions where every requested strategy produced a signal are returned.
        """
        ids = list(strategy_ids)
        with self.db.session() as s:
            rows = s.execute(
                select(
                    m.StrategySignalRow.cycle_id,
                    m.StrategySignalRow.strategy_id,
                    m.StrategySignalRow.data_timestamp,
                    m.StrategySignalRow.suggested_exposure,
                )
                .where(m.StrategySignalRow.strategy_id.in_(ids))
                .order_by(m.StrategySignalRow.data_timestamp, m.StrategySignalRow.id)
            ).all()
        by_cycle: dict[str, tuple[datetime, dict[str, float]]] = {}
        for cycle, sid, ts, exp in rows:
            entry = by_cycle.setdefault(cycle, (ts, {}))
            entry[1][sid] = float(exp)
        complete = [v for v in by_cycle.values() if set(v[1]) == set(ids)]
        return sorted(complete, key=lambda v: v[0])[-limit:]

    def finish(self, cycle_id: str, status: str, at: datetime, detail: str = "") -> None:
        if status not in ("completed", "refused", "failed"):
            raise ValueError(f"invalid final cycle status {status!r}")
        with self.db.session() as s:
            cycle = s.get(m.TradingCycle, cycle_id, with_for_update=True)
            if cycle is None:
                raise PersistenceError(f"unknown trading cycle {cycle_id}")
            if cycle.status != "running":
                raise PersistenceError(f"cycle {cycle_id} already finished ({cycle.status})")
            cycle.status, cycle.finished_at, cycle.detail = status, ensure_utc(at), detail

    def record_preflight(
        self, cycle_id: str, passed: bool, results: list[Any], at: datetime
    ) -> None:
        with self.db.session() as s:
            s.add(
                m.PreflightReportRow(
                    cycle_id=cycle_id,
                    passed=passed,
                    results_json={"results": jsonable(results)},
                    checked_at=ensure_utc(at),
                )
            )

    def record_decision(self, rec: DecisionRecord, config_version: str) -> None:
        """Signals, indicators, ensemble, proposal, risk decision and target - atomically."""
        with self.db.session() as s:
            for ind in rec.indicators:
                s.add(
                    m.IndicatorSnapshot(
                        cycle_id=rec.cycle_id,
                        symbol=ind.symbol,
                        as_of=ensure_utc(ind.as_of),
                        data_timestamp=ensure_utc(ind.data_timestamp),
                        data_snapshot_id=ind.data_snapshot_id,
                        values_json=jsonable(ind.values),
                    )
                )
            for version, sig in rec.signals:
                s.add(
                    m.StrategySignalRow(
                        cycle_id=rec.cycle_id,
                        strategy_version_id=_strategy_version(s, sig.strategy_name, version),
                        strategy_id=sig.strategy_name,
                        timestamp=sig.timestamp,
                        data_timestamp=sig.data_timestamp,
                        direction=sig.direction.value,
                        raw_score=sig.raw_score,
                        normalized_score=sig.normalized_score,
                        confidence=sig.confidence,
                        suggested_exposure=sig.suggested_exposure,
                        reason=sig.reason,
                        indicator_values_json=jsonable(sig.indicator_values),
                    )
                )
            e = rec.ensemble
            s.add(
                m.EnsembleDecisionRow(
                    cycle_id=rec.cycle_id,
                    method=e.method,
                    strategy_weights_json=e.strategy_weights,
                    combined_score=e.combined_score,
                    combined_exposure=e.combined_exposure,
                    regime_json=jsonable(e.regime),
                )
            )
            proposal = m.PortfolioProposalRow(
                cycle_id=rec.cycle_id,
                weights_json=rec.proposal.weights,
                requested_exposure=rec.proposal.requested_exposure,
                rationale=rec.proposal.rationale,
            )
            s.add(proposal)
            s.flush()
            r = rec.risk
            if rec.target.decision_id != r.decision_id:
                raise PersistenceError("target portfolio and risk decision ids differ")
            s.add(
                m.RiskDecisionRow(
                    decision_id=r.decision_id,
                    cycle_id=rec.cycle_id,
                    proposal_id=proposal.id,
                    approved_weights_json=jsonable(r.approved_weights),
                    adjustments_json=jsonable(r.adjustments),
                    drawdown_band=r.band,
                    drawdown=r.drawdown,
                    vol_scale=r.vol_scale,
                    regime=r.regime,
                    blocked_risk_increasing=r.blocked_risk_increasing,
                    flags_json=list(r.flags),
                )
            )
            s.flush()
            t = rec.target
            s.add(
                m.TargetPortfolioRow(
                    decision_id=t.decision_id,
                    as_of=t.as_of,
                    weights_json=jsonable(t.weights),
                    cash_weight=t.cash_weight,
                    net_underlying_exposure=rec.net_underlying_exposure,
                    config_version=config_version,
                )
            )

    def record_account(
        self,
        cycle_id: str,
        equity: Decimal,
        cash: Decimal,
        buying_power: Decimal,
        is_paper: bool,
        at: datetime,
    ) -> None:
        with self.db.session() as s:
            s.add(
                m.AccountSnapshotRow(
                    cycle_id=cycle_id,
                    equity=equity,
                    cash=cash,
                    buying_power=buying_power,
                    is_paper=is_paper,
                    as_of=ensure_utc(at),
                )
            )

    def record_positions(
        self,
        cycle_id: str,
        source: str,
        positions: Iterable[tuple[str, Decimal, Decimal]],
        at: datetime,
    ) -> None:
        if source not in ("broker", "expected"):
            raise ValueError("position source must be 'broker' or 'expected'")
        with self.db.session() as s:
            for symbol, qty, value in positions:
                s.add(
                    m.PositionSnapshot(
                        cycle_id=cycle_id,
                        source=source,
                        symbol=symbol,
                        quantity=qty,
                        market_value=value,
                        as_of=ensure_utc(at),
                    )
                )

    def record_reconciliation(
        self, cycle_id: str, passed: bool, differences: Any, at: datetime
    ) -> None:
        with self.db.session() as s:
            s.add(
                m.ReconciliationReportRow(
                    cycle_id=cycle_id,
                    passed=passed,
                    differences_json={"differences": jsonable(differences)},
                    at=ensure_utc(at),
                )
            )

    def latest_reconciliation(self) -> ReconciliationView | None:
        with self.db.session() as s:
            r = s.scalars(
                select(m.ReconciliationReportRow)
                .order_by(m.ReconciliationReportRow.at.desc(), m.ReconciliationReportRow.id.desc())
                .limit(1)
            ).first()
            if r is None:
                return None
            return ReconciliationView(r.id, r.cycle_id, r.passed, dict(r.differences_json), r.at)

    def record_shadow_order(self, cycle_id: str, req: OrderRequest) -> None:
        with self.db.session() as s:
            s.add(
                m.ShadowOrder(
                    client_order_id=req.client_order_id,
                    cycle_id=cycle_id,
                    decision_id=req.decision_id,
                    symbol=req.symbol,
                    side=req.side.value,
                    quantity=req.quantity,
                    order_type=req.order_type.value,
                    tif=req.time_in_force.value,
                    limit_price=req.limit_price,
                    risk_increasing=req.risk_increasing,
                )
            )


# ================================================================== orders
EXPOSURE_OPEN = ("created", "validated", "submitted", "acknowledged", "partially_filled", "unknown")


class OrderRepository(_Repo):
    def __init__(self, db: Database, guard: TransitionGuard) -> None:
        super().__init__(db)
        self.guard = guard

    def create_intent(self, cycle_id: str, req: OrderRequest, at: datetime) -> None:
        """Durably record an order intent in state CREATED. Returns only after COMMIT.

        Raises ``DatabaseUnavailableError`` if the database cannot be reached and
        ``PersistenceError`` for a duplicate ``client_order_id`` or an unknown
        cycle / risk decision: in every failure case the order must not be sent.
        """
        try:
            with self.db.session() as s:
                s.add(
                    m.OrderIntent(
                        client_order_id=req.client_order_id,
                        cycle_id=cycle_id,
                        decision_id=req.decision_id,
                        symbol=req.symbol,
                        side=req.side.value,
                        quantity=req.quantity,
                        order_type=req.order_type.value,
                        tif=req.time_in_force.value,
                        limit_price=req.limit_price,
                        risk_increasing=req.risk_increasing,
                        state=OrderState.CREATED.value,
                    )
                )
                s.flush()
                s.add(
                    m.OrderEvent(
                        client_order_id=req.client_order_id,
                        from_state=None,
                        to_state=OrderState.CREATED.value,
                        raw_json={},
                        at=ensure_utc(at),
                    )
                )
        except PersistenceError as exc:
            if isinstance(exc.__cause__, IntegrityError):
                raise PersistenceError(
                    f"order intent {req.client_order_id} refused: {exc.__cause__.orig}",
                    hint="duplicate client_order_id or unknown cycle/decision: do not transmit",
                ) from exc
            raise

    def transition(self, client_order_id: str, update: OrderUpdate) -> str:
        """Apply a state change (validated by the injected guard) and log the event."""
        target = OrderState(update.to_state)
        with self.db.session() as s:
            intent = s.get(m.OrderIntent, client_order_id, with_for_update=True)
            if intent is None:
                raise PersistenceError(f"unknown order intent {client_order_id}")
            current = OrderState(intent.state)
            self.guard(current, target)
            intent.state = target.value
            if update.broker_order_id:
                intent.broker_order_id = update.broker_order_id
            s.add(
                m.OrderEvent(
                    client_order_id=client_order_id,
                    from_state=current.value,
                    to_state=target.value,
                    broker_order_id=update.broker_order_id,
                    filled_qty=update.filled_qty,
                    avg_price=update.avg_price,
                    raw_json=jsonable(update.raw),
                    at=ensure_utc(update.at),
                )
            )
            return current.value

    def record_execution(self, rec: ExecutionRecord) -> bool:
        """Idempotent on ``broker_execution_id``: returns False for a duplicate report."""
        slippage = None
        if rec.expected_price:
            slippage = float((rec.price - rec.expected_price) / rec.expected_price * 10_000)
        with self.db.session() as s:
            res = s.execute(
                insert(m.Execution)
                .values(
                    client_order_id=rec.client_order_id,
                    broker_execution_id=rec.broker_execution_id,
                    qty=rec.qty,
                    price=rec.price,
                    fee=rec.fee,
                    at=ensure_utc(rec.at),
                    expected_price=rec.expected_price,
                    slippage_bps=slippage,
                )
                .on_conflict_do_nothing(index_elements=["broker_execution_id"])
                .returning(m.Execution.id)
            )
            return res.first() is not None

    def intents(
        self, *, states: Sequence[str] | None = None, cycle_id: str | None = None
    ) -> list[IntentView]:
        with self.db.session() as s:
            q = select(m.OrderIntent).order_by(
                m.OrderIntent.created_at, m.OrderIntent.client_order_id
            )
            if states is not None:
                q = q.where(m.OrderIntent.state.in_(list(states)))
            if cycle_id is not None:
                q = q.where(m.OrderIntent.cycle_id == cycle_id)
            return [
                IntentView(
                    client_order_id=o.client_order_id,
                    cycle_id=o.cycle_id,
                    decision_id=o.decision_id,
                    symbol=o.symbol,
                    side=o.side,
                    quantity=o.quantity,
                    state=o.state,
                    broker_order_id=o.broker_order_id,
                    risk_increasing=o.risk_increasing,
                )
                for o in s.scalars(q)
            ]

    def next_sequence(self, cycle_id: str, symbol: str, side: str) -> int:
        """Sequence for a new intent of (cycle, symbol, side): allocated from persisted state."""
        with self.db.session() as s:
            n = s.scalar(
                select(func.count())
                .select_from(m.OrderIntent)
                .where(
                    m.OrderIntent.cycle_id == cycle_id,
                    m.OrderIntent.symbol == symbol,
                    m.OrderIntent.side == side,
                )
            )
            return int(n or 0)

    def execution_totals(self, client_order_id: str) -> tuple[Decimal, Decimal]:
        """(filled quantity, filled notional) recorded so far for one order."""
        with self.db.session() as s:
            row = s.execute(
                select(
                    func.coalesce(func.sum(m.Execution.qty), 0),
                    func.coalesce(func.sum(m.Execution.qty * m.Execution.price), 0),
                ).where(m.Execution.client_order_id == client_order_id)
            ).one()
            return Decimal(row[0]), Decimal(row[1])

    def executions_for_cycle(self, cycle_id: str) -> list[tuple[str, str, Decimal]]:
        """(symbol, side, qty) of every recorded execution of the cycle's orders."""
        with self.db.session() as s:
            rows = s.execute(
                select(m.OrderIntent.symbol, m.OrderIntent.side, m.Execution.qty)
                .join(m.Execution, m.Execution.client_order_id == m.OrderIntent.client_order_id)
                .where(m.OrderIntent.cycle_id == cycle_id)
                .order_by(m.Execution.at, m.Execution.id)
            ).all()
            return [(a, b, c) for a, b, c in rows]

    def get_state(self, client_order_id: str) -> str | None:
        with self.db.session() as s:
            intent = s.get(m.OrderIntent, client_order_id)
            return None if intent is None else intent.state

    def open_intents(self) -> list[tuple[str, str, str]]:
        """(client_order_id, symbol, state) of every order that may still change exposure."""
        with self.db.session() as s:
            rows = s.execute(
                select(m.OrderIntent.client_order_id, m.OrderIntent.symbol, m.OrderIntent.state)
                .where(m.OrderIntent.state.in_(EXPOSURE_OPEN))
                .order_by(m.OrderIntent.created_at)
            ).all()
            return [(a, b, c) for a, b, c in rows]


# ================================================================== monitoring
class MonitoringRepository(_Repo):
    def record_system_event(
        self, severity: str, component: str, event: str, context: Any, at: datetime
    ) -> None:
        with self.db.session() as s:
            s.add(
                m.SystemEvent(
                    severity=severity,
                    component=component,
                    event=event,
                    context_json=jsonable(context)
                    if isinstance(context, dict)
                    else {"value": jsonable(context)},
                    at=ensure_utc(at),
                )
            )

    def record_error(
        self,
        component: str,
        exc: BaseException,
        at: datetime,
        cycle_id: str | None = None,
        tb: str = "",
    ) -> None:
        with self.db.session() as s:
            s.add(
                m.ErrorRow(
                    component=component,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    traceback=tb,
                    cycle_id=cycle_id,
                    at=ensure_utc(at),
                )
            )

    def record_kill_switch_event(
        self, engaged: bool, actor: str, reason: str, at: datetime
    ) -> None:
        with self.db.session() as s:
            s.add(m.KillSwitchEvent(engaged=engaged, actor=actor, reason=reason, at=ensure_utc(at)))

    # ------------------------------------------------------------ kill switch (shared)
    def read_kill_switch(self) -> Json | None:
        """The shared kill-switch state, or ``None`` if it was never written."""
        with self.db.session() as s:
            row = s.get(m.KillSwitchStateRow, 1)
            if row is None:
                return None
            return {
                "engaged": row.engaged,
                "actor": row.actor,
                "reason": row.reason,
                "changed_at": row.changed_at,
            }

    def write_kill_switch(self, engaged: bool, actor: str, reason: str, at: datetime) -> None:
        """Set the shared state (upsert of the single row)."""
        values = {
            "id": 1,
            "engaged": engaged,
            "actor": actor,
            "reason": reason,
            "changed_at": ensure_utc(at),
        }
        stmt = insert(m.KillSwitchStateRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={**{k: v for k, v in values.items() if k != "id"}, "updated_at": func.now()},
        )
        with self.db.session() as s:
            s.execute(stmt)

    def record_notification(
        self,
        event_type: str,
        severity: str,
        channel: str,
        delivered: bool,
        at: datetime,
        error: str | None = None,
    ) -> None:
        with self.db.session() as s:
            s.add(
                m.NotificationSent(
                    event_type=event_type,
                    severity=severity,
                    channel=channel,
                    delivered=delivered,
                    error=error,
                    at=ensure_utc(at),
                )
            )

    def equity_history(self, environment: str) -> list[tuple[date, Decimal]]:
        with self.db.session() as s:
            rows = s.execute(
                select(m.DailyPerformance.session_date, m.DailyPerformance.equity)
                .where(m.DailyPerformance.environment == environment)
                .order_by(m.DailyPerformance.session_date)
            ).all()
            return [(a, b) for a, b in rows]

    def upsert_daily_performance(
        self,
        session_date: date,
        environment: str,
        equity: Decimal,
        pnl: Decimal,
        daily_return: float,
        drawdown: float,
        turnover: float,
        benchmarks: dict[str, float],
    ) -> None:
        values = {
            "equity": equity,
            "pnl": pnl,
            "daily_return": daily_return,
            "drawdown": drawdown,
            "turnover": turnover,
            "benchmark_returns_json": benchmarks,
        }
        with self.db.session() as s:
            s.execute(
                insert(m.DailyPerformance)
                .values(session_date=session_date, environment=environment, **values)
                .on_conflict_do_update(constraint="uq_daily_performance_session", set_=values)
            )
