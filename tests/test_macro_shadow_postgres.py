"""Phase 2B fixture suite on independently committed PostgreSQL transactions."""
import os
import socket
import unittest
from unittest.mock import patch

from tests import test_macro_persistence_adapter as adapter
from tests import test_persistence_postgres as foundation
from tests.test_macro_shadow_persistence import collect
from persistence import macro_shadow as shadow
from persistence.database import transaction
from persistence.repository import EventRepository
from persistence.models import events
import sqlalchemy as sa


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLMacroTests(adapter.AdapterTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema

    def test_actual_worker_commits_collector_result(self):
        baseline, _ = collect(False, ai=True)
        writer = shadow.ShadowWriter(engine_factory=lambda: self.engine)
        enabled, _ = collect(True, ai=True, submit=writer.submit)
        self.assertTrue(writer.close(timeout=5))
        self.assertEqual(enabled, baseline)
        with transaction(self.engine) as session:
            event_id = session.execute(sa.select(events.c.id)).scalar_one()
            repo = EventRepository(session)
            row = repo.current(event_id)
            self.assertEqual(row["headline"], enabled[0][0][0]["headline"])
            self.assertEqual({r["kind"] for r in repo.history(row["id"])}, {"score", "decision", "ai"})

    def test_worker_repository_failure_rolls_back_without_collector_effect(self):
        baseline, _ = collect(False)
        with patch.object(EventRepository, "append_history", side_effect=RuntimeError("private")), \
             self.assertLogs("macro_shadow", level="WARNING"):
            writer = shadow.ShadowWriter(engine_factory=lambda: self.engine)
            enabled, _ = collect(True, submit=writer.submit)
            self.assertTrue(writer.close(timeout=5))
        self.assertEqual(enabled, baseline)
        with transaction(self.engine) as session:
            self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(events)).scalar_one(), 0)

    def test_actual_postgres_connection_failure_does_not_affect_collector(self):
        # Reserve a local port without listening: no service/non-test DB can occupy it.
        settings = foundation.require_test_database(foundation.DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        reserved = self.enterContext(socket.socket())
        reserved.bind(("127.0.0.1", 0))
        missing = foundation.DatabaseSettings(url=settings.url.set(host="127.0.0.1", port=reserved.getsockname()[1]),
                                               connect_timeout_seconds=2)
        baseline, _ = collect(False)
        with self.assertLogs("macro_shadow", level="WARNING"):
            writer = shadow.ShadowWriter(engine_factory=lambda: foundation.make_engine(missing))
            enabled, _ = collect(True, submit=writer.submit)
            self.assertTrue(writer.close(timeout=5))
        self.assertEqual(enabled, baseline)

    def test_concurrent_full_snapshot_is_idempotent(self):
        adapted = adapter.adapt_macro(adapter.sample(), adapter.NOW)
        def write(repo):
            row = repo.record(**adapted["record"])
            for key, values in adapted["provenance"]:
                repo.add_provenance(row["id"], key, **values)
            for kind, values in adapted["histories"]:
                repo.append_history(row["id"], kind, values)
            return row
        first, second = foundation.PostgreSQLTests.race(self, write, write)
        self.assertEqual(first["id"], second["id"])
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            self.assertEqual(len(repo.versions(first["event_id"])), 1)
            self.assertEqual(len(repo.provenance(first["id"])), 3)
            self.assertEqual(len(repo.history(first["id"])), 2)
