"""Browser path, API -> PostgreSQL -> worker -> PostgreSQL -> API, with no shared volume.

A fresh project with no market data: upload the QQQ CSV through the API (the
API never writes files), the worker imports it, then a QQQ ``baseline_buy_hold``
backtest is queued through the API, executed by the worker and read back.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from adaptive_quant.api.app import create_app
from adaptive_quant.api.services import ApiServices
from adaptive_quant.config.loader import load_config
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.persistence.db import Database
from adaptive_quant.quant.data.calendar import nyse_calendar
from adaptive_quant.trading.safety.kill_switch import FileKillSwitchStore, KillSwitch
from adaptive_quant.worker.runner import Worker
from tests.api.conftest import AUTH, TOKEN
from tests.conftest import REPO_CONFIG
from tests.control.conftest import QE, QNOW, QS
from tests.data_helpers import daily_bars

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
ACTOR = "Jane Operator"


def qqq_csv() -> bytes:
    """Same layout as the operator's real file: date,open,high,low,close,volume,div,split."""
    df = daily_bars(QS, QE, start_price=100, vol=0.012, seed=11).reset_index()
    df.insert(0, "date", df.pop("timestamp").dt.tz_convert("America/New_York").dt.date)
    df = df.drop(columns=[c for c in ("is_synthetic",) if c in df.columns])
    return df.assign(div=0.0, split=1.0).to_csv(index=False).encode()


def test_upload_import_backtest_from_the_browser(tmp_path: Path, db: Database) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, config_dir)
    loaded = load_config("development", config_dir=config_dir)
    clock = FrozenClock(QNOW)
    api_dir = tmp_path / "api-only"  # the API's own (tiny) state: kill-switch file only
    api_dir.mkdir()
    ks = KillSwitch(FileKillSwitchStore(api_dir / "ks.json", api_dir / "ks.jsonl"), clock)
    api = TestClient(
        create_app(ApiServices(loaded, SecretStr(TOKEN), ks, clock, nyse_calendar(), db))
    )
    worker = Worker(loaded, Secrets(), clock, db, nyse_calendar(), environ={}, wid="w")
    worker.startup()
    assert api.get("/api/v1/data/datasets", headers=AUTH).json()["datasets"] == []

    r = api.post(
        "/api/v1/data/uploads",
        params={"symbol": "QQQ", "actor": ACTOR, "filename": "QQQ.csv"},
        content=qqq_csv(),
        headers={**AUTH, "Content-Type": "text/csv"},
    )
    assert r.status_code == 200 and r.json()["ok"], r.text
    up = r.json()["detail"]["upload"]
    assert any("div" in w for w in up["preview"]["warnings"])  # ignored columns are explained
    assert not (config_dir.parent / "var/data/import/QQQ.csv").exists()  # nothing written yet

    job = api.post(
        f"/api/v1/data/uploads/{up['id']}/import", headers=AUTH, json={"actor": ACTOR}
    ).json()["job"]
    assert worker.run_once()
    done = api.get(f"/api/v1/jobs/{job['id']}", headers=AUTH).json()
    assert done["status"] == "succeeded", done["error"]
    assert (config_dir.parent / "var/data/import/QQQ.csv").is_file()  # server-chosen name
    datasets = api.get("/api/v1/data/datasets", headers=AUTH).json()["datasets"]
    assert {d["symbol"] for d in datasets} == {"QQQ"}
    assert all(d["warnings"] for d in datasets)  # no corporate actions: warned
    assert api.get("/api/v1/backtests/options", headers=AUTH).json()["sources"] == ["file"]

    bt = api.post(
        "/api/v1/jobs/backtest",
        headers=AUTH,
        json={"actor": ACTOR, "params": {"strategies": ["baseline_buy_hold"], "source": "file"}},
    )
    assert bt.status_code == 200, bt.text
    jid = bt.json()["job"]["id"]
    assert worker.run_once()
    view = api.get(f"/api/v1/backtests/runs/{jid}", headers=AUTH).json()
    assert view["job"]["status"] == "succeeded", view["job"]["error"]
    s = view["run"]["summary"]
    assert s["strategies"] == ["baseline_buy_hold"] and s["has_synthetic"] is False
    assert s["unpriced_instruments"] == ["TQQQ", "SQQQ"]
    assert "HYPOTHETICAL" in s["disclaimer"] and s["curve"]
    for key in (
        "total_return",
        "cagr",
        "volatility",
        "sharpe",
        "sortino",
        "max_drawdown",
        "calmar",
    ):
        assert key in s["headline"], key
    runs = api.get("/api/v1/backtests/runs", headers=AUTH).json()["runs"]
    assert runs[0]["job_id"] == jid
