"""Job queue, uploads, runtime config and audit tables on a real PostgreSQL."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from adaptive_quant.core.errors import PersistenceError
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database

pytestmark = pytest.mark.postgres
T0 = datetime(2024, 7, 2, 14, tzinfo=UTC)
TYPES = ["data.inventory", "backtest.run"]


@pytest.fixture
def repo(db: Database) -> ControlRepository:
    return ControlRepository(db)


def queue(repo: ControlRepository, n: int = 1, t: str = "backtest.run") -> list[str]:
    return [
        repo.create_job(t, {"i": i}, "operator", T0 + timedelta(seconds=i))[0]["id"]
        for i in range(n)
    ]


def test_identical_active_jobs_are_deduplicated(repo: ControlRepository) -> None:
    a, created_a = repo.create_job("data.inventory", {"x": 1, "y": [2]}, "op", T0)
    b, created_b = repo.create_job("data.inventory", {"y": [2], "x": 1}, "op", T0)
    c, created_c = repo.create_job("data.inventory", {"x": 2}, "op", T0)
    assert created_a and not created_b and created_c
    assert a["id"] == b["id"] != c["id"]
    assert a["status"] == "queued" and a["requested_by"] == "op"


def test_claims_are_fifo_and_exclusive_across_workers(
    repo: ControlRepository, db: Database
) -> None:
    ids = queue(repo, 12)
    claimed: list[str] = []
    lock = threading.Lock()

    def work(wid: str) -> None:
        r = ControlRepository(db)
        while (job := r.claim_next(wid, T0, TYPES)) is not None:
            with lock:
                claimed.append(job["id"])

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)  # every job exactly once
    assert len(set(claimed)) == len(claimed)


def test_claim_respects_types_and_order(repo: ControlRepository) -> None:
    first, _second = queue(repo, 2)
    queue(repo, 1, "research.run")
    job = repo.claim_next("w", T0, TYPES)
    assert job is not None and job["id"] == first and job["status"] == "running"
    assert job["worker_id"] == "w"
    assert repo.claim_next("w", T0, ["data.inventory"]) is None


def test_only_the_owner_finishes_a_running_job(repo: ControlRepository) -> None:
    (jid,) = queue(repo)
    repo.claim_next("w1", T0, TYPES)
    assert not repo.finish(jid, "w2", "succeeded", T0)  # not the owner
    assert repo.finish(jid, "w1", "succeeded", T0, result={"ok": True}, config_version="v1")
    assert not repo.finish(jid, "w1", "failed", T0)  # already terminal
    job = repo.get_job(jid)
    assert job is not None and job["status"] == "succeeded" and job["result"] == {"ok": True}
    with pytest.raises(PersistenceError):
        repo.finish(jid, "w1", "running", T0)


def test_terminal_states_are_final_in_the_database(repo: ControlRepository, db: Database) -> None:
    (jid,) = queue(repo)
    repo.claim_next("w", T0, TYPES)
    repo.finish(jid, "w", "failed", T0, error="boom")
    with pytest.raises(PersistenceError, match="finished job never changes"), db.session() as s:
        s.execute(text("UPDATE control_jobs SET status='queued' WHERE id=:i"), {"i": jid})


def test_running_jobs_cannot_be_requeued(repo: ControlRepository, db: Database) -> None:
    (jid,) = queue(repo)
    repo.claim_next("w", T0, TYPES)
    with pytest.raises(PersistenceError, match="cannot be re-queued"), db.session() as s:
        s.execute(text("UPDATE control_jobs SET status='queued' WHERE id=:i"), {"i": jid})


def test_cancel_queued_is_immediate_running_is_cooperative(repo: ControlRepository) -> None:
    a, b = queue(repo, 2)
    assert repo.request_cancel(a, T0)["status"] == "cancelled"
    repo.claim_next("w", T0, TYPES)
    assert repo.request_cancel(b, T0)["status"] == "running"
    assert repo.heartbeat(b, "w", T0, progress=0.5) is True  # the worker learns about it
    repo.finish(b, "w", "cancelled", T0)
    with pytest.raises(PersistenceError, match="already cancelled"):
        repo.request_cancel(b, T0)
    assert repo.claim_next("w", T0, TYPES) is None


def test_orphaned_running_jobs_fail_and_are_never_rerun(repo: ControlRepository) -> None:
    a, b = queue(repo, 2)
    repo.claim_next("dead", T0, TYPES)
    repo.claim_next("alive", T0, TYPES)
    repo.heartbeat(b, "alive", T0 + timedelta(minutes=9))
    assert repo.fail_orphans(T0 + timedelta(minutes=12), timedelta(minutes=10)) == 1
    job_a, job_b = repo.get_job(a), repo.get_job(b)
    assert job_a is not None and job_a["status"] == "failed" and "stopped" in job_a["error"]
    assert job_b is not None and job_b["status"] == "running"


@pytest.mark.parametrize("table", ["control_job_logs", "runtime_config_changes", "control_events"])
def test_audit_tables_are_append_only(repo: ControlRepository, db: Database, table: str) -> None:
    (jid,) = queue(repo)
    repo.log(jid, "info", "hello", T0)
    repo.record_event(
        actor="op", action="x", target="y", outcome="accepted", detail={}, client="c", now=T0
    )
    repo.save_runtime_config(
        expected_revision=0,
        overlay={},
        strategy_overrides={},
        actor="op",
        now=T0,
        changes=[{"kind": "setting", "path": "p", "old": 1, "new": 2, "reason": "because"}],
        version_before="a",
        version_after="b",
    )
    with pytest.raises(PersistenceError, match="append-only"), db.session() as s:
        s.execute(text(f"DELETE FROM {table}"))


def test_runtime_config_uses_optimistic_revisions(repo: ControlRepository) -> None:
    assert repo.runtime_config()["revision"] == 0
    kw: dict[str, Any] = {
        "strategy_overrides": {},
        "actor": "op",
        "now": T0,
        "changes": [],
        "version_before": "a",
        "version_after": "b",
    }
    assert repo.save_runtime_config(expected_revision=0, overlay={"a": 1}, **kw) == 1
    with pytest.raises(PersistenceError, match="changed"):
        repo.save_runtime_config(expected_revision=0, overlay={"a": 2}, **kw)
    assert repo.runtime_config()["overlay"] == {"a": 1}


def test_discarding_an_upload_drops_its_content(repo: ControlRepository) -> None:
    up = repo.create_upload(
        symbol="QQQ",
        kind="bars",
        frequency="1d",
        filename_hint="q.csv",
        content=b"date\n",
        sha256="0" * 64,
        status="validated",
        preview={},
        requested_by="op",
        now=T0,
    )
    with pytest.raises(PersistenceError, match="validated"):
        repo.set_upload_status(up["id"], "queued", T0, expect=["invalid"])
    repo.set_upload_status(up["id"], "discarded", T0, expect=["validated"])
    assert repo.upload_content(up["id"]) == b""
    listed = repo.list_uploads(10)
    assert listed[0]["status"] == "discarded" and "content" not in listed[0]
