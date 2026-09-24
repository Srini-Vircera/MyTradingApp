"""Plain records the repositories accept (the persistence layer depends only on ``core``).

Callers in higher layers map their objects to these (see
``adaptive_quant.trading.audit_mapping``). All timestamps must be timezone-aware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from adaptive_quant.core.models import StrategySignal, TargetPortfolio

Json = dict[str, Any]


@dataclass(frozen=True)
class ConfigRecord:
    config_version: str
    environment: str
    digest: str
    resolved: Json  # already redacted by the caller; secrets are never in settings
    source_hashes: dict[str, str]


@dataclass(frozen=True)
class StrategyVersionRecord:
    strategy_id: str
    version: str
    implementation: str
    family: str
    params: Json
    code_hash: str
    notes: str = ""


@dataclass(frozen=True)
class LifecycleEventRecord:
    strategy_id: str
    version: str
    from_state: str
    to_state: str
    actor_id: str
    actor_kind: str
    reason: str
    at: datetime


@dataclass(frozen=True)
class ResearchRunRecord:
    kind: str
    run_id: str
    metrics: Json
    trial_id: str | None = None
    strategy_id: str | None = None
    version: str | None = None
    config_version: str | None = None
    data_fingerprint: str | None = None
    artifact_uri: str | None = None


@dataclass(frozen=True)
class DataSnapshotRecord:
    provider: str
    symbol: str
    frequency: str
    adjustment: str
    start: datetime
    end: datetime
    row_count: int
    content_hash: str
    is_synthetic: bool
    parquet_uri: str
    fetched_at: datetime


@dataclass(frozen=True)
class CycleRecord:
    cycle_id: str
    session_date: date
    environment: str
    mode: str
    config_version: str
    run_id: str
    started_at: datetime


@dataclass(frozen=True)
class IndicatorSnapshotRecord:
    symbol: str
    as_of: datetime
    data_timestamp: datetime
    values: dict[str, float | None]
    data_snapshot_id: int | None = None


@dataclass(frozen=True)
class EnsembleRecord:
    method: str
    strategy_weights: dict[str, float]
    combined_score: float
    combined_exposure: float
    regime: Json = field(default_factory=dict)


@dataclass(frozen=True)
class ProposalRecord:
    weights: dict[str, float]
    requested_exposure: float
    rationale: str


@dataclass(frozen=True)
class RiskRecord:
    decision_id: str
    approved_weights: dict[str, Decimal]
    adjustments: list[Json]  # rule, before, after, reason
    band: str
    drawdown: float
    vol_scale: float
    regime: str | None
    blocked_risk_increasing: bool
    flags: list[str]


@dataclass(frozen=True)
class DecisionRecord:
    """One cycle's full decision chain, written in a single transaction."""

    cycle_id: str
    signals: list[tuple[str, StrategySignal]]  # (strategy version, signal)
    ensemble: EnsembleRecord
    proposal: ProposalRecord
    risk: RiskRecord
    target: TargetPortfolio
    net_underlying_exposure: float
    indicators: list[IndicatorSnapshotRecord] = field(default_factory=list)


@dataclass(frozen=True)
class OrderUpdate:
    """A state change reported by the broker (or decided locally)."""

    to_state: str
    at: datetime
    broker_order_id: str | None = None
    filled_qty: Decimal | None = None
    avg_price: Decimal | None = None
    raw: Json = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionRecord:
    client_order_id: str
    broker_execution_id: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    at: datetime
    expected_price: Decimal | None = None


@dataclass(frozen=True)
class IntentView:
    """A stored order intent (read model)."""

    client_order_id: str
    cycle_id: str
    decision_id: str
    symbol: str
    side: str
    quantity: Decimal
    state: str
    broker_order_id: str | None
    risk_increasing: bool


@dataclass(frozen=True)
class ReconciliationView:
    id: int
    cycle_id: str
    passed: bool
    differences: Json
    at: datetime
