"""``aq api`` - the operator API (read-only views + kill switch).

    aq api serve        run the API (uvicorn) on api.host:api.port (localhost by default);
                        --host/--port/--behind-proxy for container deployments
    aq api openapi      print the OpenAPI schema (committed as apps/api/openapi.json)

``serve`` refuses without ``AQ_API_TOKEN`` (at least api.min_bearer_chars characters).
"""

from __future__ import annotations

import argparse
import json

from pydantic import SecretStr

from adaptive_quant.api.app import create_app
from adaptive_quant.api.services import ApiServices
from adaptive_quant.cli_db import open_database
from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import Clock
from adaptive_quant.quant.data.calendar import nyse_calendar
from adaptive_quant.trading.safety.kill_switch_store import build_kill_switch

EXIT_OK = 0
SCHEMA_PLACEHOLDER = SecretStr(
    "schema-generation-only-" + "x" * 48
)  # never accepted by a running API


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    api = sub.add_parser("api", help="operator API (read-only + kill switch)")
    a = api.add_subparsers(dest="action", required=True)
    serve = a.add_parser("serve", help="run the API server")
    serve.add_argument("--host", help="bind address (default: api.host); '::' for all interfaces")
    serve.add_argument("--port", type=int, help="port (default: api.port)")
    serve.add_argument(
        "--behind-proxy",
        action="store_true",
        help="trust X-Forwarded-* headers (only when a reverse proxy is the sole entry point)",
    )
    a.add_parser("openapi", help="print the OpenAPI schema as JSON")


def build_services(
    loaded: LoadedConfig, secrets: Secrets, clock: Clock, token: SecretStr
) -> ApiServices:
    db = open_database(loaded, secrets) if secrets.database_url is not None else None
    return ApiServices(
        loaded=loaded,
        token=token,
        kill_switch=build_kill_switch(loaded, clock, db),
        clock=clock,
        calendar=nyse_calendar(),
        db=db,
    )


def run(args: argparse.Namespace, loaded: LoadedConfig, secrets: Secrets, clock: Clock) -> int:
    if args.action == "openapi":
        services = build_services(loaded, Secrets(), clock, SCHEMA_PLACEHOLDER)
        print(json.dumps(create_app(services).openapi(), indent=2, sort_keys=True))
        return EXIT_OK
    import uvicorn

    services = build_services(loaded, secrets, clock, secrets.secret("api_token"))
    cfg = loaded.settings.api
    host = args.host or cfg.host
    port = args.port or cfg.port
    print(f"operator API on {host} port {port}, /api/v1 (mode {loaded.settings.trading.mode})")
    uvicorn.run(
        create_app(services),
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        proxy_headers=args.behind_proxy,
        forwarded_allow_ips="*" if args.behind_proxy else None,
        timeout_graceful_shutdown=20,
    )
    return EXIT_OK
