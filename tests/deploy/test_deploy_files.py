"""Static checks of the container and Railway deployment files."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

import pytest
import yaml

from adaptive_quant.config.loader import load_config
from adaptive_quant.core.enums import TradingMode
from tests.conftest import REPO_CONFIG, REPO_ROOT

DEPLOY = REPO_ROOT / "deploy"
ENTRYPOINT = DEPLOY / "docker" / "entrypoint.sh"
SECRET_NAMES = re.compile(r"(TOKEN|PASSWORD|SECRET|API_KEY|DATABASE_URL)", re.I)


def railway(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DEPLOY / "railway" / f"{name}.json").read_text())
    return data


@pytest.mark.parametrize("name", ["api", "worker", "dashboard"])
def test_railway_services_build_from_repo_dockerfiles(name: str) -> None:
    cfg = railway(name)
    assert set(cfg) <= {"$schema", "build", "deploy"}  # no variables or secrets in the repo
    assert cfg["build"]["builder"] == "DOCKERFILE"
    assert (REPO_ROOT / cfg["build"]["dockerfilePath"]).is_file()
    d = cfg["deploy"]
    assert d["numReplicas"] == 1
    assert d["restartPolicyType"] == "ON_FAILURE"
    assert 1 <= d["restartPolicyMaxRetries"] <= 10
    assert d["sleepApplication"] is False
    assert "startCommand" not in d  # the image entrypoint chooses the role (AQ_ROLE)


def test_only_the_api_runs_migrations_and_health_checks_are_real_routes() -> None:
    from adaptive_quant.api.app import API_PREFIX

    api, worker, dash = railway("api"), railway("worker"), railway("dashboard")
    assert api["deploy"]["preDeployCommand"] == ["aq db upgrade"]
    assert "preDeployCommand" not in worker["deploy"]
    assert "preDeployCommand" not in dash["deploy"]
    assert api["deploy"]["healthcheckPath"] == f"{API_PREFIX}/health/ready"
    assert "healthcheckPath" not in worker["deploy"]  # no HTTP server
    caddy = (DEPLOY / "docker" / "Caddyfile").read_text()
    assert dash["deploy"]["healthcheckPath"] == "/healthz"
    assert 'respond /healthz "ok" 200' in caddy


def test_images_run_unprivileged_with_pinned_inputs() -> None:
    app = (DEPLOY / "docker" / "app.Dockerfile").read_text()
    dash = (DEPLOY / "docker" / "dashboard.Dockerfile").read_text()
    for text in (app, dash):
        assert ":latest" not in text
        joined = text.replace("\\\n", " ")
        for m in re.finditer(r"^(?:ENV|ARG)\s+(.+)$", joined, re.M):
            names = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)(?==|\s|$)", m.group(1))
            assert not [n for n in names if SECRET_NAMES.search(n)], m.group(0)  # none baked in
    assert "--require-hashes" in app
    assert "useradd --system --uid 10001" in app
    assert "setpriv --reuid=app --regid=app" in ENTRYPOINT.read_text()
    assert app.count("--checksum=sha256:") == 2  # pinned tini binaries
    assert "USER web" in dash
    assert "npm ci" in dash
    ignore = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    for entry in (".env", "var", ".git", "**/node_modules"):
        assert entry in ignore


def test_caddy_proxies_only_the_api_and_sets_security_headers() -> None:
    caddy = (DEPLOY / "docker" / "Caddyfile").read_text()
    assert re.findall(r"handle (/\S+)", caddy) == ["/api/v1/*"]
    assert "reverse_proxy {$API_UPSTREAM}" in caddy
    for header in (
        "Strict-Transport-Security",
        "X-Frame-Options",
        "frame-ancestors 'none'",
        "connect-src 'self'",
    ):
        assert header in caddy
    assert "redir @insecure https://" in caddy
    assert "admin off" in caddy


def test_compose_mirrors_the_deployment() -> None:
    stack = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())["services"]
    assert "ports" not in stack["api"]  # private network only
    assert all(p.startswith("127.0.0.1:") for p in stack["dashboard"]["ports"])
    assert stack["migrate"]["command"] == ["aq", "db", "upgrade"]
    assert stack["worker"]["volumes"] == ["worker-var:/app/var"]
    for svc in stack.values():
        assert "AQ_LIVE_TRADING_CONFIRM" not in (svc.get("environment") or {})


def test_production_overlay_is_never_live() -> None:
    s = load_config("production", config_dir=REPO_CONFIG).settings
    assert s.trading.mode is TradingMode.SHADOW
    assert not s.trading.live_trading.enabled
    assert s.trading.kill_switch.store == "database"
    assert s.logging.format == "json"


# ------------------------------------------------------------------ entrypoint
def entry(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ["PATH"], "AQ_ENTRYPOINT_DRY_RUN": "1"}
    return subprocess.run(  # noqa: S603 - the repository entrypoint in dry-run mode
        ["sh", str(ENTRYPOINT), *args],  # noqa: S607
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_entrypoint_api_role() -> None:
    r = entry(AQ_ROLE="api", PORT="7123", AQ_BIND_HOST="::")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "aq deploy check --role api",
        "exec aq api serve --host :: --port 7123 --behind-proxy",
    ]
    default = entry(AQ_ROLE="api").stdout.splitlines()[-1]
    assert default in (  # all interfaces, IPv6 dual-stack when the kernel has IPv6
        "exec aq api serve --host :: --port 8000 --behind-proxy",
        "exec aq api serve --host 0.0.0.0 --port 8000 --behind-proxy",
    )


def test_entrypoint_worker_is_idle_unless_the_scheduler_is_enabled() -> None:
    idle = entry(AQ_ROLE="worker")
    assert idle.stdout.splitlines()[0] == "aq deploy check --role worker"
    assert idle.stdout.splitlines()[-1] == "exec sleep infinity"
    for on in ("true", "TRUE", "1", "yes"):
        r = entry(AQ_ROLE="worker", AQ_SCHEDULER_ENABLED=on)
        assert r.stdout.splitlines()[-1] == "exec aq trade run"
    off = entry(AQ_ROLE="worker", AQ_SCHEDULER_ENABLED="no")
    assert off.stdout.splitlines()[-1] == "exec sleep infinity"


def test_entrypoint_other_modes() -> None:
    assert entry(AQ_ROLE="migrate").stdout.strip() == "exec aq db upgrade"
    assert entry("aq", "research", "run").stdout.strip() == "exec aq research run"
    bad = entry(AQ_ROLE="live")
    assert bad.returncode == 64
    assert "AQ_ROLE must be" in bad.stderr
    assert entry().returncode == 64  # no role, no command


def test_backup_scripts_never_print_the_url() -> None:
    for name in ("db_backup.sh", "db_restore.sh"):
        text = (REPO_ROOT / "scripts" / name).read_text()
        assert "set -eu" in text
        assert not re.search(r"echo[^\n]*\$\{?(DATABASE_URL|TARGET_DATABASE_URL|url)\b", text)


def test_dockerfiles_use_only_mounts_railway_supports() -> None:
    # Railway's Dockerfile validation accepts only RUN --mount=type=cache
    for name in ("app.Dockerfile", "dashboard.Dockerfile"):
        text = (DEPLOY / "docker" / name).read_text()
        for mount in re.findall(r"--mount=(\S+)", text):
            assert "type=cache" in mount, f"{name}: unsupported mount {mount}"


def test_dashboard_image_never_uses_the_python_entrypoint() -> None:
    # The dashboard runs Caddy; only app.Dockerfile has the AQ_ROLE entrypoint.
    text = (DEPLOY / "docker" / "dashboard.Dockerfile").read_text()
    final = text[text.rindex("\nFROM ") :]  # the runtime stage
    assert final.startswith("\nFROM caddy:")
    assert "ENTRYPOINT" not in text
    assert "entrypoint" not in text.lower()
    assert "app.Dockerfile" not in text
    assert final.rstrip().endswith(
        'CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]'
    )
    dash = railway("dashboard")
    assert dash["build"]["dockerfilePath"] == "deploy/docker/dashboard.Dockerfile"
    for name in ("api", "worker"):
        assert railway(name)["build"]["dockerfilePath"] == "deploy/docker/app.Dockerfile"


def test_entrypoint_names_the_image_when_deployed_without_a_role() -> None:
    r = entry(RAILWAY_SERVICE_NAME="dashboard")
    assert r.returncode == 64
    assert "AQ_ROLE must be api, worker or migrate (got '')" in r.stderr
    assert "Python api/worker image (deploy/docker/app.Dockerfile)" in r.stderr
    assert "running as service 'dashboard'" in r.stderr
    assert "deploy/docker/dashboard.Dockerfile" in r.stderr
    assert r.stdout == ""  # nothing is started
