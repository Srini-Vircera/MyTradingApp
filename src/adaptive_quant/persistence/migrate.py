"""Programmatic Alembic: upgrade / downgrade / current / drift check."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.models import Base

MIGRATIONS = Path(__file__).with_name("migrations")


def _config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(_config()).get_current_head()
    if head is None:  # pragma: no cover - the package always ships migrations
        raise RuntimeError("no migrations found")
    return head


def upgrade(db: Database, revision: str = "head") -> None:
    cfg = _config()
    with db.engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def downgrade(db: Database, revision: str) -> None:
    cfg = _config()
    with db.engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


def current_revision(db: Database) -> str | None:
    with db.engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def schema_drift(db: Database) -> list[object]:
    """Differences between the migrated database and the models (empty = in sync)."""
    with db.engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(ctx, Base.metadata))
