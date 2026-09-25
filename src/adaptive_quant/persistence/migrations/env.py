"""Alembic environment. The connection is always passed in programmatically by
``adaptive_quant.persistence.migrate`` (the URL never lives in a config file)."""

from __future__ import annotations

from alembic import context

from adaptive_quant.persistence.models import Base

connection = context.config.attributes.get("connection")
if connection is None:  # pragma: no cover - only reachable when misused from the alembic CLI
    raise RuntimeError("run migrations via `aq db upgrade` (connection is injected)")

context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
