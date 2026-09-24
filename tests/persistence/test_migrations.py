"""Alembic migrations create exactly the modelled schema, reversibly."""

from sqlalchemy import inspect

from adaptive_quant.persistence import migrate
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.models import APPEND_ONLY, Base


def tables(db: Database) -> set[str]:
    return set(inspect(db.engine).get_table_names()) - {"alembic_version"}


def test_upgrade_matches_models_and_downgrade_is_clean(empty_db: Database) -> None:
    assert migrate.current_revision(empty_db) is None
    migrate.upgrade(empty_db)
    assert migrate.current_revision(empty_db) == migrate.head_revision()
    assert tables(empty_db) == set(Base.metadata.tables)
    assert len(tables(empty_db)) == 28
    assert migrate.schema_drift(empty_db) == []
    migrate.downgrade(empty_db, "base")
    assert tables(empty_db) == set()
    migrate.upgrade(empty_db)  # repeatable
    assert migrate.schema_drift(empty_db) == []


def test_every_append_only_table_has_its_trigger(db: Database) -> None:
    with db.engine.connect() as c:
        rows = c.exec_driver_sql(
            "SELECT event_object_table FROM information_schema.triggers "
            "WHERE trigger_name LIKE '%%append_only'"
        ).all()
    assert {r[0] for r in rows} == set(APPEND_ONLY)
