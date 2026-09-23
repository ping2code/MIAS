"""Optional real PostgreSQL checks in a disposable schema of a named test DB."""
import os
import unittest
from uuid import uuid4
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine
from persistence.models import metadata
from persistence.repository import EventRepository
from sqlalchemy.orm import Session
from tests.test_persistence import migration_config, normalized, NOW


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL unavailable: real PostgreSQL validation pending")
class PostgreSQLTests(unittest.TestCase):
    def test_migration_roundtrip(self):
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        engine = make_engine(settings)
        schema = "mias_phase1_" + uuid4().hex
        try:
            with engine.begin() as connection:
                connection.execute(sa.schema.CreateSchema(schema))
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                config = migration_config(connection)
                command.upgrade(config, "head")
                self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
                with Session(connection, join_transaction_mode="create_savepoint") as session:
                    with session.begin():
                        repo = EventRepository(session)
                        inputs = dict(source_family="macro", event_key="test-release", identity_version="v1",
                                      version_key="initial", normalized=normalized(), observed_at=NOW)
                        first = repo.record(**inputs)
                        self.assertEqual(repo.record(**inputs)["id"], first["id"])
                        self.assertEqual(repo.current(first["event_id"])["attributes"], {})
                        repo.add_provenance(first["id"], "test-document", source_name="official",
                                            canonical_url="https://example.gov/release", retrieved_at=NOW)
                command.downgrade(config, "-1")
                command.upgrade(config, "head")
                connection.execute(sa.schema.DropSchema(schema, cascade=True))
        finally:
            engine.dispose()
