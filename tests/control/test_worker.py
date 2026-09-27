"""The worker executes queued jobs through the shared services and persists results."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from adaptive_quant.config.loader import load_config
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database
from adaptive_quant.quant.data.calendar import nyse_calendar
from adaptive_quant.services import data as data_service
from adaptive_quant.worker import handlers
from adaptive_quant.worker.context import scrub
from adaptive_quant.worker.runner import Worker
from tests.control.conftest import QNOW

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
SECRET = "sk-" + "s3cr3t" * 5


def make_worker(config_dir: Path, db: Database) -> Worker:
    secrets = Secrets(ALPACA_API_SECRET_KEY=SECRET, AQ_API_TOKEN="x" * 40)
    return Worker(
        load_config("development", config_dir=config_dir),
        secrets,
        FrozenClock(QNOW),
        db,
        nyse_calendar(),
        environ={},
        wid="test-worker",
    )


def submit(db: Database, job_type: str, params: dict[str, Any]) -> str:
    job, _ = ControlRepository(db).create_job(job_type, params, "operator", QNOW)
    return str(job["id"])


def test_startup_publishes_the_inventory_with_warnings(qqq_project: Path, db: Database) -> None:
    w = make_worker(qqq_project, db)
    w.startup()
    repo = ControlRepository(db)
    rows = [r for r in repo.inventory() if r["symbol"] == "QQQ"]
    assert {r["adjustment"] for r in rows} == {"raw", "split", "all"}
    for row in rows:
        assert row["source"] == "file" and row["frequency"] == "1d" and row["rows"] > 900
        assert row["validation_passed"] is True and row["is_synthetic"] is False
        assert data_service.NO_ACTIONS_WARNING in row["warnings"]
    (beat,) = repo.heartbeats()
    status = beat["status"]
    assert status["master_gate"] is False
    assert status["credentials_configured"]["ALPACA_API_SECRET_KEY"] is True
    assert "AQ_API_TOKEN" not in status["credentials_configured"]
    assert SECRET not in str(beat)


def test_qqq_buy_and_hold_job_is_persisted(qqq_project: Path, db: Database) -> None:
    w = make_worker(qqq_project, db)
    jid = submit(db, "backtest.run", {"strategies": ["baseline_buy_hold"], "source": "file"})
    assert w.run_once() is True
    repo = ControlRepository(db)
    job = repo.get_job(jid)
    assert job is not None, job
    assert job["status"] == "succeeded", job["error"]
    assert job["config_version"] and job["progress"] == 1.0
    run = repo.get_backtest(jid)
    assert run is not None
    s = run["summary"]
    assert run["strategies"] == ["baseline_buy_hold"] and run["source"] == "file"
    assert s["unpriced_instruments"] == ["TQQQ", "SQQQ"] and s["has_synthetic"] is False
    assert "HYPOTHETICAL" in s["disclaimer"] and len(s["curve"]) > 100
    listed = repo.list_backtests(10)
    assert listed[0]["job_id"] == jid and "curve" not in listed[0]["summary"]
    assert w.run_once() is False  # queue drained


def test_invalid_parameters_fail_the_job_not_the_worker(qqq_project: Path, db: Database) -> None:
    w = make_worker(qqq_project, db)
    jid = submit(db, "backtest.run", {"strategies": ["baseline_buy_hold"], "shell": "rm -rf /"})
    w.run_once()
    job = ControlRepository(db).get_job(jid)
    assert job is not None and job["status"] == "failed" and "shell" in job["error"]


def test_unknown_strategy_fails_cleanly(qqq_project: Path, db: Database) -> None:
    w = make_worker(qqq_project, db)
    jid = submit(db, "backtest.run", {"strategies": ["no_such_strategy"]})
    w.run_once()
    job = ControlRepository(db).get_job(jid)
    assert job is not None and job["status"] == "failed" and "no_such_strategy" in job["error"]


def test_errors_never_leak_credentials(
    qqq_project: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def leaky(*_a: object) -> dict[str, Any]:
        raise RuntimeError(f"upstream said: bad key {SECRET}")

    monkeypatch.setitem(handlers.HANDLERS, "data.inventory", leaky)
    w = make_worker(qqq_project, db)
    jid = submit(db, "data.inventory", {})
    w.run_once()
    repo = ControlRepository(db)
    job = repo.get_job(jid)
    assert job is not None and job["status"] == "failed"
    assert SECRET not in job["error"] and "***" in job["error"]
    assert all(SECRET not in log["message"] for log in repo.job_logs(jid))


def test_cancel_requested_stops_at_the_next_checkpoint(qqq_project: Path, db: Database) -> None:
    w = make_worker(qqq_project, db)
    repo = ControlRepository(db)
    jid = submit(db, "backtest.run", {"strategies": ["baseline_buy_hold"]})
    job = repo.claim_next(w.id, QNOW, ["backtest.run"])
    assert job is not None
    repo.request_cancel(jid, QNOW)
    w.execute(job)
    done = repo.get_job(jid)
    assert done is not None and done["status"] == "cancelled"
    assert repo.get_backtest(jid) is None


def test_scrub_ignores_short_or_missing_values() -> None:
    s = Secrets(POLYGON_API_KEY="abc", ALPACA_API_KEY_ID="PKXXXXXXXX")
    assert scrub("abc PKXXXXXXXX", s) == "abc ***"


class FakeAccount:
    def __init__(self, is_paper: bool) -> None:
        self.is_paper = is_paper
        self.trading_blocked = False
        self.currency = "USD"


class FakeBroker:
    """Records every call; only ``get_account`` exists, so any other call would fail."""

    def __init__(self, is_paper: bool = True, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.is_paper, self.fail = is_paper, fail

    def get_account(self) -> FakeAccount:
        self.calls.append("get_account")
        if self.fail:
            raise RuntimeError(f"401 unauthorized for key {SECRET}")
        return FakeAccount(self.is_paper)


@pytest.mark.parametrize(
    ("broker", "status", "is_paper"),
    [
        (FakeBroker(), "succeeded", True),
        (FakeBroker(is_paper=False), "succeeded", False),
        (FakeBroker(fail=True), "failed", False),
    ],
)
def test_broker_verification_only_reads_the_account(
    qqq_project: Path,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    broker: FakeBroker,
    status: str,
    is_paper: bool,
) -> None:
    from adaptive_quant.control.state import BROKER_KEY
    from adaptive_quant.trading.brokers import factory

    monkeypatch.setattr(factory, "build_broker", lambda *_a: broker)
    w = make_worker(qqq_project, db)
    jid = submit(db, "broker.verify", {})
    w.run_once()
    repo = ControlRepository(db)
    job = repo.get_job(jid)
    assert job is not None and job["status"] == status
    assert broker.calls == ["get_account"]
    state = repo.get_state(BROKER_KEY)
    assert state is not None
    v = state["value"]
    assert v["is_paper"] is is_paper and v["paper_endpoint"] is True
    assert v["endpoint_host"] == "paper-api.alpaca.markets"
    assert SECRET not in str(state) and SECRET not in str(job)


def test_file_download_job_refreshes_the_inventory(qqq_project: Path, db: Database) -> None:
    w = make_worker(qqq_project, db)
    jid = submit(db, "data.download", {"provider": "file", "symbols": ["QQQ"]})
    w.run_once()
    repo = ControlRepository(db)
    job = repo.get_job(jid)
    assert job is not None and job["status"] == "succeeded", job
    assert any(r["symbol"] == "QQQ" for r in repo.inventory())
    assert repo.job_logs(jid)  # progress was logged


def test_upload_import_writes_a_server_named_file(tmp_path: Path, db: Database) -> None:
    import shutil

    from tests.conftest import REPO_CONFIG
    from tests.control.conftest import QE, QS
    from tests.data_helpers import daily_bars

    config_dir = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, config_dir)
    df = daily_bars(QS, QE, start_price=100, vol=0.012, seed=3).reset_index()
    df.insert(0, "date", df.pop("timestamp").dt.tz_convert("America/New_York").dt.date)
    content = df.drop(columns=["is_synthetic"], errors="ignore").to_csv(index=False).encode()
    repo = ControlRepository(db)
    up = repo.create_upload(
        symbol="QQQ",
        kind="bars",
        frequency="1d",
        filename_hint="../../evil.csv",
        content=content,
        sha256="0" * 64,
        status="queued",
        preview={},
        requested_by="op",
        now=QNOW,
    )
    w = make_worker(config_dir, db)
    jid = submit(db, "data.import_upload", {"upload_id": up["id"]})
    w.run_once()
    job = repo.get_job(jid)
    assert job is not None
    assert job["status"] == "succeeded", job["error"]
    assert (tmp_path / "var/data/import/QQQ.csv").read_bytes() == content
    assert not (tmp_path.parent / "evil.csv").exists()
    assert repo.get_upload(up["id"])["status"] == "imported"  # type: ignore[index]
    # a second import of the same upload is refused (not queued any more)
    jid2 = submit(
        db,
        "data.import_upload",
        {
            "upload_id": up["id"],
        },
    )
    w.run_once()
    again = repo.get_job(jid2)
    assert again is not None and again["status"] == "failed" and "imported" in again["error"]
