"""Isolated foundation tests: no collectors, dotenv, or external service calls."""
from datetime import datetime, timezone, date, timedelta
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from persistence.config import DatabaseSettings, ConfigurationError, require_test_database
from persistence.database import make_engine, transaction, check_database, PersistenceError
from persistence.models import metadata, events, event_versions
from persistence.repository import EventRepository, IdentityConflict

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def normalized(**changes):
    return dict(schema_version=1, normalizer_version="fixture-v1", headline="Official release",
                summary="Selected structured facts", source_name="official", publisher="Government",
                timestamp_precision="unknown", publication_basis="unverified", attributes={}, **changes)


def migration_config(connection=None):
    config = Config(str(ROOT / "alembic.ini"))
    if connection is not None:
        config.attributes.update(connection=connection, test_target_verified=True)
    return config


class ConfigurationTests(unittest.TestCase):
    def test_required_url(self):
        with self.assertRaises(ConfigurationError): DatabaseSettings.from_env({})

    def test_sqlite_opt_in(self):
        with self.assertRaises(ConfigurationError): DatabaseSettings(url="sqlite://")

    def test_safe_repr(self):
        settings = DatabaseSettings(url="postgresql://user:private@localhost/mias")
        self.assertNotIn("private", repr(settings))
        self.assertEqual(settings.url.drivername, "postgresql+psycopg")

    def test_unsafe_query(self):
        with self.assertRaises(ConfigurationError): DatabaseSettings(url="postgresql://localhost/mias?options=secret")

    def test_limits(self):
        for key in ("pool_size", "statement_timeout_ms", "connect_timeout_seconds"):
            with self.subTest(key=key), self.assertRaises(ConfigurationError):
                DatabaseSettings(url="postgresql://localhost/mias", **{key: 0})

    def test_environment_no_dotenv(self):
        with patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv forbidden")):
            settings = DatabaseSettings.from_env({"DATABASE_URL": "sqlite://", "DB_ALLOW_SQLITE": "true"})
            self.assertEqual(settings.backend, "sqlite")

    def test_test_target_guard(self):
        with self.assertRaises(ConfigurationError): require_test_database(DatabaseSettings(url="postgresql://localhost/production"))
        self.assertEqual(require_test_database(DatabaseSettings(url="postgresql://localhost/mias_test")).backend, "postgresql")

    def test_engine_is_lazy(self):
        with patch("psycopg.connect", side_effect=AssertionError("connection forbidden")):
            engine = make_engine(DatabaseSettings(url="postgresql://localhost/mias"))
            engine.dispose()

    def test_imports_do_not_connect_or_load_dotenv(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, "-c", """
from unittest.mock import patch
with patch('psycopg.connect', side_effect=AssertionError('connection forbidden')), \
     patch('sqlalchemy.create_engine', side_effect=AssertionError('engine forbidden')), \
     patch('dotenv.load_dotenv', side_effect=AssertionError('dotenv forbidden')):
    import persistence.config
    import persistence.database
    import persistence.models
    import persistence.repository
    import persistence.adapters.macro
    import persistence.macro_shadow
"""], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_health_failure_redacted(self):
        engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        with patch.object(engine, "connect", side_effect=sa.exc.OperationalError("secret", {}, Exception("private"))):
            status = check_database(engine)
            self.assertFalse(status.ok)
            self.assertNotIn("private", repr(status))
        engine.dispose()


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def record(self, repo, **changes):
        values = dict(source_family="macro", event_key="existing-release-key", identity_version="v1",
                      version_key="initial", normalized=normalized(), observed_at=NOW)
        values.update(changes)
        return repo.record(**values)

    def test_insert_read(self):
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            row = self.record(repo)
            self.assertEqual(repo.current(row["event_id"])["id"], row["id"])
            self.assertEqual(row["observed_at"], NOW)

    def test_repeated_collection(self):
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            first = self.record(repo)
            again = self.record(repo, observed_at=NOW + timedelta(hours=1))
            self.assertEqual(first["id"], again["id"])
            self.assertEqual(len(repo.versions(first["event_id"])), 1)
            self.assertEqual(session.execute(sa.select(events.c.last_seen_at)).scalar_one(), NOW + timedelta(hours=1))

    def test_revision_and_backfill(self):
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            first = self.record(repo)
            revised = self.record(repo, version_key="revision", make_current=True)
            self.record(repo, version_key="backfill", observed_at=NOW - timedelta(days=2))
            self.record(repo, make_current=True)
            self.assertEqual(repo.current(first["event_id"])["id"], revised["id"])
            self.assertEqual(len(repo.versions(first["event_id"])), 3)

    def test_identity_conflict_rollback(self):
        with self.assertRaises(IdentityConflict), transaction(self.engine) as session:
            repo = EventRepository(session)
            self.record(repo)
            data = normalized(); data["headline"] = "Changed"
            self.record(repo, normalized=data)
        with transaction(self.engine) as session:
            self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(events)).scalar_one(), 0)

    def test_family_identity_isolation(self):
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            ids = {self.record(repo, source_family=f)["event_id"] for f in ("news", "sec", "fed", "macro", "treasury", "geopolitical")}
            self.assertEqual(len(ids), 6)

    def test_date_only(self):
        with transaction(self.engine) as session:
            data = normalized(); data.update(timestamp_precision="date", publication_date=date(2026, 9, 23))
            row = self.record(EventRepository(session), normalized=data)
            self.assertIsNone(row["published_at"])
            self.assertEqual(row["publication_date"], date(2026, 9, 23))

    def test_bad_precision(self):
        with self.assertRaises(PersistenceError), transaction(self.engine) as session:
            data = normalized(); data["timestamp_precision"] = "second"
            self.record(EventRepository(session), normalized=data)

    def test_naive_timestamp(self):
        with self.assertRaises(ValueError), transaction(self.engine) as session:
            self.record(EventRepository(session), observed_at=NOW.replace(tzinfo=None))

    def test_cross_event_pointer_rejected(self):
        with self.assertRaises(PersistenceError), transaction(self.engine) as session:
            repo = EventRepository(session)
            one = self.record(repo)
            two = self.record(repo, event_key="another")
            session.execute(events.update().where(events.c.id == one["event_id"]).values(current_version_id=two["id"]))

    def test_foreign_keys_enabled(self):
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)

    def test_provenance(self):
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            row = self.record(repo)
            values = dict(source_name="official", canonical_url="https://example.gov/release", retrieved_at=NOW)
            one = repo.add_provenance(row["id"], "document-1", **values)
            two = repo.add_provenance(row["id"], "document-1", **values)
            self.assertEqual(one["id"], two["id"])
            self.assertEqual(len(repo.provenance(row["id"])), 1)

    def test_provenance_conflict(self):
        with self.assertRaises(IdentityConflict), transaction(self.engine) as session:
            repo = EventRepository(session); row = self.record(repo)
            for link in ("https://example.gov/one", "https://example.gov/two"):
                repo.add_provenance(row["id"], "doc", source_name="official", canonical_url=link, retrieved_at=NOW)

    def test_no_raw_or_sensitive_metadata(self):
        for data in ({"raw_payload": "body"}, {"nested": {"authorization": "private"}}, {"x": float("nan")}, {"x": "a" * 65537}):
            with self.subTest(data_type=list(data)), self.assertRaises(ValueError), transaction(self.engine) as session:
                fields = normalized(); fields["attributes"] = data
                self.record(EventRepository(session), normalized=fields)

    def test_unknown_fields(self):
        with self.assertRaises(ValueError), transaction(self.engine) as session:
            self.record(EventRepository(session), normalized=normalized(raw_payload="forbidden"))

    def test_health(self):
        self.assertTrue(check_database(self.engine).ok)

    def test_commit_visible_to_new_session(self):
        with transaction(self.engine) as session:
            row = self.record(EventRepository(session))
        with transaction(self.engine) as session:
            self.assertEqual(EventRepository(session).current(row["event_id"])["id"], row["id"])

    def test_application_exception_rolls_back(self):
        with self.assertRaises(RuntimeError), transaction(self.engine) as session:
            self.record(EventRepository(session))
            raise RuntimeError("application failure")
        with transaction(self.engine) as session:
            self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(events)).scalar_one(), 0)

    def test_provenance_can_support_several_versions(self):
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            for version in ("initial", "revised"):
                row = self.record(repo, version_key=version)
                repo.add_provenance(row["id"], "same-document", source_name="official",
                                    canonical_url="https://example.gov/release", retrieved_at=NOW)
                self.assertEqual(len(repo.provenance(row["id"])), 1)

    def test_database_error_redacted(self):
        with self.assertRaises(PersistenceError) as caught:
            with transaction(self.engine) as session:
                session.execute(sa.text("SELECT private_value FROM missing_table"))
        self.assertNotIn("private_value", str(caught.exception))


