"""Operator command line: ``aq <command>``.

Run ``aq --help`` for the list of commands. Errors are printed as plain
sentences with a "What to do" hint; exit code 2 means the command was refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from adaptive_quant import __version__
from adaptive_quant.config.loader import LoadedConfig, load_config
from adaptive_quant.config.schema import redact
from adaptive_quant.config.secrets import Secrets, load_secrets
from adaptive_quant.core.clock import SystemClock, to_market_time
from adaptive_quant.core.errors import AQError
from adaptive_quant.observability.logging import configure_logging
from adaptive_quant.trading.safety.kill_switch import (
    RELEASE_CONFIRMATION,
    FileKillSwitchStore,
    KillSwitch,
)

SECRET_GROUPS: dict[str, tuple[str, ...]] = {
    "broker": ("alpaca_api_key_id", "alpaca_api_secret_key"),
    "database": ("database_url",),
    "polygon": ("polygon_api_key",),
}

EXIT_OK = 0
EXIT_REFUSED = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aq", description="Adaptive Quant operator CLI")
    parser.add_argument("--env", help="development | paper | production (default: $AQ_ENV)")
    parser.add_argument("--config-dir", type=Path, help="configuration directory")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="dotenv file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version")

    cfg = sub.add_parser("config", help="inspect and validate configuration")
    cfg_sub = cfg.add_subparsers(dest="action", required=True)
    val = cfg_sub.add_parser("validate", help="validate configuration files")
    val.add_argument(
        "--require-secrets",
        choices=sorted(SECRET_GROUPS),
        action="append",
        default=[],
        help="also require these credentials to be set (repeatable)",
    )
    cfg_sub.add_parser("show", help="print the resolved configuration (secrets redacted)")

    sub.add_parser("secrets", help="show which credentials are set (never their values)")

    ks = sub.add_parser("kill-switch", help="inspect or change the global kill switch")
    ks_sub = ks.add_subparsers(dest="action", required=True)
    ks_sub.add_parser("status", help="show kill switch state")
    eng = ks_sub.add_parser("engage", help="STOP automated risk-increasing trading")
    eng.add_argument("--actor", required=True, help="your name")
    eng.add_argument("--reason", required=True)
    rel = ks_sub.add_parser("release", help="re-enable automated trading (deliberate action)")
    rel.add_argument("--actor", required=True, help="your name")
    rel.add_argument("--reason", required=True)
    rel.add_argument("--confirm", required=True, help=f"must be exactly: {RELEASE_CONFIRMATION!r}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except AQError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "version":
        print(__version__)
        return EXIT_OK

    secrets = load_secrets(args.env_file)
    loaded = load_config(args.env, config_dir=args.config_dir, secrets=secrets)
    configure_logging(loaded.settings.logging.level, fmt="console")

    if args.command == "config":
        return _config(args, loaded, secrets)
    if args.command == "secrets":
        for name, present in secrets.present().items():
            print(f"{name:<28} {'set' if present else 'NOT SET'}")
        return EXIT_OK
    if args.command == "kill-switch":
        return _kill_switch(args, loaded)
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover


def _config(args: argparse.Namespace, loaded: LoadedConfig, secrets: Secrets) -> int:
    s = loaded.settings
    if args.action == "show":
        payload = {
            "config_version": loaded.config_version,
            "sources": [str(src.path) for src in loaded.sources],
            "settings": redact(s.model_dump(mode="json")),
        }
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    for group in args.require_secrets:
        secrets.require(*SECRET_GROUPS[group])
    print(f"OK  configuration is valid ({loaded.config_version})")
    print(f"    environment : {s.app.environment}")
    print(
        f"    trading mode: {s.trading.mode}"
        + ("   <-- REAL MONEY" if s.trading.mode.uses_real_money else "")
    )
    print(f"    tradeable   : {', '.join(s.universe.tradeable_symbols)}")
    for warning in loaded.warnings:
        print(f"WARN {warning}")
    return EXIT_OK


def _kill_switch(args: argparse.Namespace, loaded: LoadedConfig) -> int:
    cfg = loaded.settings.trading.kill_switch
    switch = KillSwitch(
        FileKillSwitchStore(
            loaded.resolve_path(cfg.state_file), loaded.resolve_path(cfg.audit_file)
        ),
        SystemClock(),
    )
    if args.action == "engage":
        switch.engage(actor=args.actor, reason=args.reason)
    elif args.action == "release":
        switch.release(actor=args.actor, reason=args.reason, confirmation=args.confirm)
    status = switch.status()
    if status.engaged:
        print("kill switch: ENGAGED (automated risk-increasing trading is STOPPED)")
    else:
        print("kill switch: released")
    print(f"  reason : {status.reason}")
    if status.actor:
        print(f"  by     : {status.actor}")
    if status.changed_at:
        print(f"  at     : {to_market_time(status.changed_at):%Y-%m-%d %H:%M:%S %Z}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
