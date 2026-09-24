"""SQLAlchemy models for the audit database (``docs/DATABASE.md``).

Conventions:

* every timestamp is ``timestamptz`` (UTC); every row has ``created_at``;
* money, prices and quantities are exact ``Numeric``; weights ``Numeric(12, 6)``;
* structured payloads are ``JSONB``;
* append-only tables (``APPEND_ONLY``) reject UPDATE and DELETE with a database
  trigger - corrections are new rows;
* ``order_intents.state`` may only change along the order state machine's safe
  paths; a database trigger additionally refuses leaving a terminal state and
  UNKNOWN -> CREATED/VALIDATED/SUBMITTED (never resubmit an unknown order).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TS = DateTime(timezone=True)
MONEY = Numeric(20, 2)
PRICE = Numeric(20, 6)
QTY = Numeric(24, 6)
WEIGHT = Numeric(12, 6)
Json = dict[str, Any]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _id() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def _created() -> Mapped[datetime]:
    return mapped_column(TS, server_default=func.now(), nullable=False)


# ================================================================== reference & versioning
class ConfigVersion(Base):
    __tablename__ = "config_versions"
    config_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    environment: Mapped[str] = mapped_column(String(16))
    digest: Mapped[str] = mapped_column(String(64))
    resolved_json: Mapped[Json]  # secrets are never part of the resolved settings
    source_hashes: Mapped[Json]
    created_at: Mapped[datetime] = _created()


class InstrumentRow(Base):
    __tablename__ = "instruments"
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    leverage: Mapped[float] = mapped_column(Float)
    underlying: Mapped[str | None] = mapped_column(String(16))
    tradeable: Mapped[bool] = mapped_column(Boolean)
    inception_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = _created()


class StrategyVersionRow(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_strategy_versions_strategy_version"),
    )
    id: Mapped[int] = _id()
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(96))  # implementation@code#paramhash
    implementation: Mapped[str] = mapped_column(String(64))
    family: Mapped[str] = mapped_column(String(32))
    params_json: Mapped[Json]
    code_hash: Mapped[str] = mapped_column(String(64))
    research_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()


class StrategyLifecycleEvent(Base):
    __tablename__ = "strategy_lifecycle_events"
    id: Mapped[int] = _id()
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    from_state: Mapped[str] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(128))
    actor_kind: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class ResearchRun(Base):
    __tablename__ = "research_runs"
    id: Mapped[int] = _id()
    kind: Mapped[str] = mapped_column(String(32))  # trial / research / backtest / monte_carlo ...
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    trial_id: Mapped[str | None] = mapped_column(String(32), index=True)
    strategy_version_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_versions.id"))
    config_version: Mapped[str | None] = mapped_column(String(64))
    data_fingerprint: Mapped[str | None] = mapped_column(String(64))
    data_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("data_snapshots.id"))
    metrics_json: Mapped[Json]
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


# ================================================================== market data metadata
class DataSnapshot(Base):
    __tablename__ = "data_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "symbol",
            "frequency",
            "adjustment",
            "content_hash",
            name="uq_data_snapshots_content",
        ),
    )
    id: Mapped[int] = _id()
    provider: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(16))
    frequency: Mapped[str] = mapped_column(String(8))
    adjustment: Mapped[str] = mapped_column(String(16))
    start: Mapped[datetime] = mapped_column(TS)
    end: Mapped[datetime] = mapped_column(TS)
    row_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    is_synthetic: Mapped[bool] = mapped_column(Boolean)
    parquet_uri: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class DataQualityReport(Base):
    __tablename__ = "data_quality_reports"
    id: Mapped[int] = _id()
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("data_snapshots.id"), index=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    issues_json: Mapped[Json]
    checked_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class CorporateActionRow(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint("symbol", "ex_date", "type", "source", name="uq_corporate_actions_action"),
    )
    id: Mapped[int] = _id()
    symbol: Mapped[str] = mapped_column(String(16))
    ex_date: Mapped[date] = mapped_column(Date)
    type: Mapped[str] = mapped_column(String(16))
    ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    source: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created()


# ================================================================== decision audit trail
class TradingCycle(Base):
    __tablename__ = "trading_cycles"
    cycle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    environment: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16))
    config_version: Mapped[str] = mapped_column(ForeignKey("config_versions.config_version"))
    run_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # running / completed / refused / failed
    started_at: Mapped[datetime] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()


def _cycle_fk() -> Mapped[str]:
    return mapped_column(ForeignKey("trading_cycles.cycle_id"), index=True)


class PreflightReportRow(Base):
    __tablename__ = "preflight_reports"
    id: Mapped[int] = _id()
    cycle_id: Mapped[str] = _cycle_fk()
    passed: Mapped[bool] = mapped_column(Boolean)
    results_json: Mapped[Json]
    checked_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class IndicatorSnapshot(Base):
    __tablename__ = "indicator_snapshots"
    id: Mapped[int] = _id()
    cycle_id: Mapped[str] = _cycle_fk()
    symbol: Mapped[str] = mapped_column(String(16))
    as_of: Mapped[datetime] = mapped_column(TS)
    data_timestamp: Mapped[datetime] = mapped_column(TS)
    data_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("data_snapshots.id"))
    values_json: Mapped[Json]
    created_at: Mapped[datetime] = _created()


class StrategySignalRow(Base):
    __tablename__ = "strategy_signals"
    __table_args__ = (CheckConstraint("data_timestamp <= timestamp", name="no_lookahead"),)
    id: Mapped[int] = _id()
    cycle_id: Mapped[str] = _cycle_fk()
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    strategy_id: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(TS)
    data_timestamp: Mapped[datetime] = mapped_column(TS)
    direction: Mapped[str] = mapped_column(String(8))
    raw_score: Mapped[float] = mapped_column(Float)
    normalized_score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    suggested_exposure: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    indicator_values_json: Mapped[Json]
    created_at: Mapped[datetime] = _created()


class EnsembleDecisionRow(Base):
    __tablename__ = "ensemble_decisions"
    id: Mapped[int] = _id()
    cycle_id: Mapped[str] = _cycle_fk()
    method: Mapped[str] = mapped_column(String(32))
    strategy_weights_json: Mapped[Json]
    combined_score: Mapped[float] = mapped_column(Float)
    combined_exposure: Mapped[float] = mapped_column(Float)
    regime_json: Mapped[Json]
    created_at: Mapped[datetime] = _created()


class PortfolioProposalRow(Base):
    __tablename__ = "portfolio_proposals"
    id: Mapped[int] = _id()
    cycle_id: Mapped[str] = _cycle_fk()
    weights_json: Mapped[Json]
    requested_exposure: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"
    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cycle_id: Mapped[str] = _cycle_fk()
    proposal_id: Mapped[int] = mapped_column(ForeignKey("portfolio_proposals.id"))
    approved_weights_json: Mapped[Json]
    adjustments_json: Mapped[list[Any]]
    drawdown_band: Mapped[str] = mapped_column(String(32))
    drawdown: Mapped[float] = mapped_column(Float)
    vol_scale: Mapped[float] = mapped_column(Float)
    regime: Mapped[str | None] = mapped_column(String(16))
    blocked_risk_increasing: Mapped[bool] = mapped_column(Boolean)
    flags_json: Mapped[list[Any]]
    created_at: Mapped[datetime] = _created()


class TargetPortfolioRow(Base):
    __tablename__ = "target_portfolios"
    decision_id: Mapped[str] = mapped_column(
        ForeignKey("risk_decisions.decision_id"), primary_key=True
    )
    as_of: Mapped[datetime] = mapped_column(TS)
    weights_json: Mapped[Json]
    cash_weight: Mapped[Decimal] = mapped_column(WEIGHT)
    net_underlying_exposure: Mapped[float] = mapped_column(Float)
    config_version: Mapped[str] = mapped_column(ForeignKey("config_versions.config_version"))
    created_at: Mapped[datetime] = _created()


# ================================================================== execution
ORDER_STATES = (
    "created",
    "validated",
    "submitted",
    "acknowledged",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "unknown",
)


class OrderIntent(Base):
    """Written (and committed) BEFORE the order is transmitted to any broker."""

    __tablename__ = "order_intents"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="positive_qty"),
        CheckConstraint(f"state IN {ORDER_STATES}", name="state"),
        CheckConstraint("side IN ('buy', 'sell')", name="side"),
    )
    client_order_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    cycle_id: Mapped[str] = _cycle_fk()
    decision_id: Mapped[str] = mapped_column(ForeignKey("risk_decisions.decision_id"))
    symbol: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    order_type: Mapped[str] = mapped_column(String(16))
    tif: Mapped[str] = mapped_column(String(8))
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    risk_increasing: Mapped[bool] = mapped_column(Boolean)
    state: Mapped[str] = mapped_column(String(20))
    broker_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class OrderEvent(Base):
    __tablename__ = "order_events"
    id: Mapped[int] = _id()
    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("order_intents.client_order_id"), index=True
    )
    from_state: Mapped[str | None] = mapped_column(String(20))
    to_state: Mapped[str] = mapped_column(String(20))
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    filled_qty: Mapped[Decimal | None] = mapped_column(QTY)
    avg_price: Mapped[Decimal | None] = mapped_column(PRICE)
    raw_json: Mapped[Json]
    at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class Execution(Base):
    __tablename__ = "executions"
    id: Mapped[int] = _id()
    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("order_intents.client_order_id"), index=True
    )
    broker_execution_id: Mapped[str] = mapped_column(String(64), unique=True)
    qty: Mapped[Decimal] = mapped_column(QTY)
    price: Mapped[Decimal] = mapped_column(PRICE)
    fee: Mapped[Decimal] = mapped_column(MONEY)
    at: Mapped[datetime] = mapped_column(TS)
    expected_price: Mapped[Decimal | None] = mapped_column(PRICE)
    slippage_bps: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = _created()


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"
    id: Mapped[int] = _id()
    cycle_id: Mapped[str] = _cycle_fk()
    source: Mapped[str] = mapped_column(String(16))  # broker / expected
    symbol: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    market_value: Mapped[Decimal] = mapped_column(MONEY)
    as_of: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class AccountSnapshotRow(Base):
    __tablename__ = "account_snapshots"
    id: Mapped[int] = _id()
    cycle_id: Mapped[str] = _cycle_fk()
    equity: Mapped[Decimal] = mapped_column(MONEY)
    cash: Mapped[Decimal] = mapped_column(MONEY)
    buying_power: Mapped[Decimal] = mapped_column(MONEY)
    is_paper: Mapped[bool] = mapped_column(Boolean)
    as_of: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class ReconciliationReportRow(Base):
    __tablename__ = "reconciliation_reports"
    id: Mapped[int] = _id()
    cycle_id: Mapped[str] = _cycle_fk()
    passed: Mapped[bool] = mapped_column(Boolean)
    differences_json: Mapped[Json]
    at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class ShadowOrder(Base):
    """Same shape as an order intent; never transmitted (shadow mode)."""

    __tablename__ = "shadow_orders"
    id: Mapped[int] = _id()
    client_order_id: Mapped[str] = mapped_column(String(48), unique=True)
    cycle_id: Mapped[str] = _cycle_fk()
    decision_id: Mapped[str] = mapped_column(ForeignKey("risk_decisions.decision_id"))
    symbol: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    order_type: Mapped[str] = mapped_column(String(16))
    tif: Mapped[str] = mapped_column(String(8))
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    risk_increasing: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = _created()


# ================================================================== monitoring
class DailyPerformance(Base):
    __tablename__ = "daily_performance"
    __table_args__ = (
        UniqueConstraint("session_date", "environment", name="uq_daily_performance_session"),
    )
    id: Mapped[int] = _id()
    session_date: Mapped[date] = mapped_column(Date)
    environment: Mapped[str] = mapped_column(String(16))
    equity: Mapped[Decimal] = mapped_column(MONEY)
    pnl: Mapped[Decimal] = mapped_column(MONEY)
    daily_return: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float)
    turnover: Mapped[float] = mapped_column(Float)
    benchmark_returns_json: Mapped[Json]
    created_at: Mapped[datetime] = _created()


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = _id()
    severity: Mapped[str] = mapped_column(String(16))
    component: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(128))
    context_json: Mapped[Json]
    at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class ErrorRow(Base):
    __tablename__ = "errors"
    id: Mapped[int] = _id()
    component: Mapped[str] = mapped_column(String(64))
    error_type: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    traceback: Mapped[str] = mapped_column(Text, default="")
    cycle_id: Mapped[str | None] = mapped_column(ForeignKey("trading_cycles.cycle_id"))
    at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class KillSwitchEvent(Base):
    __tablename__ = "kill_switch_events"
    id: Mapped[int] = _id()
    engaged: Mapped[bool] = mapped_column(Boolean)
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class NotificationSent(Base):
    __tablename__ = "notifications_sent"
    id: Mapped[int] = _id()
    event_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    channel: Mapped[str] = mapped_column(String(32))
    delivered: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


class CycleStep(Base):
    """A completed (or refused / failed) step of a trading cycle: lets a restart resume."""

    __tablename__ = "cycle_steps"
    id: Mapped[int] = _id()
    cycle_id: Mapped[str] = mapped_column(ForeignKey("trading_cycles.cycle_id"), index=True)
    step: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # done / refused / failed / skipped
    detail: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[Json]
    at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = _created()


Index("ix_order_events_at", OrderEvent.at)

#: Tables whose rows can never be updated or deleted (enforced by a trigger).
APPEND_ONLY = (
    "config_versions",
    "strategy_lifecycle_events",
    "research_runs",
    "data_quality_reports",
    "preflight_reports",
    "indicator_snapshots",
    "strategy_signals",
    "ensemble_decisions",
    "portfolio_proposals",
    "risk_decisions",
    "target_portfolios",
    "order_events",
    "executions",
    "position_snapshots",
    "account_snapshots",
    "reconciliation_reports",
    "shadow_orders",
    "system_events",
    "errors",
    "kill_switch_events",
    "notifications_sent",
    "cycle_steps",
)
