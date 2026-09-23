"""Lazy engine, short transactions, and non-secret diagnostics."""

from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from persistence.config import DatabaseSettings


class PersistenceError(RuntimeError):
    """Redacted public exception. Driver errors must not escape repository APIs."""


def make_engine(settings: DatabaseSettings):
    """Construct a lazy engine: no connection or DDL occurs here."""
    options = dict(echo=False, hide_parameters=True, pool_pre_ping=True)
    if settings.backend == "postgresql":
        options.update(pool_size=settings.pool_size, max_overflow=settings.max_overflow,
                       pool_timeout=settings.pool_timeout_seconds,
                       connect_args={"connect_timeout": settings.connect_timeout_seconds,
                                     "application_name": settings.application_name,
                                     "options": f"-c statement_timeout={settings.statement_timeout_ms} -c lock_timeout={settings.lock_timeout_ms}"})
    else:
        options["connect_args"] = {"timeout": settings.connect_timeout_seconds}
        if settings.url.database in (None, "", ":memory:"):
            options["poolclass"] = StaticPool
    engine = create_engine(settings.url, **options)
    if settings.backend == "sqlite":
        @event.listens_for(engine, "connect")
        def sqlite_connect(connection, _):
            # Explicit BEGIN avoids sqlite3 legacy transaction behavior affecting DDL/savepoints.
            connection.isolation_level = None
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine, "begin")
        def sqlite_begin(connection):
            connection.exec_driver_sql("BEGIN")
    return engine


@contextmanager
def transaction(engine):
    """One session per unit of work; commit or roll back, then always close."""
    try:
        with Session(engine, expire_on_commit=False) as session:
            with session.begin():
                yield session
    except SQLAlchemyError:
        raise PersistenceError("Database transaction failed") from None


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    backend: str
    error_code: str | None = None


def check_database(engine):
    """Explicit connectivity probe; no schema writes or credential-bearing errors."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return HealthStatus(True, engine.dialect.name)
    except SQLAlchemyError:
        return HealthStatus(False, engine.dialect.name, "database_unavailable")
