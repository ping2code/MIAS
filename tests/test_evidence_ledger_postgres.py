"""Phase 6 evidence ledger (migration 0007) on a real disposable PostgreSQL (TEST_DATABASE_URL, mias_test_* only).

Covered: upgrade/downgrade/re-upgrade, downgrade refusal with rows, compare_metadata, check constraints, partial unique
indexes, append-only triggers (UPDATE, DELETE, TRUNCATE), insert/duplicate/conflict, true concurrent identical and
conflicting writes, snapshot soft reference verified by the audit, and a privacy canary over every stored value.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
import os
import unittest
from threading import Barrier
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from persistence.config import DatabaseSettings, require_test_database
from persistence.database import PersistenceError, make_engine, transaction
from persistence.models import metadata, technical_evidence_ledger
from persistence.technical_evidence_ledger import EvidenceLedgerRepository, build_failure
from persistence.technical_snapshot_repository import persist_technical_snapshot
from persistence import technical_evidence_tools as tools
from tests import test_persistence as unit
from tests.test_evidence_ledger import CAL, SESSION, common, evidence
from tests.test_technical_snapshot_persistence import row_for, snapshots

TABLE = "technical_evidence_ledger"


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class EvidenceLedgerPostgresTests(unittest.TestCase):
    def setUp(self):
        self.settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.admin = make_engine(self.settings)
        self.schema = "mias_phase6_" + uuid4().hex
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

    def record(self, row):
        with transaction(self.engine) as session:
            return EvidenceLedgerRepository(session).record_collected(row)

    def rows(self):
        with transaction(self.engine) as session:
            return tools.load_rows(session)

    def run_tool(self, command_name):
        out = io.StringIO()
        pin = dict(prospective_start_session=SESSION, earliest_evaluation_session=date(2027, 3, 29))
        code = tools.main([command_name], engine=self.engine, calendar=CAL, pin=pin, out=out,
                          now=datetime(2026, 9, 29, 21, tzinfo=timezone.utc))
        return code, json.loads(out.getvalue())

    def test_upgrade_downgrade_reupgrade_and_metadata(self):
        self.assertIn(TABLE, self.tables())
        for _ in range(2):
            self.migrate("downgrade", "0006_technical_numeric_volume")
            self.assertNotIn(TABLE, self.tables())
            self.assertIn("technical_snapshots", self.tables())
            with self.engine.connect() as connection:
                self.assertIsNone(connection.execute(sa.text(
                    "SELECT 1 FROM pg_proc WHERE proname = 'technical_evidence_ledger_append_only' "
                    "AND pronamespace = current_schema()::regnamespace")).scalar())
            self.migrate("upgrade", "head")
        with self.engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
            self.assertTrue(tools.triggers_present(connection))
            indexes = {i["name"]: i for i in sa.inspect(connection).get_indexes(TABLE)}
        self.assertTrue(indexes["uq_technical_evidence_ledger_accepted"]["unique"])
        self.assertTrue(indexes["uq_technical_evidence_ledger_conflict"]["unique"])
        for name in ("ix_technical_evidence_ledger_session", "ix_technical_evidence_ledger_status",
                     "ix_technical_evidence_ledger_collected_at", "ix_technical_evidence_ledger_conflicts_with"):
            self.assertIn(name, indexes)

    def test_downgrade_refused_with_rows(self):
        self.record(evidence())
        with self.assertRaisesRegex(RuntimeError, "refusing to downgrade"):
            self.migrate("downgrade", "0006_technical_numeric_volume")
        self.assertIn(TABLE, self.tables())
        self.assertEqual(len(self.rows()), 1)

    def test_triggers_block_update_delete_truncate(self):
        first = self.record(evidence())
        for statement in (sa.update(technical_evidence_ledger).values(bar_count=1),
                          sa.delete(technical_evidence_ledger), sa.text(f"TRUNCATE {TABLE}")):
            with self.assertRaises(PersistenceError):
                with transaction(self.engine) as session:
                    session.execute(statement)
        rows = self.rows()
        self.assertEqual((len(rows), rows[0]["id"], rows[0]["bar_count"]), (1, first["id"], 78))
        with self.engine.connect() as connection:
            with self.assertRaisesRegex(sa.exc.DBAPIError, "append-only"):
                connection.execute(sa.text(f"DELETE FROM {TABLE}"))

    def test_check_constraints(self):
        good = dict(evidence(), id=str(uuid4()), recorded_at=datetime.now(timezone.utc))
        bad_rows = [dict(good, record_status="pending"), dict(good, interval="30m"), dict(good, error_kind="transport"),
                    dict(good, bar_content_hash="abc"), dict(good, code_commit="abc"), dict(good, bar_count=0),
                    dict(good, data_delay_seconds=-1),
                    dict(good, record_status="failed"),  # Failed rows carry no evidence hashes.
                    dict(good, record_status="conflict")]  # Conflicts need conflicts_with.
        for row in bad_rows:
            with self.assertRaises(PersistenceError, msg=str({k: row[k] for k in ("record_status", "interval")})):
                with transaction(self.engine) as session:
                    session.execute(sa.insert(technical_evidence_ledger).values(**row))
        self.assertEqual(self.rows(), [])

    def test_insert_duplicate_conflict_preserves_original(self):
        first = self.record(evidence())
        duplicate = self.record(evidence(code_commit="f" * 40))
        conflict = self.record(evidence(shift=Decimal("0.5")))
        repeat = self.record(evidence(shift=Decimal("0.5")))
        other = self.record(evidence(shift=Decimal("0.7")))
        self.assertEqual([r["outcome"] for r in (first, duplicate, conflict, repeat, other)],
                         ["inserted", "duplicate", "conflict", "conflict", "conflict"])
        self.assertIsNone(repeat["conflict_id"])
        rows = self.rows()
        accepted = [r for r in rows if r["record_status"] == "collected"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual((accepted[0]["id"], accepted[0]["code_commit"]), (first["id"], common()["code_commit"]))
        self.assertEqual(accepted[0]["evidence_hash"], evidence()["evidence_hash"])
        self.assertEqual(sum(r["record_status"] == "conflict" for r in rows), 2)

    def test_concurrent_identical_and_conflicting_writes(self):
        for rows, expected in (([evidence("MSFT")] * 8, {"inserted": 1, "duplicate": 7}),
                               ([evidence("JPM", shift=Decimal(i) / 10) for i in range(8)],
                                {"inserted": 1, "conflict": 7})):
            barrier = Barrier(len(rows))

            def write(row):
                barrier.wait()
                return self.record(row)["outcome"]

            with ThreadPoolExecutor(len(rows)) as pool:
                outcomes = list(pool.map(write, rows))
            self.assertEqual({k: outcomes.count(k) for k in set(outcomes)}, expected)
        rows = self.rows()
        self.assertEqual(sum(r["record_status"] == "collected" for r in rows), 2)
        self.assertEqual(sum(r["record_status"] == "conflict" for r in rows), 7)

    def test_failures_and_audit_with_snapshot_reference(self):
        bars, snaps = snapshots()
        snapshot = row_for(snaps[-1], bars)
        persist_technical_snapshot(self.engine, snapshot)
        stamp = datetime.fromisoformat(snapshot["snapshot_timestamp"])
        with transaction(self.engine) as session:
            EvidenceLedgerRepository(session).record_failure(build_failure(error_kind="transport",
                                                                           error_detail="timeout", **common()))
        # The snapshot fixture is a synthetic 5m META series; the soft reference needs only identity + hash.
        good = evidence(snapshot_timestamp=stamp, snapshot_content_hash=snapshot["content_hash"])
        self.assertEqual(self.record(good)["outcome"], "inserted")
        code, result = self.run_tool("audit")
        self.assertEqual((code, result["problems"], result["triggers_present"]), (0, 0, True))
        wrong = evidence("NVDA", snapshot_timestamp=stamp, snapshot_content_hash="0" * 64)
        self.record(wrong)
        code, result = self.run_tool("audit")
        self.assertEqual(code, 1)
        self.assertEqual(len(result["snapshot_missing"]), 1)  # NVDA has no snapshot at that identity.
        mismatched = evidence("META", "1h", snapshot_timestamp=stamp, snapshot_content_hash="0" * 64)
        self.record(mismatched)
        code, result = self.run_tool("audit")
        self.assertEqual(len(result["snapshot_missing"]), 2)  # 1h identity also absent; content hash never trusted.
        code, result = self.run_tool("reconcile")
        self.assertEqual((code, result["expected_sessions"], result["failures_without_success"]), (0, 2, 0))

    def test_privacy_canary(self):
        self.record(evidence())
        with transaction(self.engine) as session:
            EvidenceLedgerRepository(session).record_failure(build_failure(
                error_kind="provider_auth", error_detail="status 401", **common("NVDA")))
        with self.engine.connect() as connection:
            dump = json.dumps([dict(r._mapping) for r in connection.execute(sa.text(f"SELECT * FROM {TABLE}"))],
                              default=str)
            columns = {c["name"] for c in sa.inspect(connection).get_columns(TABLE)}
        self.assertNotIn("never-printed-canary-key", dump)
        self.assertNotIn("100.01", dump)  # No prices.
        self.assertFalse(columns & {"open", "high", "low", "close", "volume", "price", "payload", "raw", "api_key",
                                    "technical_state", "forward_return"})


if __name__ == "__main__":
    unittest.main()
