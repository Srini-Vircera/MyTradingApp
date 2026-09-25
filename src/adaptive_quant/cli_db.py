"""``aq db`` - audit database: migrations, health and decision explanations.

The connection URL comes only from ``DATABASE_URL`` (``.env`` or environment)
and is never printed with its password.
"""

from __future__ import annotations

import argparse
import json

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import Clock
from adaptive_quant.persistence import migrate
from adaptive_quant.persistence.db import Database, safe_url
from adaptive_quant.persistence.explain import explain_decision
from adaptive_quant.persistence.repositories import ReferenceRepository
from adaptive_quant.quant.strategies.catalog import StrategyCatalog
from adaptive_quant.trading.audit import config_record, strategy_version_record

EXIT_OK = 0
EXIT_REFUSED = 2


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    db = sub.add_parser("db", help="audit database (PostgreSQL)")
    d = db.add_subparsers(dest="action", required=True)
    d.add_parser("upgrade", help="apply migrations, then record config, instruments, strategies")
    d.add_parser("status", help="connection, schema revision and drift")
    ex = d.add_parser("explain", help="print the full stored decision chain of a trading cycle")
    ex.add_argument("cycle_id")


def open_database(loaded: LoadedConfig, secrets: Secrets) -> Database:
    c = loaded.settings.database
    return Database(
        secrets.secret("database_url"),
        pool_size=c.pool_size,
        statement_timeout_ms=c.statement_timeout_ms,
        echo=c.echo_sql,
    )


def run(args: argparse.Namespace, loaded: LoadedConfig, secrets: Secrets, clock: Clock) -> int:
    db = open_database(loaded, secrets)
    try:
        if args.action == "upgrade":
            db.ping()
            migrate.upgrade(db)
            ref = ReferenceRepository(db)
            ref.record_config(config_record(loaded))
            ref.sync_instruments(loaded.settings.universe.instruments)
            catalog = StrategyCatalog.from_config(loaded.settings.strategies)
            for e in catalog.entries:
                ref.ensure_strategy_version(strategy_version_record(e.version))
            print(f"{safe_url(db.url)}: schema at {migrate.current_revision(db)}")
            print(f"recorded config {loaded.config_version}, {len(catalog)} strategy versions")
            return EXIT_OK
        if args.action == "status":
            db.ping()
            current, head = migrate.current_revision(db), migrate.head_revision()
            print(f"database  {safe_url(db.url)}: reachable")
            print(f"schema    {current or 'not migrated'} (head {head})")
            if current != head:
                print("REFUSED: schema is not at head - run `aq db upgrade`")
                return EXIT_REFUSED
            drift = migrate.schema_drift(db)
            print(f"drift     {drift or 'none'}")
            return EXIT_OK if not drift else EXIT_REFUSED
        print(json.dumps(explain_decision(db, args.cycle_id), indent=2, sort_keys=True))
        return EXIT_OK
    finally:
        db.dispose()
