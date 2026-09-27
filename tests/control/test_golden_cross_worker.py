"""A browser-queued golden_death_cross comparison through PostgreSQL and the worker,
with runtime mode PAPER and every broker path forced to fail."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from adaptive_quant.api.app import create_app
from adaptive_quant.api.services import ApiServices
from adaptive_quant.config.loader import load_config
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database
from adaptive_quant.quant.data.calendar import nyse_calendar
from adaptive_quant.trading.safety.kill_switch import FileKillSwitchStore, KillSwitch
from adaptive_quant.worker.runner import Worker
from tests.api.conftest import AUTH, TOKEN
from tests.control.conftest import QNOW

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
COMPARE = [
    "golden_death_cross",
    "baseline_buy_hold",
    "st_ema_cross",
    "it_ma_stack",
    "ltt_sma_distance",
]


def app(config_dir: Path, db: Database, tmp: Path) -> TestClient:
    loaded = load_config("development", config_dir=config_dir)
    clock = FrozenClock(QNOW)
    ks = KillSwitch(FileKillSwitchStore(tmp / "ks.json", tmp / "ks.jsonl"), clock)
    return TestClient(
        create_app(ApiServices(loaded, SecretStr(TOKEN), ks, clock, nyse_calendar(), db))
    )


def test_independent_comparison_from_the_browser_in_paper_mode(
    qqq_project: Path, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adaptive_quant.trading.brokers import alpaca, factory
    from adaptive_quant.trading.orders import manager

    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("a backtest touched a broker")

    monkeypatch.setattr(factory, "build_broker", forbidden)
    monkeypatch.setattr(alpaca.AlpacaPaperBroker, "submit_order", forbidden)
    monkeypatch.setattr(manager.OrderManager, "execute", forbidden)
    repo = ControlRepository(db)
    repo.save_runtime_config(
        expected_revision=0,
        overlay={"trading.mode": "paper"},
        strategy_overrides={},
        actor="op",
        now=QNOW,
        changes=[],
        version_before="a",
        version_after="b",
    )
    api = app(qqq_project, db, tmp_path)
    r = api.post(
        "/api/v1/jobs/backtest",
        headers=AUTH,
        json={
            "actor": "ann",
            "params": {
                "strategies": COMPARE,
                "run_mode": "independent",
                "source": "file",
                "initial_capital": 100000,
                "strategy_params": {
                    "golden_death_cross": {"fast_period": 50, "slow_period": 200, "ma_type": "SMA"}
                },
            },
        },
    )
    assert r.status_code == 200, r.text
    jid = r.json()["job"]["id"]
    loaded = load_config("development", config_dir=qqq_project)
    Worker(
        loaded, Secrets(), FrozenClock(QNOW), db, nyse_calendar(), environ={}, wid="w"
    ).run_once()
    # a freshly started API (page refresh / redeploy) reads the stored result
    view = app(qqq_project, db, tmp_path).get(f"/api/v1/backtests/runs/{jid}", headers=AUTH).json()
    assert view["job"]["status"] == "succeeded", view["job"]["error"]
    s = view["run"]["summary"]
    assert s["mode"] == "independent" and [c["strategy"] for c in s["comparison"]] == COMPARE
    assert len({(r_["curve"][0]["date"], r_["curve"][-1]["date"]) for r_ in s["runs"]}) == 1
    gdc = s["runs"][0]["crossover"]["golden_death_cross"]
    assert gdc["classic"] and gdc["first_signal"]
    assert repo.get_state("scheduler") is None  # nothing started automation
