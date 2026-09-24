"""Map platform objects to audit records, and build the guarded order repository.

The persistence layer knows only ``core``; this module (trading layer, allowed
to use quant *outputs*) converts M7 decisions, strategy versions, research
evidence and configuration into persistence records.
"""

from __future__ import annotations

from collections.abc import Sequence

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.schema import redact
from adaptive_quant.core.models import Instrument, StrategySignal
from adaptive_quant.governance.lifecycle import LifecycleTransition
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.records import (
    ConfigRecord,
    DecisionRecord,
    EnsembleRecord,
    LifecycleEventRecord,
    ProposalRecord,
    ResearchRunRecord,
    RiskRecord,
    StrategyVersionRecord,
)
from adaptive_quant.persistence.repositories import OrderRepository
from adaptive_quant.quant.portfolio.manager import PortfolioDecision
from adaptive_quant.quant.research.trials import TrialRecord
from adaptive_quant.quant.strategies.catalog import StrategyVersion
from adaptive_quant.trading.orders.state_machine import assert_transition


def order_repository(db: Database) -> OrderRepository:
    """Order persistence whose every state change is checked by the M1 state machine."""
    return OrderRepository(db, assert_transition)


def config_record(loaded: LoadedConfig) -> ConfigRecord:
    s = loaded.settings
    return ConfigRecord(
        config_version=loaded.config_version,
        environment=s.app.environment.value,
        digest=loaded.digest,
        resolved=redact(s.model_dump(mode="json")),
        source_hashes={src.path.name: src.sha256 for src in loaded.sources},
    )


def strategy_version_record(v: StrategyVersion) -> StrategyVersionRecord:
    return StrategyVersionRecord(
        strategy_id=v.strategy_id,
        version=v.version_id,
        implementation=v.implementation,
        family=v.family.value,
        params=dict(v.params),
        code_hash=v.code_version,
        notes=v.notes,
    )


def lifecycle_record(t: LifecycleTransition) -> LifecycleEventRecord:
    return LifecycleEventRecord(
        strategy_id=t.strategy_id,
        version=t.strategy_version,
        from_state=t.from_state.value,
        to_state=t.to_state.value,
        actor_id=t.actor.id,
        actor_kind=t.actor.kind.value,
        reason=t.reason,
        at=t.at,
    )


def trial_records(
    trials: Sequence[TrialRecord], config_version: str | None = None
) -> list[ResearchRunRecord]:
    return [
        ResearchRunRecord(
            kind=f"trial:{t.purpose}",
            run_id=t.run_id,
            trial_id=t.trial_id,
            strategy_id=t.strategy_id,
            version=t.version_id,
            config_version=config_version,
            data_fingerprint=t.data_fingerprint,
            metrics={
                "params": t.params,
                "start": t.start,
                "end": t.end,
                "sessions": t.sessions,
                "sharpe_annual": t.sharpe_annual,
                "synthetic_sessions": t.synthetic_sessions,
                "error": t.error,
            },
        )
        for t in trials
    ]


def decision_record(
    cycle_id: str,
    decision_id: str,
    config_version: str,
    decision: PortfolioDecision,
    signals: Sequence[StrategySignal],
    instruments: dict[str, Instrument],
) -> DecisionRecord:
    """One M7 decision chain -> the audit record written atomically by CycleRepository."""
    risk = decision.risk
    ens = decision.ensemble
    target = risk.to_target(decision_id, config_version)
    return DecisionRecord(
        cycle_id=cycle_id,
        signals=[(s.strategy_version, s) for s in signals],
        ensemble=EnsembleRecord(
            method=ens.weights.method,
            strategy_weights=dict(ens.weights.weights),
            combined_score=ens.score,
            combined_exposure=ens.exposure,
            regime={
                "regime": risk.regime,
                "clusters": [list(c) for c in ens.weights.clusters],
                "notes": list(ens.weights.adjustments),
            },
        ),
        proposal=ProposalRecord(
            weights=dict(decision.proposal.weights),
            requested_exposure=decision.proposal.requested_exposure,
            rationale=decision.proposal.rationale,
        ),
        risk=RiskRecord(
            decision_id=decision_id,
            approved_weights=dict(risk.weights),
            adjustments=[
                {"rule": a.rule, "before": a.before, "after": a.after, "reason": a.reason}
                for a in risk.adjustments
            ],
            band=risk.band,
            drawdown=risk.drawdown,
            vol_scale=risk.vol_scale,
            regime=risk.regime,
            blocked_risk_increasing=risk.risk_increasing_blocked,
            flags=list(risk.flags),
        ),
        target=target,
        net_underlying_exposure=target.net_underlying_exposure(instruments),
    )
