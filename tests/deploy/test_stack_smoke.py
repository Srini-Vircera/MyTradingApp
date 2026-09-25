"""End-to-end smoke test of the deployed topology, without containers:

PostgreSQL <- ``aq db upgrade`` (pre-deploy) <- ``aq api serve`` (private) <- Caddy
(the dashboard server, with the production Caddyfile) <- the browser.

Needs PostgreSQL (like the other persistence tests) and a ``caddy`` binary
(on PATH or ``AQ_TEST_CADDY``); skipped without Caddy.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.persistence.db import Database
from adaptive_quant.trading.safety.kill_switch import KillSwitch
from adaptive_quant.trading.safety.kill_switch_store import DatabaseKillSwitchStore
from tests.conftest import REPO_CONFIG, REPO_ROOT

pytestmark = pytest.mark.postgres
CADDY = os.environ.get("AQ_TEST_CADDY") or shutil.which("caddy")
TOKEN = "smoke-test-operator-token-" + "q" * 24


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(url: str, ok: int = 200, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code == ok:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"{url} not ready")


def aq(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - this interpreter, test-only
        [sys.executable, "-m", "adaptive_quant.cli", "--env-file", "/nonexistent", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.fixture
def stack(empty_db: Database, tmp_path: Path) -> Iterator[tuple[str, Database]]:
    if CADDY is None:
        pytest.skip("caddy binary not available (set AQ_TEST_CADDY)")
    env = {
        "PATH": os.environ["PATH"],
        "AQ_ENV": "production",
        "AQ_CONFIG_DIR": str(REPO_CONFIG),
        "DATABASE_URL": empty_db.url.render_as_string(hide_password=False),
        "AQ_API_TOKEN": TOKEN,
    }
    # pre-deploy: migrations (the API refuses readiness until they ran)
    up = aq("db", "upgrade", env=env)
    assert up.returncode == 0, up.stderr
    assert aq("deploy", "check", "--role", "api", env=env).returncode == 0
    api_port, web_port = free_port(), free_port()
    static = tmp_path / "srv"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>dashboard</title>")
    (static / "404.html").write_text("not found page")
    api_cmd = [sys.executable, "-m", "adaptive_quant.cli", "--env-file", "/nonexistent",
               "api", "serve", "--host", "127.0.0.1", "--port", str(api_port),
               "--behind-proxy"]  # fmt: skip
    caddy_cmd = [CADDY, "run", "--config", str(REPO_ROOT / "deploy/docker/Caddyfile"),
                 "--adapter", "caddyfile"]  # fmt: skip
    caddy_env = {
        "PATH": env["PATH"],
        "HOME": str(tmp_path),
        "PORT": str(web_port),
        "API_UPSTREAM": f"127.0.0.1:{api_port}",
        "DASHBOARD_ROOT": str(static),
        "XDG_CONFIG_HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path),
    }
    null = subprocess.DEVNULL
    procs = [
        subprocess.Popen(api_cmd, env=env, stdout=null, stderr=null),  # noqa: S603 - test-only
        subprocess.Popen(caddy_cmd, env=caddy_env, stdout=null, stderr=null),  # noqa: S603
    ]
    try:
        base = f"http://127.0.0.1:{web_port}"
        wait_for(f"{base}/healthz")
        wait_for(f"{base}/api/v1/health/ready")
        yield base, empty_db
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=30)


def test_the_deployed_stack(stack: tuple[str, Database]) -> None:
    base, db = stack
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with httpx.Client(base_url=base, timeout=10) as c:
        home = c.get("/")
        assert home.status_code == 200 and "dashboard" in home.text
        for header in ("strict-transport-security", "content-security-policy", "x-frame-options"):
            assert header in home.headers
        assert "server" not in home.headers
        assert c.get("/nope").status_code == 404

        # the API is reachable only through the proxy path, token required
        assert c.get("/api/v1/health/ready").json() == {"status": "ready"}
        assert c.get("/api/v1/overview").status_code == 401
        overview = c.get("/api/v1/overview", headers=auth)
        assert overview.status_code == 200
        banner = overview.json()["banner"]
        assert (banner["mode"], banner["uses_real_money"]) == ("shadow", False)

        # fresh deployment: the shared kill switch starts engaged (never released)
        ks = c.get("/api/v1/kill-switch", headers=auth).json()
        assert ks["engaged"] and ks["fail_safe"]

        # STOP from the dashboard origin reaches the worker's view of the kill switch
        body = {"actor": "ann", "reason": "smoke test", "confirm": "STOP AUTOMATED TRADING"}
        assert c.post("/api/v1/kill-switch/engage", json=body, headers=auth).status_code == 200
        worker_view = KillSwitch(DatabaseKillSwitchStore(db), FrozenClock(datetime.now(UTC)))
        assert worker_view.status().actor == "operator:ann"
        assert worker_view.is_engaged()

        # plain HTTP at the edge is redirected; oversized bodies are refused
        r = c.get("/portfolio/", headers={"X-Forwarded-Proto": "http"})
        assert r.status_code == 308 and r.headers["location"].startswith("https://")
        big = c.post("/api/v1/kill-switch/engage", content=b"x" * 70_000, headers=auth)
        assert big.status_code == 413
