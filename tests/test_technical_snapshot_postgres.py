"""Phase 4C technical snapshot schema on a real disposable PostgreSQL (TEST_DATABASE_URL, mias_test_* only).

Each test uses a fresh schema migrated to head. Covered: migration 0004 upgrade/downgrade/re-upgrade, constraints,
indexes, insert/duplicate/conflict, range queries, true concurrent identical and conflicting writes, the shadow writer,
database outage, and the read-only tools including content-hash integrity after the JSONB round trip.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import os
import socket
import time
import unittest
from threading import Event
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from persistence import technical_shadow
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import PersistenceError, make_engine, transaction
from persistence.models import metadata, technical_snapshot_conflicts, technical_snapshots
from persistence.technical_snapshot_repository import TechnicalSnapshotRepository, persist_technical_snapshot
from persistence.technical_snapshot_tools import main as tools_main
from tests import test_persistence as unit
from tests.test_technical_snapshot_persistence import CAL, row_for, snapshots

TABLES = {"technical_snapshots", "technical_snapshot_conflicts"}


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class TechnicalSnapshotPostgresTests(unittest.TestCase):
    def setUp(self):
        self.settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.admin = make_engine(self.settings)
        self.schema = "mias_phase4c_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(self.schema))
        self.engine = make_engine(self.settings)

        @sa.event.listens_for(self.engine, "connect")
        def search_path(connection, _):
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
            connection.autocommit = False

        self.addCleanup(self.cleanup)
        self.migrate("upgrade", "head")
        self.bars, self.snaps = snapshots()

    def cleanup(self):
        self.engine.dispose()
        with self.admin.begin() as connection:
            connection.execute(sa.schema.DropSchema(self.schema, cascade=True))
        self.admin.dispose()

    def migrate(self, direction, target):
        with self.engine.begin() as connection:
            getattr(command, direction)(unit.migration_config(connection), target)

    def tables(self):
        with self.engine.connect() as connection:
            return set(sa.inspect(connection).get_table_names())

    def count(self, table=technical_snapshots):
        with transaction(self.engine) as session:
            return session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()

    def test_upgrade_downgrade_reupgrade(self):
        self.assertTrue(TABLES <= self.tables())
        persist_technical_snapshot(self.engine, row_for(self.snaps[-1], self.bars))
        for _ in range(2):
            self.migrate("downgrade", "0003_geo_anchor_registry")
            self.assertFalse(TABLES & self.tables())
            self.assertIn("events", self.tables())  # Prior tables untouched.
            self.migrate("upgrade", "head")
            self.assertTrue(TABLES <= self.tables())
        self.migrate("downgrade", "base")
        self.assertEqual(self.tables(), {"alembic_version"})
        self.migrate("upgrade", "head")
        with self.engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
        self.assertEqual(version, "0004_technical_snapshots")

    def test_indexes_constraints_and_types(self):
        with self.engine.connect() as connection:
            inspector = sa.inspect(connection)
            uniques = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("technical_snapshots")}
            self.assertEqual(uniques["uq_technical_snapshots_identity"],
                             ["symbol", "interval", "snapshot_timestamp", "engine_version"])
            indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("technical_snapshots")}
            self.assertEqual(indexes["ix_technical_snapshots_state"], ["symbol", "interval", "technical_state"])
            self.assertIn("ix_technical_snapshots_created_at", indexes)
            columns = {c["name"]: c for c in inspector.get_columns("technical_snapshots")}
            self.assertIsInstance(columns["support_levels"]["type"], sa.dialects.postgresql.JSONB)
            self.assertTrue(columns["snapshot_timestamp"]["type"].timezone)
            self.assertIsInstance(columns["rsi14"]["type"], sa.Float)
            fk = inspector.get_foreign_keys("technical_snapshot_conflicts")[0]
            self.assertEqual((fk["referred_table"], fk["options"]["ondelete"]), ("technical_snapshots", "RESTRICT"))
            connection.execute(sa.text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(connection.execute(sa.text(
                "EXPLAIN SELECT * FROM technical_snapshots WHERE symbol='META' AND interval='5m' "
                "AND snapshot_timestamp >= now() - interval '1 day'")).scalars().all())
        # Symbol/interval/time-range queries are index-backed (the identity or state index, planner's choice).
        self.assertTrue("uq_technical_snapshots_identity" in plan or "ix_technical_snapshots_state" in plan, plan)
        row = row_for(self.snaps[-1], self.bars)
        values = dict(row, id=str(uuid4()), created_at=datetime.now(timezone.utc),
                      snapshot_timestamp=datetime.fromisoformat(row["snapshot_timestamp"]),
                      warmup_start=datetime.fromisoformat(row["warmup_start"]))
        for bad in (dict(technical_state="BUY"), dict(confidence="SURE"), dict(interval="2h"),
                    dict(is_completed_bar=False), dict(content_hash="short"), dict(volume=-1), dict(gap_type="huge")):
            with self.subTest(bad=bad), self.assertRaises(PersistenceError), transaction(self.engine) as session:
                session.execute(sa.insert(technical_snapshots).values(**dict(values, **bad)))
        self.assertEqual(self.count(), 0)

    def test_insert_duplicate_conflict_and_queries(self):
        row = row_for(self.snaps[-1], self.bars)
        self.assertEqual(persist_technical_snapshot(self.engine, row)["outcome"], "inserted")
        self.assertEqual(persist_technical_snapshot(self.engine, row)["outcome"], "duplicate")
        conflict = persist_technical_snapshot(self.engine, row_for(self.snaps[-1], self.bars, warmup_bars=3))
        self.assertEqual((conflict["outcome"], conflict["conflict_recorded"]), ("conflict", True))
        for snap in self.snaps[-6:-1]:
            persist_technical_snapshot(self.engine, row_for(snap, self.bars))
        self.assertEqual((self.count(), self.count(technical_snapshot_conflicts)), (6, 1))
        with transaction(self.engine) as session:
            repo = TechnicalSnapshotRepository(session)
            window = repo.snapshots("META", "5m", start=self.snaps[-3].timestamp, end=self.snaps[-1].timestamp)
            self.assertEqual([r["snapshot_timestamp"] for r in window], [s.timestamp for s in self.snaps[-3:-1]])
            stored = repo.snapshots("META", "5m")[-1]
        self.assertEqual(stored["content_hash"], row["content_hash"])
        self.assertEqual((stored["rsi14"], stored["ema200"], stored["support_levels"]),
                         (row["rsi14"], row["ema200"], row["support_levels"]))

    def race(self, first_row, second_row):
        """Hold writer one's transaction until PostgreSQL shows writer two blocked on the same identity."""
        started, pids = Event(), []

        def worker():
            with transaction(self.engine) as session:
                pids.append(session.execute(sa.text("SELECT pg_backend_pid()")).scalar_one())
                started.set()
                return TechnicalSnapshotRepository(session).store(second_row)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction(self.engine) as session:
                one = TechnicalSnapshotRepository(session).store(first_row)
                future = pool.submit(worker)
                self.assertTrue(started.wait(3))
                deadline, blocked = time.monotonic() + 3, False
                while time.monotonic() < deadline and not blocked:
                    with self.admin.connect() as observer:
                        blocked = observer.execute(sa.text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"), {"pid": pids[0]}).scalar()
                    time.sleep(0.02)
                self.assertTrue(blocked, "the second transaction must overlap and block")
            return one, future.result(timeout=10)

    def test_concurrent_identical_writes(self):
        row = row_for(self.snaps[-1], self.bars)
        one, two = self.race(row, row)
        self.assertEqual((one["outcome"], two["outcome"], one["id"]), ("inserted", "duplicate", two["id"]))
        self.assertEqual(self.count(), 1)

    def test_concurrent_conflicting_writes(self):
        one, two = self.race(row_for(self.snaps[-1], self.bars), row_for(self.snaps[-1], self.bars, warmup_bars=7))
        self.assertEqual((one["outcome"], two["outcome"]), ("inserted", "conflict"))
        self.assertEqual((self.count(), self.count(technical_snapshot_conflicts)), (1, 1))

    def test_writer_and_outage(self):
        writer = technical_shadow.TechnicalSnapshotWriter(engine_factory=lambda: self.engine, capacity=16)
        for snap in self.snaps[-4:]:
            writer.submit(row_for(snap, self.bars))
        writer.submit(row_for(self.snaps[-1], self.bars))
        stats = writer.shutdown(drain=True, timeout=10)["stats"]
        self.assertEqual((stats["persisted"], stats["duplicate"], stats["failed"]), (5, 1, 0))
        self.assertEqual(self.count(), 4)
        reserved = self.enterContext(socket.socket())
        reserved.bind(("127.0.0.1", 0))
        missing = DatabaseSettings(url=self.settings.url.set(host="127.0.0.1", port=reserved.getsockname()[1]),
                                   connect_timeout_seconds=2)
        with self.assertLogs("technical_shadow", "WARNING"):
            dead = technical_shadow.TechnicalSnapshotWriter(engine_factory=lambda: make_engine(missing), capacity=16)
            dead.submit(row_for(self.snaps[0], self.bars))
            stats = dead.shutdown(drain=True, timeout=10)["stats"]
        self.assertEqual((stats["failed"], stats["persisted"]), (1, 0))

    def test_tools_on_postgres(self):
        for snap in self.snaps[-10:]:
            persist_technical_snapshot(self.engine, row_for(snap, self.bars))
        now = datetime(2026, 9, 23, 20, tzinfo=timezone.utc)
        out = io.StringIO()
        self.assertEqual(tools_main(["audit"], engine=self.engine, calendar=CAL, now=now, out=out), 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["rows"], report["problems"], report["content_hash_mismatches"]), (10, 0, []))
        out = io.StringIO()
        tools_main(["reconcile", "--symbol", "META", "--interval", "5m", "--start", "2026-09-21", "--end", "2026-09-22"],
                   engine=self.engine, calendar=CAL, now=now, out=out)
        report = json.loads(out.getvalue())
        self.assertEqual((report["expected"], report["stored_identities"], report["unexpected"]), (78, 10, []))
        out = io.StringIO()
        tools_main(["status"], engine=self.engine, calendar=CAL, now=now, out=out)
        self.assertEqual(json.loads(out.getvalue())["groups"][0]["rows"], 10)


if __name__ == "__main__":
    unittest.main()
