"""Explicit database settings, without dotenv or application-service imports."""

from dataclasses import dataclass, field
from collections.abc import Mapping
import os

from sqlalchemy.engine import URL, make_url


class ConfigurationError(ValueError):
    """Safe to display: messages never include connection strings."""


@dataclass(frozen=True)
class DatabaseSettings:
    url: URL = field(repr=False)
    pool_size: int = 5
    max_overflow: int = 5
    pool_timeout_seconds: int = 10
    connect_timeout_seconds: int = 5
    statement_timeout_ms: int = 15_000
    lock_timeout_ms: int = 5_000
    application_name: str = "mias"
    sqlite_enabled: bool = False

    def __post_init__(self):
        try:
            url = make_url(self.url)
        except Exception:
            raise ConfigurationError("Invalid database URL") from None
        if url.drivername in {"postgres", "postgresql"}:
            url = url.set(drivername="postgresql+psycopg")
        if url.drivername not in {"postgresql+psycopg", "sqlite", "sqlite+pysqlite"}:
            raise ConfigurationError("Unsupported database driver")
        if url.get_backend_name() == "sqlite":
            if not self.sqlite_enabled:
                raise ConfigurationError("SQLite requires explicit development/test opt-in")
            if url.query:
                raise ConfigurationError("SQLite URL query options are not supported")
        elif not url.database:
            raise ConfigurationError("PostgreSQL database name is required")
        # Do not accept arbitrary connect options, including search_path or SQL.
        if set(url.query) - {"sslmode", "sslrootcert"}:
            raise ConfigurationError("Unsupported database URL query options")
        if any(not isinstance(v, str) for v in url.query.values()):
            raise ConfigurationError("Repeated database URL options are not supported")
        if "sslmode" in url.query and url.query["sslmode"] not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ConfigurationError("Invalid database TLS mode")
        for name in ("pool_size", "pool_timeout_seconds", "connect_timeout_seconds", "statement_timeout_ms", "lock_timeout_ms"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 2_147_483_647:
                raise ConfigurationError("Database limits must be positive integers")
        if type(self.max_overflow) is not int or not 0 <= self.max_overflow <= 100:
            raise ConfigurationError("Database overflow must be between zero and 100")
        if self.pool_size > 100:
            raise ConfigurationError("Database pool size must not exceed 100")
        if self.application_name != "mias" and (not self.application_name.isascii() or not self.application_name.replace("_", "").replace("-", "").isalnum()):
            raise ConfigurationError("Invalid database application name")
        if not 1 <= len(self.application_name) <= 63:
            raise ConfigurationError("Invalid database application name length")
        object.__setattr__(self, "url", url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        """Read only named process-environment variables; never load .env."""
        env = os.environ if env is None else env
        raw = env.get("DATABASE_URL")
        if not raw:
            raise ConfigurationError("DATABASE_URL is required")
        names = {
            "pool_size": ("DB_POOL_SIZE", "5"),
            "max_overflow": ("DB_MAX_OVERFLOW", "5"),
            "pool_timeout_seconds": ("DB_POOL_TIMEOUT_SECONDS", "10"),
            "connect_timeout_seconds": ("DB_CONNECT_TIMEOUT_SECONDS", "5"),
            "statement_timeout_ms": ("DB_STATEMENT_TIMEOUT_MS", "15000"),
            "lock_timeout_ms": ("DB_LOCK_TIMEOUT_MS", "5000"),
        }
        try:
            values = {key: int(env.get(name, default)) for key, (name, default) in names.items()}
        except (TypeError, ValueError):
            raise ConfigurationError("Database limits must be integers") from None
        flag = env.get("DB_ALLOW_SQLITE", "false").lower()
        if flag not in {"true", "false"}:
            raise ConfigurationError("DB_ALLOW_SQLITE must be true or false")
        return cls(url=raw, sqlite_enabled=flag == "true",
                   application_name=env.get("DB_APPLICATION_NAME", "mias"), **values)

    @property
    def backend(self):
        return self.url.get_backend_name()


def require_test_database(settings: DatabaseSettings):
    """Guard destructive test migrations. Never guess credentials or a database."""
    if settings.backend != "postgresql":
        raise ConfigurationError("PostgreSQL test URL required")
    name = settings.url.database or ""
    if not (name.startswith("mias_test_") or name == "mias_test"):
        raise ConfigurationError("Test database name must be mias_test or start with mias_test_")
    return settings
