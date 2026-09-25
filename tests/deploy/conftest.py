"""Deployment tests re-use the PostgreSQL fixtures."""

from tests.persistence.conftest import db, empty_db, pg_server_url  # noqa: F401
