"""Offline DDL or explicit test-only migrations; never load dotenv."""
import os
from alembic import context
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine
from persistence.models import metadata

config = context.config


def run(connection):
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(dialect_name="postgresql", target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    # Only explicit programmatic test callers supply an already guarded connection.
    if not config.attributes.get("test_target_verified"):
        raise RuntimeError("An explicitly verified test target is required")
    run(config.attributes["connection"])
else:
    settings = require_test_database(DatabaseSettings(url=os.environ.get("TEST_DATABASE_URL", "")))
    engine = make_engine(settings)
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
