"""Builders for persistence tests (generated values only)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np

from adaptive_quant.core.enums import OrderSide
from adaptive_quant.core.ids import client_order_id
from adaptive_quant.core.models import OrderRequest, StrategySignal, direction_for_score
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.records import ConfigRecord, CycleRecord, DecisionRecord
from adaptive_quant.persistence.repositories import CycleRepository, ReferenceRepository
from adaptive_quant.quant.ensemble.engine import EnsembleEngine, Member
from adaptive_quant.quant.portfolio.manager import PortfolioManager
from adaptive_quant.quant.portfolio.policy import AllocationPolicy
from adaptive_quant.quant.risk.engine import RiskEngine
from adaptive_quant.quant.risk.models import RiskContext
from adaptive_quant.quant.strategies.catalog import StrategyCatalog
from adaptive_quant.trading.audit import decision_record, strategy_version_record
from tests.unit.backtest.helpers import SETTINGS

NOW = datetime(2024, 1, 2, 20, 45, tzinfo=UTC)
CYCLE = "cyc-paper-2024-01-02"
CFG = "paper-test0001"
STRATS = ("ltt_sma_distance", "st_ema_cross")


def config() -> ConfigRecord:
    return ConfigRecord(
        CFG, "paper", "d" * 16, {"trading": {"mode": "paper"}}, {"base.yaml": "a" * 64}
    )


def signals() -> list[StrategySignal]:
    cat = StrategyCatalog.from_config(SETTINGS.strategies)
    out = []
    for sid, score, exp in ((STRATS[0], 0.6, 1.8), (STRATS[1], -0.2, 0.0)):
        out.append(
            StrategySignal(
                strategy_name=sid,
                strategy_version=cat.get(sid).version.version_id,
                timestamp=NOW,
                data_timestamp=datetime(2024, 1, 1, 21, tzinfo=UTC),
                direction=direction_for_score(score),
                raw_score=score,
                normalized_score=score,
                confidence=0.7,
                suggested_exposure=exp,
                reason=f"{sid} test reason",
                indicator_values={"x": 1.5, "y": None},
            )
        )
    return out


def seed(db: Database) -> DecisionRecord:
    """Config, instruments, strategy versions, a running cycle and a real M7 decision."""
    ref = ReferenceRepository(db)
    ref.record_config(config())
    ref.sync_instruments(SETTINGS.universe.instruments)
    cat = StrategyCatalog.from_config(SETTINGS.strategies)
    for sid in STRATS:
        ref.ensure_strategy_version(strategy_version_record(cat.get(sid).version))
    CycleRepository(db).start(
        CycleRecord(CYCLE, date(2024, 1, 2), "paper", "paper", CFG, "run-1", NOW)
    )
    sigs = signals()
    manager = PortfolioManager(
        EnsembleEngine(
            [Member(s, cat.get(s).version.family) for s in STRATS], SETTINGS.strategies.ensemble
        ),
        AllocationPolicy(),
        RiskEngine(SETTINGS.risk, SETTINGS.universe.by_symbol),
    )
    ctx = RiskContext(
        as_of=NOW,
        equity=100_000.0,
        peak_equity=112_000.0,  # 10.7% drawdown: caution band
        previous_equity=100_500.0,
        current_weights={"QQQ": 0.5},
        underlying_returns=np.random.default_rng(0).normal(0, 0.012, 400),
    )
    decision = manager.decide(sigs, np.zeros((0, 2)), ctx)
    rec = decision_record(CYCLE, "dec-1", CFG, decision, sigs, SETTINGS.universe.by_symbol)
    CycleRepository(db).record_decision(rec, CFG)
    return rec


def order(
    symbol: str = "TQQQ", seq: int = 0, decision: str = "dec-1", qty: str = "10"
) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id(CYCLE, symbol, OrderSide.BUY, seq),
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=Decimal(qty),
        risk_increasing=True,
        decision_id=decision,
    )
