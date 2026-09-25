"""A complete, in-memory-data trading-cycle rig on real PostgreSQL (generated prices only)."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from adaptive_quant.config.loader import load_config
from adaptive_quant.core.clock import MARKET_TZ, FrozenClock
from adaptive_quant.core.enums import Severity, TradingMode
from adaptive_quant.core.errors import DataQualityError, MissingDataError
from adaptive_quant.core.models import OrderRequest, OrderSnapshot
from adaptive_quant.notifications.base import Notification, NotificationRouter, Notifier
from adaptive_quant.persistence.db import Database
from adaptive_quant.quant.data.factory import build_validator
from adaptive_quant.quant.strategies.catalog import StrategyCatalog
from adaptive_quant.trading.brokers.simulated import SimulatedBroker
from adaptive_quant.trading.safety.kill_switch import FileKillSwitchStore, KillSwitch
from adaptive_quant.trading.scheduler.cycle import CycleDeps
from tests.conftest import REPO_CONFIG
from tests.data_helpers import calendar
from tests.unit.backtest.helpers import random_frames

LOADED = load_config("development", config_dir=REPO_CONFIG)
STRATS = ("st_ema_cross", "it_ma_stack")


def et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=MARKET_TZ)


class Capture(Notifier):
    name = "capture"

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)

    def events(self) -> list[str]:
        return [n.event.value for n in self.sent]


class MemoryData:
    def __init__(self, frames: dict[str, pd.DataFrame], fail: bool = False) -> None:
        self._frames = frames
        self.fail = fail

    def refresh(self) -> list[str]:
        if self.fail:
            raise DataQualityError("provider unreachable (injected)")
        return ["in-memory data"]

    def frames(self) -> dict[str, pd.DataFrame]:
        return dict(self._frames)

    def bars(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._frames:
            raise MissingDataError(f"no {symbol}")
        return self._frames[symbol]


class SpyBroker(SimulatedBroker):
    """Shadow-mode spy: any transmission attempt fails the test."""

    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        raise AssertionError(f"submit_order called in shadow mode for {request.client_order_id}")


def build(
    db: Database,
    tmp: Path,
    session: date,
    *,
    mode: TradingMode = TradingMode.PAPER,
    data_end: date | None = None,
    broker_cls: type[SimulatedBroker] = SimulatedBroker,
    data_fail: bool = False,
    **broker_kw: Any,
) -> tuple[CycleDeps, SimulatedBroker, Capture, FrozenClock]:
    cal = calendar()
    end = data_end or cal.previous_session(session).date
    frames = random_frames(11, vol=0.01, start=date(2021, 1, 4), end=end)
    clock = FrozenClock(et(session, 9, 0))
    prices = {s: Decimal(str(round(float(f["close"].iloc[-1]), 6))) for s, f in frames.items()}
    broker = broker_cls(clock, prices, **broker_kw)
    settings = LOADED.settings.model_copy(
        update={
            "trading": LOADED.settings.trading.model_copy(
                update={"mode": mode, "allow_fractional_shares": False}
            )
        }
    )
    loaded = dataclasses.replace(LOADED, settings=settings)
    cat = StrategyCatalog.from_config(settings.strategies)
    ks = KillSwitch(FileKillSwitchStore(tmp / "ks.json", tmp / "ks_audit.jsonl"), clock)
    ks.release("tester", "test rig starts released", "RE-ENABLE TRADING")
    capture = Capture()
    deps = CycleDeps(
        settings=settings,
        config_version=loaded.config_version,
        config_record_source=loaded,
        calendar=cal,
        clock=clock,
        db=db,
        broker=broker,
        data=MemoryData(frames, fail=data_fail),
        strategies=[cat.get(s).strategy for s in STRATS],
        versions={s: cat.get(s).version for s in STRATS},
        kill_switch=ks,
        validator=build_validator(loaded, cal),
        notifier=NotificationRouter([capture], Severity.INFO),
    )
    return deps, broker, capture, clock
