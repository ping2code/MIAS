"""Phase 2S-A: Treasury shadow burst capacity (bounded, non-blocking, counted overflow).

Phase 2S measured a healthy live Treasury cycle submitting 115 observations (37 yield
observations, 78 release/auction items; 105 historical) into a 64-slot queue: the same 50
tail observations were dropped every cycle. These tests reproduce that burst, prove the
configured Treasury capacity absorbs it, and prove the queue is still bounded and
non-blocking. Other families keep the shared default capacity.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
import runpy
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

from tests.test_treasury_pipeline import treasury  # noqa: F401  (imports shared.config with .env disabled)
from persistence import macro_shadow, treasury_shadow as shadow
from persistence.database import PersistenceError
from shared import queue_settings
from tests import test_persistence_postgres as foundation
from tests.test_treasury_shadow_persistence import FakeEngine

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
YIELDS, RELEASES, CURRENT = 37, 78, 10  # The measured Phase 2S live composition (115 total, 105 historical).


def burst():
    """115 distinct, collector-shaped Treasury observations built from the pinned corpus templates."""
    from tests import treasury_readiness_corpus as corpus
    rows = [r["event"] for r in corpus.load_corpus()[1]]
    yields = [e for e in rows if e["event_type"] == "treasury_yield_observation"]
    releases = [e for e in rows if e["event_type"] == "treasury_release"]
    events = []
    for index in range(YIELDS + RELEASES):
        template = deepcopy((yields if index < YIELDS else releases)[index % (len(yields) if index < YIELDS else len(releases))])
        tag = f"mias-burst-{index:03d}"
        template["event_id"] = hashlib.sha256(f"phase2sa|{tag}".encode()).hexdigest()
        template["release_id"] = f"{template.get('release_id') or 'press:x'}:{tag}"
        template["url"] = f"{template['url'].rstrip('/')}/{tag}"
        template["headline"] = f"{template['headline']} ({tag})"
        events.append((template, index >= YIELDS + RELEASES - CURRENT))  # (event, make_current)
    return events


def gate():
    entered, release = Event(), Event()
    def write(*args, **kwargs):
        entered.set()
        release.wait(10)
        return {"duplicate": False, "promotion": "first"}
    return entered, release, write


class TreasuryCapacityConfigTests(unittest.TestCase):
    def config(self, env):
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, env, clear=True):
            return runpy.run_path("shared/config.py")

    def test_default_and_valid_values(self):
        self.assertEqual(self.config({})["TREASURY_PERSISTENCE_QUEUE_SIZE"], 256)
        for value in ("64", "115", "512", "4096"):
            self.assertEqual(self.config({"TREASURY_PERSISTENCE_QUEUE_SIZE": value})["TREASURY_PERSISTENCE_QUEUE_SIZE"],
                             int(value))
        self.assertGreater(queue_settings.TREASURY_DEFAULT_QUEUE_SIZE, YIELDS + RELEASES)

    def test_invalid_values_fail_fast_like_other_settings(self):
        for value in ("", "abc", "12.5", "63", "0", "-1", "4097"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config({"TREASURY_PERSISTENCE_QUEUE_SIZE": value})
        with self.assertRaises(ValueError):
            queue_settings.treasury_queue_size({"TREASURY_PERSISTENCE_QUEUE_SIZE": None})

    def test_writer_wiring_and_lazy_lifecycle(self):
        with patch.dict(os.environ, {}, clear=True):
            writer = shadow.TreasuryShadowWriter(engine_factory=FakeEngine)
        self.addCleanup(writer.shutdown)
        self.assertEqual(writer.capacity, 256)
        with patch.dict(os.environ, {"TREASURY_PERSISTENCE_QUEUE_SIZE": "128"}):
            configured = shadow.TreasuryShadowWriter(engine_factory=FakeEngine)
        self.addCleanup(configured.shutdown)
        self.assertEqual(configured.capacity, 128)
        explicit = shadow.TreasuryShadowWriter(engine_factory=FakeEngine, capacity=2)
        self.addCleanup(explicit.shutdown)
        self.assertEqual(explicit.capacity, 2)  # Explicit capacities (tests, tools) are unchanged.
        from tests import test_shadow_lifecycle as lifecycle
        from contextlib import ExitStack
        with ExitStack() as stack:  # The lazily created production writer uses the configured size.
            lifecycle.fresh(shadow, stack)
            stack.enter_context(patch.dict(os.environ, {"TREASURY_PERSISTENCE_QUEUE_SIZE": "300"}))
            stack.enter_context(patch.object(shadow.TreasuryShadowWriter.__init__, "__defaults__", (FakeEngine, None)))
            self.assertTrue(shadow.submit_treasury(burst()[0][0]))
            self.assertEqual(shadow._writer.capacity, 300)
            shadow._writer.shutdown()
        with ExitStack() as stack:  # An invalid value fails initialization only: counted, never raised to the collector.
            lifecycle.fresh(shadow, stack)
            stack.enter_context(patch.dict(os.environ, {"TREASURY_PERSISTENCE_QUEUE_SIZE": "bogus"}))
            with self.assertLogs("treasury_shadow", level="WARNING"):
                self.assertFalse(shadow.submit_treasury(burst()[0][0]))
            self.assertEqual(shadow.get_persistence_stats()["failed_initializing"], 1)
            self.assertIsNone(shadow._writer)

    def test_other_families_keep_the_shared_default(self):
        from persistence import fed_shadow, geopolitical_shadow, news_shadow, sec_shadow
        with patch.dict(os.environ, {"TREASURY_PERSISTENCE_QUEUE_SIZE": "4096"}):
            writers = [macro_shadow.ShadowWriter(engine_factory=FakeEngine), fed_shadow.FedShadowWriter(engine_factory=FakeEngine),
                       sec_shadow.SecShadowWriter(engine_factory=FakeEngine), news_shadow.NewsShadowWriter(engine_factory=FakeEngine),
                       geopolitical_shadow.GeopoliticalShadowWriter(engine_factory=FakeEngine)]
        for writer in writers:
            self.addCleanup(writer.shutdown)
            with self.subTest(writer=type(writer).__name__):
                self.assertEqual(writer.capacity, queue_settings.SHARED_DEFAULT_QUEUE_SIZE)


class TreasuryBurstTests(unittest.TestCase):
    def writer(self, **kwargs):
        writer = shadow.TreasuryShadowWriter(engine_factory=FakeEngine, **kwargs)
        self.addCleanup(writer.shutdown)
        return writer

    def submit_burst(self, writer, events):
        started = monotonic()
        accepted = [writer.submit(event, make_current=current) for event, current in events]
        return accepted, monotonic() - started

    def test_phase2s_burst_dropped_50_at_the_old_capacity(self):
        entered, release, write = gate()
        with patch.object(shadow, "persist_treasury", side_effect=write), self.assertLogs("treasury_shadow", "WARNING"):
            writer = self.writer(capacity=64)
            events = burst()
            writer.submit(*events[0][:1], make_current=events[0][1])
            self.assertTrue(entered.wait(2))  # One in flight, then the rest of the burst arrives at once.
            accepted, _ = self.submit_burst(writer, events[1:])
            release.set()
            stats = writer.shutdown(timeout=10)["stats"]
        self.assertEqual((1 + sum(accepted), stats["dropped_queue_full"]), (65, 50))  # The exact pre-fix evidence.

    def test_configured_capacity_absorbs_the_measured_burst(self):
        entered, release, write = gate()
        with patch.object(shadow, "persist_treasury", side_effect=write), patch.dict(os.environ, {}, clear=True):
            writer = self.writer()
            events = burst()
            writer.submit(events[0][0], make_current=events[0][1])
            self.assertTrue(entered.wait(2))
            accepted, elapsed = self.submit_burst(writer, events[1:])
            self.assertEqual(writer.get_persistence_stats()["queue_depth"], len(events) - 1)
            release.set()
            stats = writer.shutdown(timeout=10)["stats"]
        self.assertTrue(all(accepted))
        self.assertLess(elapsed, 1.0)  # Submission stays non-blocking even while the database is stalled.
        self.assertEqual((stats["queued"], stats["persisted"], stats["dropped_queue_full"], stats["failed"]),
                         (len(events), len(events), 0, 0))
        self.assertEqual((stats["queue_depth"], stats["in_flight"]), (0, 0))

    def test_deliberate_overflow_is_still_bounded_counted_and_non_blocking(self):
        entered, release, write = gate()
        overflow = burst() * 4  # 460 submissions against a 256 queue while the worker is stalled.
        with patch.object(shadow, "persist_treasury", side_effect=write), patch.dict(os.environ, {}, clear=True), \
             self.assertLogs("treasury_shadow", "WARNING") as logs:
            writer = self.writer()
            writer.submit(overflow[0][0], make_current=overflow[0][1])
            self.assertTrue(entered.wait(2))
            accepted, elapsed = self.submit_burst(writer, overflow[1:])
            mid = writer.get_persistence_stats()
            release.set()
            stats = writer.shutdown(timeout=15)["stats"]
        self.assertLess(elapsed, 1.0)
        self.assertEqual((mid["queue_depth"], sum(accepted)), (256, 256))
        self.assertEqual(stats["dropped_queue_full"], len(overflow) - 1 - 256)
        self.assertEqual(stats["queued"] + stats["dropped_queue_full"], len(overflow))  # No silent drops.
        self.assertEqual((stats["persisted"], stats["queue_depth"], stats["in_flight"]), (257, 0, 0))
        self.assertTrue(stats["last_failure_at"])
        self.assertTrue(any("queue full" in line for line in logs.output))

    def test_database_unavailable_counts_failures_without_drops_or_blocking(self):
        with patch.dict(os.environ, {}, clear=True), self.assertLogs("treasury_shadow", "WARNING") as logs:
            writer = shadow.TreasuryShadowWriter(engine_factory=lambda: (_ for _ in ()).throw(PersistenceError("private")))
            accepted, elapsed = self.submit_burst(writer, burst())
            result = writer.shutdown(timeout=15)
        stats = result["stats"]
        self.assertTrue(all(accepted) and elapsed < 1.0 and result["stopped"])
        self.assertEqual((stats["failed"], stats["persisted"], stats["dropped_queue_full"], stats["queue_depth"]),
                         (115, 0, 0, 0))
        self.assertNotIn("private", str(logs.output))
        with patch.object(shadow, "persist_treasury", return_value={"duplicate": False, "promotion": "first"}), \
             patch.dict(os.environ, {}, clear=True):
            recovered = self.writer()  # Recovery: a fresh writer persists new work; nothing is replayed.
            self.submit_burst(recovered, burst()[:5])
            self.assertEqual(recovered.shutdown(timeout=10)["stats"]["persisted"], 5)

    def test_shutdown_guarantees_hold_with_the_larger_queue(self):
        with patch.dict(os.environ, {}, clear=True):
            entered, release, write = gate()
            with patch.object(shadow, "persist_treasury", side_effect=write):
                writer = self.writer()
                writer.submit(burst()[0][0])
                self.assertTrue(entered.wait(2))
                self.submit_burst(writer, burst()[1:])
                with self.assertLogs("treasury_shadow", "WARNING"):
                    timed = writer.shutdown(timeout=0.05)  # Bounded drain timeout: pending discarded and counted.
                self.assertEqual((timed["timed_out"], timed["stats"]["dropped_shutdown"], timed["stats"]["in_flight"]),
                                 (True, 114, 1))
                self.assertEqual(writer.shutdown(timeout=0)["stats"]["drain_timeouts"], 1)  # Repeated shutdown.
                release.set()
                self.assertTrue(writer.shutdown(timeout=5)["stopped"])
            entered, release, write = gate()
            with patch.object(shadow, "persist_treasury", side_effect=write):
                writer = self.writer()
                writer.submit(burst()[0][0])
                self.assertTrue(entered.wait(2))
                self.submit_burst(writer, burst()[1:])
                immediate = writer.shutdown(drain=False, timeout=10)
                self.assertEqual((immediate["stats"]["dropped_shutdown"], immediate["unprocessed"]), (114, 115))
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])
            with patch.object(shadow, "persist_treasury", return_value={"duplicate": True}):
                graceful = self.writer()  # Restart: a new writer drains a full burst gracefully.
                self.submit_burst(graceful, burst())
                result = graceful.shutdown(drain=True, timeout=10)
            self.assertEqual((result["stopped"], result["unprocessed"], result["stats"]["duplicate"]), (True, 0, 115))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLTreasuryBurstTests(unittest.TestCase):
    """The measured burst through the real Treasury shadow path into real PostgreSQL."""
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema

    def reconcile_all(self, events):
        from persistence.database import transaction
        from persistence.reconciliation import reconcile_treasury_event
        from persistence.repository import EventRepository
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            return [reconcile_treasury_event(e, repo, expect_current=current)["mismatches"] for e, current in events]

    def test_measured_burst_fully_persists_and_reconciles(self):
        events = burst()
        with patch.dict(os.environ, {}, clear=True):
            writer = shadow.TreasuryShadowWriter(engine_factory=lambda: self.engine)
        for event, current in events:
            self.assertTrue(writer.submit(event, make_current=current))
        stats = writer.shutdown(timeout=30)["stats"]
        self.assertEqual((stats["queued"], stats["persisted"], stats["dropped_queue_full"], stats["failed"],
                          stats["queue_depth"], stats["in_flight"]), (115, 115, 0, 0, 0, 0))
        self.assertEqual(self.reconcile_all(events), [[]] * 115)
        restarted = shadow.TreasuryShadowWriter(engine_factory=lambda: self.engine)  # Restart: DB-backed duplicates.
        for event, current in events:
            restarted.submit(event, make_current=current)
        again = restarted.shutdown(timeout=30)["stats"]
        self.assertEqual((again["duplicate"], again["dropped_queue_full"]), (115, 0))

    def test_old_capacity_reproduces_the_finding_on_real_postgresql(self):
        events = burst()
        entered, release = Event(), Event()
        real = shadow.persist_treasury
        def first_blocks(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                release.wait(10)
            return real(*args, **kwargs)
        with patch.object(shadow, "persist_treasury", side_effect=first_blocks), \
             self.assertLogs("treasury_shadow", "WARNING"):
            writer = shadow.TreasuryShadowWriter(engine_factory=lambda: self.engine, capacity=64)
            writer.submit(events[0][0], make_current=events[0][1])
            self.assertTrue(entered.wait(5))
            for event, current in events[1:]:
                writer.submit(event, make_current=current)
            release.set()
            stats = writer.shutdown(timeout=30)["stats"]
        self.assertEqual((stats["persisted"], stats["dropped_queue_full"]), (65, 50))
        missing = [m for m in self.reconcile_all(events) if m]
        self.assertEqual(len(missing), 50)  # Exactly the dropped tail is absent, and only as "not found".
        self.assertTrue(all("event_found" in m for m in missing))


if __name__ == "__main__":
    unittest.main()
