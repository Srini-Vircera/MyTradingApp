"""``aq deploy check`` - refuse to start a container that is not deployment-safe.

    aq deploy check --role api|worker

Run by the container entrypoint before every service starts (Railway or any
other Docker host). It never changes anything; it lists every problem at once
and exits non-zero so the platform marks the deployment failed.

Deployment-safe means:

* never live: trading mode is backtest, shadow or paper; live trading is not
  enabled in config; ``AQ_LIVE_TRADING_CONFIRM`` is not set at all;
* a real deployment overlay (not ``development``), JSON logs;
* the kill switch is the shared database store, and ``DATABASE_URL`` is set;
* the broker adapter points at the Alpaca *paper* endpoint;
* per role: the API has its token; a worker running the scheduler has broker
  credentials and a paper or shadow mode.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from urllib.parse import urlparse

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.enums import DeploymentEnvironment, TradingMode

EXIT_OK = 0
EXIT_REFUSED = 2
ROLES = ("api", "worker")
ALPACA_PAPER_HOST = "paper-api.alpaca.markets"
TRUE = {"1", "true", "yes", "on"}


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    dep = sub.add_parser("deploy", help="deployment safety checks")
    d = dep.add_subparsers(dest="action", required=True)
    chk = d.add_parser("check", help="refuse unless this container is deployment-safe")
    chk.add_argument("--role", choices=ROLES, required=True)


def scheduler_enabled(environ: Mapping[str, str]) -> bool:
    return environ.get("AQ_SCHEDULER_ENABLED", "").strip().lower() in TRUE


def problems(
    loaded: LoadedConfig, secrets: Secrets, role: str, environ: Mapping[str, str]
) -> list[str]:
    s = loaded.settings
    out: list[str] = []
    # ---- never live
    if s.trading.mode is TradingMode.LIVE or s.trading.mode.uses_real_money:
        out.append("trading.mode is live; this deployment supports backtest, shadow and paper only")
    if s.trading.live_trading.enabled:
        out.append("trading.live_trading.enabled is true; it must stay false")
    if "AQ_LIVE_TRADING_CONFIRM" in environ:
        out.append("AQ_LIVE_TRADING_CONFIRM is set; remove it from the service variables")
    # ---- deployment basics
    if s.app.environment is DeploymentEnvironment.DEVELOPMENT:
        out.append("AQ_ENV is development; deploy with AQ_ENV=production (or paper)")
    if s.logging.format != "json":
        out.append("logging.format must be json in a deployment")
    if s.trading.kill_switch.store != "database":
        out.append(
            "trading.kill_switch.store must be 'database' so every service shares one kill switch"
        )
    if secrets.database_url is None:
        out.append("DATABASE_URL is not set (reference the PostgreSQL service's variable)")
    host = urlparse(s.broker.alpaca.paper_base_url).hostname
    if s.broker.provider == "alpaca" and host != ALPACA_PAPER_HOST:
        out.append(f"broker.alpaca.paper_base_url must use {ALPACA_PAPER_HOST} (got {host!r})")
    # ---- per role
    if role == "api":
        token = secrets.api_token.get_secret_value() if secrets.api_token else ""
        if len(token.strip()) < s.api.min_bearer_chars:
            out.append(f"AQ_API_TOKEN is missing or shorter than {s.api.min_bearer_chars} chars")
    if role == "worker" and scheduler_enabled(environ):
        if s.trading.mode not in (TradingMode.SHADOW, TradingMode.PAPER):
            out.append(f"the scheduler needs trading.mode shadow or paper (got {s.trading.mode})")
        if s.broker.provider == "alpaca" and (
            secrets.alpaca_api_key_id is None or secrets.alpaca_api_secret_key is None
        ):
            out.append(
                "the scheduler needs ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY (paper account)"
            )
    return out


def run(args: argparse.Namespace, loaded: LoadedConfig, secrets: Secrets) -> int:
    found = problems(loaded, secrets, args.role, os.environ)
    s = loaded.settings
    print(
        f"deploy check: role={args.role} env={s.app.environment.value} "
        f"mode={s.trading.mode.value} config={loaded.config_version}"
    )
    for p in found:
        print(f"REFUSED: {p}")
    if found:
        return EXIT_REFUSED
    print("deploy check passed (live trading disabled)")
    return EXIT_OK