class MigrationTests(unittest.TestCase):
    def test_upgrade_downgrade_metadata(self):
        engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        try:
            with engine.begin() as connection:
                config = migration_config(connection)
                command.upgrade(config, "head")
                self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
                from sqlalchemy.orm import Session
                with Session(connection, join_transaction_mode="create_savepoint") as session:
                    with session.begin():
                        EventRepository(session).record(source_family="fed", event_key="fixture", identity_version="v1",
                            version_key="initial", normalized=normalized(), observed_at=NOW)
                command.downgrade(config, "base")
                self.assertEqual(sa.inspect(connection).get_table_names(), ["alembic_version"])
                command.upgrade(config, "head")
        finally:
            engine.dispose()

    def test_postgres_offline_upgrade_downgrade(self):
        output = StringIO(); config = migration_config(); config.output_buffer = output
        command.upgrade(config, "head", sql=True)
        command.downgrade(config, "0001_persistence_foundation:base", sql=True)
        sql = output.getvalue()
        self.assertIn("JSONB", sql)
        self.assertIn("fk_events_current_version", sql)
        self.assertIn("DROP TABLE", sql)
        self.assertNotIn("password", sql)

    def test_online_requires_verified_target(self):
        with patch.dict("os.environ", {"TEST_DATABASE_URL": ""}):
            with self.assertRaises(ConfigurationError): command.upgrade(migration_config(), "head")


if __name__ == "__main__":
    unittest.main()
