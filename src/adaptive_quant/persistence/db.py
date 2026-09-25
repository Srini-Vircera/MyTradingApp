"""Engine and session management.

* The URL (with password) comes only from the ``DATABASE_URL`` secret; it is
  never logged or included in errors (``safe_url`` hides the password).
* ``postgresql://`` URLs are upgraded to the psycopg 3 driver.
* Every session runs with the configured ``statement_timeout``.
* Any connection-level failure is raised as :class:`DatabaseUnavailableError`,
  so callers can fail closed (e.g. refuse to create order intents).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from adaptive_quant.core.errors import (
    ConfigurationError,
    DatabaseUnavailableError,
    PersistenceError,
)


def normalize_url(raw: str) -> URL:
    try:
        url = make_url(raw)
    except Exception as exc:  # never echo the raw URL (it holds a password)
        raise ConfigurationError("DATABASE_URL is not a valid database URL") from exc
    if url.drivername in ("postgresql", "postgres"):
        url = url.set(drivername="postgresql+psycopg")
    if not url.drivername.startswith("postgresql"):
        raise ConfigurationError("DATABASE_URL must point to PostgreSQL")
    return url


def safe_url(url: URL) -> str:
    return url.render_as_string(hide_password=True)


class Database:
    def __init__(
        self,
        url: SecretStr | str,
        *,
        pool_size: int = 5,
        statement_timeout_ms: int = 15_000,
        connect_timeout_s: int = 5,
        echo: bool = False,
    ) -> None:
        raw = url.get_secret_value() if isinstance(url, SecretStr) else url
        self.url = normalize_url(raw)
        self.engine: Engine = create_engine(
            self.url,
            pool_size=pool_size,
            pool_pre_ping=True,
            echo=echo,
            connect_args={
                "connect_timeout": connect_timeout_s,
                "options": f"-c statement_timeout={int(statement_timeout_ms)} -c timezone=UTC",
            },
        )
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def __repr__(self) -> str:
        return f"Database({safe_url(self.url)})"

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A transaction: committed on success, rolled back on any error."""
        s = self._sessions()
        try:
            yield s
            s.commit()
        except (OperationalError, DBAPIError) as exc:
            s.rollback()
            if isinstance(exc, OperationalError) or exc.connection_invalidated:
                raise DatabaseUnavailableError(
                    f"audit database unavailable ({safe_url(self.url)}): {type(exc.orig).__name__}",
                    hint="check PostgreSQL (make db-up) and DATABASE_URL; nothing was recorded",
                ) from exc
            raise PersistenceError(f"database rejected the operation: {exc.orig}") from exc
        except SQLAlchemyError as exc:
            s.rollback()
            raise PersistenceError(f"database error: {exc}") from exc
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()

    def ping(self) -> None:
        """Raise :class:`DatabaseUnavailableError` unless a round trip succeeds."""
        with self.session() as s:
            s.execute(text("SELECT 1"))

    def dispose(self) -> None:
        self.engine.dispose()
