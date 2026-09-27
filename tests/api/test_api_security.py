"""Auth, the mutation whitelist, kill-switch confirmations, headers, CORS and schema drift."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr

from adaptive_quant.api.app import ENGAGE_CONFIRMATION, MUTATING_ROUTES, create_app
from adaptive_quant.api.services import ApiServices
from adaptive_quant.core.errors import ConfigurationError
from adaptive_quant.trading.safety.kill_switch import RELEASE_CONFIRMATION
from tests.api.conftest import AUTH, TOKEN, services
from tests.conftest import REPO_ROOT

_DB = ["overview", "portfolio", "signals", "risk", "performance", "orders", "shadow-orders",
       "executions", "reconciliation", "cycles", "cycles/x/explain"]  # fmt: skip
_CONTROL = ["jobs", f"jobs/{'0' * 32}", "data/datasets", "data/options", "data/uploads",
            f"data/uploads/{'0' * 32}", "backtests/options", "backtests/runs",
            f"backtests/runs/{'0' * 32}", "research/runs", "strategies/manager", "settings",
            "trading/control", "audit/events"]  # fmt: skip
_OTHER = ["strategies", "backtests", "system/health", "configuration", "kill-switch",
          "trading/live-readiness"]  # fmt: skip
GET_PAGES = [f"/api/v1/{p}" for p in _DB + _CONTROL + _OTHER]
DB_PAGES = {f"/api/v1/{p}" for p in _DB + _CONTROL}
#: GET page -> route template (for the coverage check)
_TEMPLATES = {"/x/": "/{cycle_id}/", "jobs/" + "0" * 32: "jobs/{job_id}",
              "uploads/" + "0" * 32: "uploads/{upload_id}",
              "runs/" + "0" * 32: "runs/{job_id}"}  # fmt: skip


def _template(path: str) -> str:
    for concrete, template in _TEMPLATES.items():
        path = path.replace(concrete, template)
    return path


def _flatten(items: list[Any]) -> list[APIRoute]:
    out: list[APIRoute] = []
    for r in items:
        if isinstance(r, APIRoute):
            out.append(r)
        elif hasattr(r, "original_router"):  # included routers (resolved lazily by FastAPI)
            out += _flatten(list(r.original_router.routes))
    return out


def routes(client: TestClient) -> list[APIRoute]:
    found = _flatten(list(client.app.routes))  # type: ignore[attr-defined]
    documented = {
        (m.upper(), path)
        for path, ops in client.app.openapi()["paths"].items()  # type: ignore[attr-defined]
        for m in ops
    }
    assert {(m, r.path) for r in found for m in (r.methods or ())} == documented  # nothing hidden
    return found


PROBES = {"/api/v1/health/live", "/api/v1/health/ready"}


def test_probes_are_public_everything_else_needs_the_token(client: TestClient) -> None:
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}
    ready = client.get("/api/v1/health/ready")  # no database configured in this client
    assert ready.status_code == 503
    assert ready.json() == {"status": "not ready"}  # no internals
    for path in GET_PAGES:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}):
            r = client.get(path, headers=headers)
            assert r.status_code == 401, (path, headers)
            assert TOKEN not in r.text
    for method, path in MUTATING_ROUTES:
        assert client.request(method, path, json={}).status_code == 401


def test_every_route_is_covered_by_the_page_list(client: TestClient) -> None:
    documented = {_template(p) for p in GET_PAGES} | PROBES
    get_paths = {r.path for r in routes(client) if "GET" in (r.methods or ())}
    assert get_paths == documented


#: The complete set of state-changing routes. Adding one is a deliberate, reviewed change:
#: update this list, its tests (auth, strict body, audit) and docs/API.md together.
EXPECTED_MUTATIONS = {
    ("POST", f"/api/v1/{p}")
    for p in (
        "kill-switch/engage", "kill-switch/release",
        "jobs/data/download", "jobs/data/validate", "jobs/data/synthesize",
        "jobs/data/inventory", "jobs/backtest", "jobs/research", "jobs/broker/verify",
        "jobs/{job_id}/cancel", "jobs/{job_id}/retry",
        "data/uploads", "data/uploads/{upload_id}/import", "data/uploads/{upload_id}/discard",
        "strategies/{strategy_id}/params", "strategies/{strategy_id}/enabled",
        "strategies/{strategy_id}/lifecycle",
        "settings",
        "trading/scheduler/start", "trading/scheduler/stop", "trading/mode",
    )
}  # fmt: skip


def test_mutations_are_exactly_the_allowlist(client: TestClient) -> None:
    mutating = {
        (m, r.path) for r in routes(client) for m in (r.methods or ()) if m not in ("GET", "HEAD")
    }
    assert mutating == set(MUTATING_ROUTES) == EXPECTED_MUTATIONS
    assert all(m == "POST" for m, _p in mutating)  # no PUT/PATCH/DELETE anywhere


def test_no_route_can_submit_orders_enable_live_or_touch_secrets(client: TestClient) -> None:
    for path in ("/api/v1/configuration", "/api/v1/orders", "/api/v1/strategies",
                 "/api/v1/overview", "/api/v1/settings", "/api/v1/trading/control"):  # fmt: skip
        for method in ("PUT", "PATCH", "DELETE"):
            r = client.request(method, path, headers=AUTH, json={"mode": "live"})
            assert r.status_code in (404, 405), (method, path)
    words = ("order", "submit", "live", "secret", "credential", "env", "promote", "shell", "exec")
    for _m, path in MUTATING_ROUTES:
        assert not any(w in path for w in words), path


def test_every_mutation_body_model_forbids_unknown_fields(client: TestClient) -> None:
    schema = client.app.openapi()  # type: ignore[attr-defined]
    comps = schema["components"]["schemas"]

    def strict(ref: str, seen: set[str]) -> None:
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return
        seen.add(name)
        model = comps[name]
        if model.get("type") == "object" and "properties" in model:
            assert model.get("additionalProperties") is False, name
        for prop in model.get("properties", {}).values():
            for sub in [prop, *prop.get("anyOf", []), prop.get("items", {})]:
                if "$ref" in sub:
                    strict(sub["$ref"], seen)

    for method, path in MUTATING_ROUTES:
        op = schema["paths"][path][method.lower()]
        if path.endswith("/data/uploads"):  # raw CSV body + strict query parameters
            names = {p["name"] for p in op["parameters"]}
            assert names == {"symbol", "kind", "frequency", "filename", "actor"}
            continue
        body = op["requestBody"]["content"]["application/json"]["schema"]
        strict(body["$ref"], set())


def test_pages_without_a_database(client: TestClient) -> None:
    for path in GET_PAGES:
        r = client.get(path, headers=AUTH)
        if path in DB_PAGES:
            assert r.status_code == 503 and "not configured" in r.json()["detail"], path
        else:
            assert r.status_code == 200, path
            assert r.json().get("banner", {}).get("uses_real_money", False) is False
    health = client.get("/api/v1/system/health", headers=AUTH).json()
    assert health["database"] == {"configured": False}


def test_configuration_is_read_only_and_redacted(client: TestClient) -> None:
    c = client.get("/api/v1/configuration", headers=AUTH).json()
    assert c["live_trading_lock"]["changeable_via_api"] is False
    assert c["banner"]["live_trading_enabled"] is False and c["banner"]["mode"] == "shadow"
    assert "trading" in c["settings"] and TOKEN not in json.dumps(c)


def test_kill_switch_requires_confirmation_and_a_named_operator(
    client: TestClient, tmp_path: Path
) -> None:
    assert (
        client.get("/api/v1/kill-switch", headers=AUTH).json()["engaged"] is True
    )  # fresh install
    bad = {"actor": "Jane", "reason": "please release", "confirm": "yes"}
    r = client.post("/api/v1/kill-switch/release", headers=AUTH, json=bad)
    assert r.status_code == 400 and RELEASE_CONFIRMATION in r.json()["detail"]
    ok = {**bad, "confirm": RELEASE_CONFIRMATION}
    released = client.post("/api/v1/kill-switch/release", headers=AUTH, json=ok).json()
    assert released["engaged"] is False and released["actor"] == "operator:Jane"
    r = client.post("/api/v1/kill-switch/engage", headers=AUTH, json={**bad, "confirm": "stop"})
    assert r.status_code == 400
    engaged = client.post(
        "/api/v1/kill-switch/engage", headers=AUTH, json={**bad, "confirm": ENGAGE_CONFIRMATION}
    )
    assert engaged.json()["engaged"] is True
    assert (
        client.post(
            "/api/v1/kill-switch/engage", headers=AUTH, json={**bad, "extra": 1}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/kill-switch/engage",
            headers=AUTH,
            json={"actor": "", "reason": "x", "confirm": ENGAGE_CONFIRMATION},
        ).status_code
        == 422
    )


def test_security_headers_cors_and_no_docs(client: TestClient) -> None:
    r = client.get("/api/v1/kill-switch", headers={**AUTH, "Origin": "http://localhost:3000"})
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    evil = client.get("/api/v1/kill-switch", headers={**AUTH, "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in evil.headers
    assert client.get("/docs").status_code == 404 and client.get("/redoc").status_code == 404


def test_committed_openapi_schema_is_current(client: TestClient) -> None:
    committed = json.loads((REPO_ROOT / "apps" / "api" / "openapi.json").read_text())
    assert committed == client.app.openapi(), "run: aq api openapi > apps/api/openapi.json"  # type: ignore[attr-defined]
    assert "HTTPBearer" in json.dumps(committed["components"]["securitySchemes"])


def test_weak_tokens_are_refused(tmp_path: Path) -> None:
    s = services(tmp_path)
    with pytest.raises(ConfigurationError, match="at least 32"):
        ApiServices(s.loaded, SecretStr("short"), s.kill_switch, s.clock, s.calendar)
    assert create_app(s)
