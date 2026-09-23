"""Deterministic queue/lifecycle checks and read-only reconciliation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from time import monotonic, sleep
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from persistence import macro_shadow as shadow
from persistence.database import transaction, PersistenceError
from persistence.models import event_history, event_provenance
from persistence.reconciliation import reconcile_macro_event
from persistence.repository import EventRepository
from tests import test_macro_persistence_adapter as adapter_tests
from tests.test_macro_persistence_adapter import sample, NOW, source_revision
from tests.test_macro_shadow_persistence import collect


def await_stats(writer, key, expected, timeout=5):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        stats = writer.get_persistence_stats()
        if stats[key] == expected:
            return stats
        sleep(0.005)
    raise AssertionError(f"Timed out waiting for {key}={expected}; stats={writer.get_persistence_stats()}")


class FakeEngine:
    def dispose(self):
        pass


class WriterOperationsTests(unittest.TestCase):
    def writer(self, **kwargs):
        writer = shadow.ShadowWriter(engine_factory=FakeEngine, **kwargs)
        self.addCleanup(writer.shutdown)
        return writer

    def assert_balanced(self, stats):
        self.assertEqual(stats["queued"], sum(stats[k] for k in (
            "persisted", "failed", "dropped_shutdown", "queue_depth", "in_flight")))

    def test_stats_count_commits_duplicates_and_failures(self):
        with patch.object(shadow, "persist_macro", side_effect=[{"duplicate": False}, {"duplicate": True}, PersistenceError("private")]):
            writer = self.writer()
            for _ in range(3): self.assertTrue(writer.submit(sample()))
            result = writer.shutdown()
        stats = result["stats"]
        self.assertTrue(result["stopped"])
        self.assertEqual((stats["queued"], stats["persisted"], stats["duplicate"], stats["failed"]), (3, 2, 1, 1))
        self.assertEqual((stats["worker_started"], stats["worker_stopped"]), (1, 1))
        self.assertIsNotNone(stats["last_success_at"])
        self.assertIsNotNone(stats["last_failure_at"])
        self.assertNotIn("private", str(stats))
        self.assert_balanced(stats)
        stats["queued"] = -100
        self.assertEqual(writer.get_persistence_stats()["queued"], 3)

    def test_tiny_queue_full_depth_and_collector_parity(self):
        entered, release = Event(), Event()
        def write(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"duplicate": False}
        with patch.object(shadow, "persist_macro", side_effect=write):
            writer = self.writer(capacity=1)
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                writer.submit(sample())
                stats = writer.get_persistence_stats()
                self.assertEqual((stats["queue_depth"], stats["in_flight"]), (1, 1))
                baseline, _ = collect(False)
                with self.assertLogs("macro_shadow", level="WARNING") as logs:
                    enabled, _ = collect(True, submit=writer.submit)
                    for _ in range(10): self.assertFalse(writer.submit(sample()))
                self.assertEqual(enabled, baseline)
                self.assertEqual(len(logs.output), 1)
                self.assertIn("queue full", logs.output[0])
                self.assertFalse(release.is_set())
                stats = writer.get_persistence_stats()
                self.assertEqual((stats["queued"], stats["dropped_queue_full"]), (2, 11))
                self.assert_balanced(stats)
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])

    def test_drain_timeout_counts_pending_and_reports_active_task(self):
        entered, release = Event(), Event()
        def write(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"duplicate": False}
        with patch.object(shadow, "persist_macro", side_effect=write):
            writer = self.writer()
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                writer.submit(sample())
                started = monotonic()
                result = writer.shutdown(timeout=0.02)
                self.assertLess(monotonic() - started, 1)
                self.assertFalse(result["stopped"])
                self.assertTrue(result["timed_out"])
                self.assertEqual(result["unprocessed"], 2)
                self.assertEqual(result["stats"]["dropped_shutdown"], 1)
                self.assertEqual(result["stats"]["in_flight"], 1)
                self.assertFalse(writer.submit(sample()))
                again = writer.shutdown(timeout=0)
                self.assertEqual(again["stats"]["drain_timeouts"], 1)
                self.assertEqual(again["stats"]["dropped_shutdown"], 1)
                self.assert_balanced(again["stats"])
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])
            self.assertEqual(writer.get_persistence_stats()["persisted"], 1)

    def test_immediate_shutdown_discards_pending_without_waiting_for_db(self):
        entered, release = Event(), Event()
        def write(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"duplicate": False}
        with patch.object(shadow, "persist_macro", side_effect=write):
            writer = self.writer()
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                writer.submit(sample())
                started = monotonic()
                result = writer.shutdown(drain=False, timeout=10)
                self.assertLess(monotonic() - started, 1)
                self.assertEqual(result["stats"]["dropped_shutdown"], 1)
                self.assertFalse(result["timed_out"])
                self.assertFalse(result["stopped"])
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])
            self.assertEqual(writer.get_persistence_stats()["persisted"], 1)

    def test_shutdown_idle_and_disabled_are_idempotent(self):
        writer = self.writer()
        first = writer.shutdown()
        self.assertTrue(first["stopped"])
        self.assertEqual(writer.shutdown(), first)
        with patch.object(shadow, "_writer", None), patch.object(shadow, "_shutdown_requested", False), \
             patch.object(shadow, "ShadowWriter", side_effect=AssertionError("must not start")):
            self.assertEqual(shadow.get_persistence_stats()["worker_started"], 0)
            self.assertTrue(shadow.shutdown()["stopped"])

    def test_module_lifecycle_stats_and_no_restart_after_shutdown(self):
        with patch.object(shadow, "_writer", None), patch.object(shadow, "_shutdown_requested", False), \
             patch.object(shadow, "_submission_stats", dict(dropped_initializing=0, failed_initializing=0,
                               rejected_shutdown=0, last_failure_at=None)), \
             patch.object(shadow, "ShadowWriter", side_effect=RuntimeError("secret")):
            self.assertFalse(shadow.submit_macro(sample()))
            self.assertEqual(shadow.get_persistence_stats()["failed_initializing"], 1)
            with shadow._lock:
                self.assertFalse(shadow.submit_macro(sample()))
            self.assertEqual(shadow.get_persistence_stats()["dropped_initializing"], 1)
            shadow.shutdown()
            self.assertFalse(shadow.submit_macro(sample()))
            self.assertEqual(shadow.get_persistence_stats()["rejected_shutdown"], 1)
            self.assertEqual(shadow.get_persistence_stats()["worker_started"], 0)

    def test_submit_shutdown_race_accounts_for_all_accepted_work(self):
        with patch.object(shadow, "persist_macro", return_value={"duplicate": False}):
            writer = self.writer(capacity=256)
            event = sample()
            with ThreadPoolExecutor(max_workers=8) as pool:
                submissions = [pool.submit(writer.submit, event) for _ in range(100)]
                writer.shutdown(drain=False)
                accepted = sum(f.result() for f in submissions)
            result = writer.shutdown()
        self.assertTrue(result["stopped"])
        self.assertEqual(result["stats"]["queued"], accepted)
        self.assertEqual(result["stats"]["rejected_shutdown"], 100 - accepted)
        self.assert_balanced(result["stats"])

    def test_invalid_limits_and_invalid_snapshot(self):
        for capacity in (0, -1, 5000, True):
            with self.assertRaises(ValueError): shadow.ShadowWriter(capacity=capacity)
        writer = self.writer()
        for timeout in (None, float("inf"), float("nan"), -1, 31):
            with self.assertRaises(ValueError): writer.shutdown(timeout=timeout)
        self.assertFalse(writer.submit({"invalid": float("nan")}))
        self.assertEqual(writer.get_persistence_stats()["dropped_invalid"], 1)

    def test_worker_survives_task_and_logging_failure(self):
        with patch.object(shadow, "persist_macro", side_effect=[RuntimeError("private"), {"duplicate": False}]), \
             patch.object(shadow.logger, "warning", side_effect=RuntimeError("log unavailable")):
            writer = self.writer()
            writer.submit(sample())
            writer.submit(sample())
            result = writer.shutdown()
        self.assertTrue(result["stopped"])
        self.assertEqual((result["stats"]["failed"], result["stats"]["persisted"]), (1, 1))

    def test_disposal_failure_is_redacted_and_worker_stops(self):
        with patch.object(FakeEngine, "dispose", side_effect=RuntimeError("secret")), \
             patch.object(shadow, "persist_macro", return_value={"duplicate": False}), \
             self.assertLogs("macro_shadow", level="WARNING") as logs:
            writer = self.writer()
            writer.submit(sample())
            result = writer.shutdown()
        self.assertTrue(result["stopped"])
        self.assertEqual(result["stats"]["cleanup_failed"], 1)
        self.assertEqual(result["stats"]["failed"], 0)
        self.assertNotIn("secret", str(logs.output))
        self.assert_balanced(result["stats"])

    def test_concurrent_producers_and_stats_snapshots(self):
        with patch.object(shadow, "persist_macro", return_value={"duplicate": False}):
            writer = self.writer(capacity=128)
            event = sample()
            def produce(_):
                accepted = writer.submit(event)
                self.assert_balanced(writer.get_persistence_stats())
                return accepted
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertTrue(all(pool.map(produce, range(100))))
            stats = writer.shutdown()["stats"]
            self.assertEqual((stats["queued"], stats["persisted"], stats["queue_depth"]), (100, 100, 0))
            self.assert_balanced(stats)


class ReconciliationTests(unittest.TestCase):
    setUp = adapter_tests.AdapterTests.setUp

    def reconcile(self, event, **kwargs):
        with transaction(self.engine) as session:
            return reconcile_macro_event(event, EventRepository(session), **kwargs)

    def test_success_and_no_ai_not_applicable(self):
        event = sample()
        shadow.persist_macro(self.engine, event, NOW)
        before = deepcopy(event)
        result = self.reconcile(event)
        self.assertEqual(result["mismatches"], [])
        self.assertIsNone(result["ai_match"])
        self.assertEqual(event, before)

    def test_missing_event_and_missing_version(self):
        event = sample()
        result = self.reconcile(event)
        self.assertFalse(result["event_found"])
        self.assertFalse(result["score_match"])
        shadow.persist_macro(self.engine, event, NOW)
        event["metrics"]["headline_cpi_sa"]["value"] += 1
        result = self.reconcile(event)
        self.assertTrue(result["event_found"])
        self.assertFalse(result["version_found"])

    def test_expected_ai_and_history_mismatches(self):
        event = sample()
        event.update(ai_summary="Visible", ai_sentiment="NEUTRAL", ai_confidence=80,
                     ai_why_it_matters="Context", ai_event_type="macro")
        row = shadow.persist_macro(self.engine, event, NOW)
        self.assertTrue(self.reconcile(event)["ai_match"])
        # Deliberate test-fixture corruption; reconciliation itself only reads.
        with transaction(self.engine) as session:
            session.execute(event_history.delete().where(event_history.c.event_version_id == row["id"]))
        result = self.reconcile(event)
        for key in ("score_match", "decision_match", "ai_match"):
            self.assertFalse(result[key])
            self.assertIn(key, result["mismatches"])

    def test_provenance_mismatch_and_historical_current(self):
        event = sample()
        row = shadow.persist_macro(self.engine, event, NOW)
        newer = deepcopy(event)
        newer["metrics"]["headline_cpi_sa"]["value"] += 1
        source_revision(newer)
        shadow.persist_macro(self.engine, newer, NOW)
        self.assertIn("current_version_match", self.reconcile(event)["mismatches"])
        self.assertEqual(self.reconcile(event, expect_current=False)["mismatches"], [])
        with transaction(self.engine) as session:
            session.execute(event_provenance.delete().where(event_provenance.c.event_version_id == row["id"]))
        self.assertFalse(self.reconcile(event)["provenance_match"])

    def test_read_only_queries_and_missing_unscored_outcomes(self):
        event = sample(scored=False)
        shadow.persist_macro(self.engine, event, NOW)
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            result = self.reconcile(event)
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        self.assertEqual(result["mismatches"], [])
        self.assertIsNone(result["score_match"])
        self.assertIsNone(result["decision_match"])
        self.assertTrue(statements)
        self.assertTrue(all(s.lstrip().upper().startswith(("SELECT", "BEGIN")) for s in statements), statements)

    def test_duplicate_diagnostic_counts_all_components(self):
        event = sample()
        first = shadow.persist_macro(self.engine, event, NOW, report=True)
        second = shadow.persist_macro(self.engine, event, NOW, report=True)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        event["impact_score"] -= 1
        changed_score = shadow.persist_macro(self.engine, event, NOW, report=True)
        self.assertFalse(changed_score["duplicate"])
        self.assertEqual(changed_score["version"]["id"], first["version"]["id"])
