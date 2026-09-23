"""Live tests only: caller must prove TEST_DATABASE_URL is disposable/test-only."""
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from threading import Event
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine, transaction, PersistenceError
from persistence.models import metadata, events, event_versions
from persistence.repository import EventRepository, IdentityConflict
from tests import test_persistence as unit


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL unavailable: live validation pending")
class PostgreSQLTests(unit.RepositoryTests):
    def setUp(self):
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.admin = make_engine(settings)
        self.schema = "mias_phase2a_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(self.schema))
        self.engine = make_engine(settings)

        @sa.event.listens_for(self.engine, "connect")
        def search_path(connection, _):
            # Persist across transaction rollback; identifier is generated locally.
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
            connection.autocommit = False

        self.addCleanup(self.cleanup_schema)
        with self.engine.begin() as connection:
            command.upgrade(unit.migration_config(connection), "head")

    def tearDown(self):
        pass

    def cleanup_schema(self):
        self.engine.dispose()
        with self.admin.begin() as connection:
            connection.execute(sa.schema.DropSchema(self.schema, cascade=True))
        self.admin.dispose()

    def test_foreign_keys_enabled(self):
        with self.assertRaises(PersistenceError), transaction(self.engine) as session:
            EventRepository(session).add_provenance(str(uuid4()), "orphan", source_name="official",
                canonical_url="https://example.gov", retrieved_at=unit.NOW)

    def test_migration_roundtrip(self):
        for direction in ("downgrade", "upgrade"):
            with self.engine.begin() as connection:
                config = unit.migration_config(connection)
                if direction == "downgrade":
                    command.downgrade(config, "base")
                    self.assertEqual(sa.inspect(connection).get_table_names(), ["alembic_version"])
                    self.assertEqual(connection.exec_driver_sql(
                        "SELECT count(*) FROM pg_constraint WHERE connamespace = current_schema()::regnamespace"
                    ).scalar_one(), 1)  # Alembic primary key only.
                else:
                    command.upgrade(config, "head")
        with self.engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
            inspector = sa.inspect(connection)
            self.assertEqual(set(inspector.get_table_names()), set(metadata.tables) | {"alembic_version"})
            for table in metadata.tables.values():
                columns = {c["name"]: c for c in inspector.get_columns(table.name)}
                for column in table.c:
                    self.assertEqual(columns[column.name]["nullable"], column.nullable)
                    self.assertIsNone(columns[column.name]["default"])
                if "attributes" in columns:
                    self.assertIsInstance(columns["attributes"]["type"], sa.dialects.postgresql.JSONB)
                for column in ("observed_at", "recorded_at", "first_seen_at", "retrieved_at"):
                    if column in columns:
                        self.assertTrue(columns[column]["type"].timezone)
            fk = inspector.get_foreign_keys("events")[0]
            self.assertTrue(fk["options"]["deferrable"])
            self.assertEqual(fk["options"]["initially"], "DEFERRED")
            for table in ("event_versions", "event_provenance"):
                self.assertEqual(inspector.get_foreign_keys(table)[0]["options"]["ondelete"], "RESTRICT")
            self.assertEqual(connection.get_isolation_level(), "READ COMMITTED")

    def test_jsonb_and_timezone_roundtrip(self):
        data = unit.normalized()
        data.update(attributes={"nested": [None, True, 1.25, {"unicode": "Δ"}]},
                    timestamp_precision="second", published_at=unit.NOW.astimezone(timezone(timedelta(hours=5))))
        with transaction(self.engine) as session:
            row = self.record(EventRepository(session), normalized=data)
        with transaction(self.engine) as session:
            current = EventRepository(session).current(row["event_id"])
            self.assertEqual(current["attributes"], data["attributes"])
            self.assertEqual(current["published_at"], unit.NOW)
            self.assertEqual(current["published_at"].utcoffset(), timedelta(0))

    def test_failed_append_preserves_committed_history(self):
        with transaction(self.engine) as session:
            original = self.record(EventRepository(session))
        with self.assertRaises(IdentityConflict), transaction(self.engine) as session:
            repo = EventRepository(session)
            self.record(repo, version_key="rolled-back", make_current=True)
            data = unit.normalized(); data["headline"] = "conflict"
            self.record(repo, normalized=data)
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            self.assertEqual([r["id"] for r in repo.versions(original["event_id"])], [original["id"]])
            self.assertEqual(repo.current(original["event_id"])["id"], original["id"])

    def test_direct_constraints_and_recovery(self):
        with transaction(self.engine) as session:
            row = self.record(EventRepository(session))
        for operation in ("duplicate", "delete", "orphan"):
            with self.subTest(operation=operation), self.assertRaises(PersistenceError), transaction(self.engine) as session:
                if operation == "duplicate":
                    values = dict(row); values["id"] = str(uuid4())
                    session.execute(event_versions.insert().values(**values))
                elif operation == "delete":
                    session.execute(events.delete().where(events.c.id == row["event_id"]))
                else:
                    values = dict(row); values.update(id=str(uuid4()), event_id=str(uuid4()))
                    session.execute(event_versions.insert().values(**values))
        with transaction(self.engine) as session:
            self.assertEqual(EventRepository(session).current(row["event_id"])["id"], row["id"])

    def race(self, first, second):
        """Hold writer one until PostgreSQL confirms writer two waits on a lock."""
        started = Event()
        def worker():
            with transaction(self.engine) as session:
                pid = session.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
                pids.append(pid)
                started.set()
                return second(EventRepository(session))
        pids = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction(self.engine) as session:
                one = first(EventRepository(session))
                future = pool.submit(worker)
                self.assertTrue(started.wait(3))
                deadline = time.monotonic() + 3
                blocked = False
                while time.monotonic() < deadline:
                    with self.admin.connect() as observer:
                        blocked = observer.execute(sa.text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"
                        ), {"pid": pids[0]}).scalar()
                    if blocked:
                        break
                    time.sleep(0.02)
                self.assertTrue(blocked, "Second real PostgreSQL transaction must overlap and block")
            two = future.result(timeout=10)
        return one, two

    def test_concurrent_event_identity(self):
        one, two = self.race(lambda r: self.record(r), lambda r: self.record(r))
        self.assertEqual(one["id"], two["id"])
        with transaction(self.engine) as session:
            for table in (events, event_versions):
                self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one(), 1)

    def test_concurrent_version_identity(self):
        with transaction(self.engine) as session:
            self.record(EventRepository(session))
        one, two = self.race(lambda r: self.record(r, version_key="revision"),
                             lambda r: self.record(r, version_key="revision"))
        self.assertEqual(one["id"], two["id"])
        with transaction(self.engine) as session:
            self.assertEqual(len(EventRepository(session).versions(one["event_id"])), 2)

    def test_concurrent_conflicting_version(self):
        def conflict(repo):
            data = unit.normalized()
            data["headline"] = "Conflicting writer"
            return self.record(repo, normalized=data)
        with self.assertRaises(IdentityConflict):
            self.race(lambda r: self.record(r), conflict)
        with transaction(self.engine) as session:
            row = session.execute(sa.select(event_versions)).mappings().one()
            self.assertEqual(row["headline"], "Official release")
            self.assertEqual(EventRepository(session).current(row["event_id"])["id"], row["id"])

    def test_concurrent_provenance(self):
        with transaction(self.engine) as session:
            row = self.record(EventRepository(session))
        def add(repo):
            return repo.add_provenance(row["id"], "doc", source_name="official",
                canonical_url="https://example.gov", retrieved_at=unit.NOW)
        one, two = self.race(add, add)
        self.assertEqual(one["id"], two["id"])
        with transaction(self.engine) as session:
            self.assertEqual(len(EventRepository(session).provenance(row["id"])), 1)

    def test_concurrent_current_promotion(self):
        with transaction(self.engine) as session:
            self.record(EventRepository(session))
        one, two = self.race(lambda r: self.record(r, version_key="a", make_current=True),
                             lambda r: self.record(r, version_key="b", make_current=True))
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            self.assertEqual(repo.current(one["event_id"])["id"], two["id"])
            self.assertEqual(len(repo.versions(one["event_id"])), 3)
