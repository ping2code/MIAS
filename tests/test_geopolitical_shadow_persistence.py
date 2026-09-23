"""Geopolitical shadow must not change collector output, Redis, AI or delivery; writer lifecycle."""
from concurrent.futures import ThreadPoolExecutor
import os
import runpy
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

from tests import geopolitical_readiness_corpus as corpus
from tests.test_geopolitical_pipeline import MemoryRedis, geo
from tests.test_geopolitical_persistence_adapter import sample
from tests.test_treasury_shadow_persistence import await_stats, FakeEngine
from persistence import macro_shadow
from persistence import geopolitical_shadow as shadow
from persistence.database import PersistenceError


def replay_steps(enabled, submit=None, steps=None):
    """Run the whole corpus document sequence; return every observable collector output."""
    redis, outputs, submitted = MemoryRedis(), [], []
    for label, name, clock, change in steps or corpus.STEPS:
        redis.now = clock
        if change:
            corpus.expire(redis, change)
        result, captured = corpus.run_collector([corpus.DOCS[name]], redis, enabled=enabled, submit=submit)
        outputs.append((label, result))
        submitted.extend((label, event, kwargs) for event, kwargs in captured)
    return outputs, submitted


class GeopoliticalCollectorShadowTests(unittest.TestCase):
    def test_disabled_does_not_initialize_or_submit(self):
        with patch.object(shadow, "runtime_engine", side_effect=AssertionError("DB forbidden")), \
             patch.object(shadow, "GeopoliticalShadowWriter", side_effect=AssertionError("worker forbidden")):
            outputs, submitted = replay_steps(False)
        self.assertEqual(submitted, [])
        self.assertIsNone(shadow._writer)
        self.assertEqual(dict(outputs)["bis_final"][1]["delivered"], 1)

    def test_enabled_identical_output_redis_ai_and_delivery(self):
        baseline, _ = replay_steps(False)
        enabled, submitted = replay_steps(True)
        self.assertEqual(enabled, baseline)  # Events, stats, Redis state, Telegram calls, AI calls.
        labels = [label for label, _, _ in submitted]
        self.assertNotIn("missing_date", labels)
        self.assertNotIn("unresolved_identity", labels)
        self.assertNotIn("broad_no_symbols", labels)
        processed = {label: event for label, event, kwargs in submitted if kwargs["make_current"]}
        for label, (events, stats, *_rest) in enabled:
            if stats["processed"]:
                self.assertEqual(processed[label], events[0])  # Exactly the collector result.
        skipped = [(label, kwargs) for label, _, kwargs in submitted if not kwargs["make_current"]]
        self.assertEqual(skipped, [("stale_companion", {"make_current": False})])

    def test_submit_exception_has_no_effect_or_secret_logs(self):
        baseline, _ = replay_steps(False)
        with patch.object(geo, "_shadow_last_failure", float("-inf")), \
             self.assertLogs("geopolitical_collector", level="WARNING") as logs:
            enabled, _ = replay_steps(True, submit=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret-password")))
        self.assertEqual(enabled, baseline)
        self.assertNotIn("secret-password", str(logs.output))
        self.assertEqual(sum("Geopolitical shadow submission failed" in line for line in logs.output), 1)

    def test_companion_and_duplicate_submit_cached_result_without_reanalysis(self):
        steps = [s for s in corpus.STEPS if s[0] in {"bis_final", "fr_companion", "duplicate"}]
        outputs, submitted = replay_steps(True, steps=steps)
        self.assertEqual(sum(result[4] for _, result in outputs), 1)  # One AI analysis.
        self.assertEqual([len(event["provenance"]) for _, event, _ in submitted], [1, 2, 2])
        self.assertEqual(len({event["event_id"] for _, event, _ in submitted}), 1)

    def test_db_unavailable_worker_does_not_affect_collector(self):
        baseline, _ = replay_steps(False)
        with self.assertLogs("geopolitical_shadow", level="WARNING") as logs:
            writer = shadow.GeopoliticalShadowWriter(engine_factory=lambda: (_ for _ in ()).throw(RuntimeError("password")))
            enabled, submitted = replay_steps(True, submit=writer.submit)
            result = writer.shutdown(timeout=5)
        self.assertEqual(enabled, baseline)
        self.assertTrue(result["stopped"])
        self.assertEqual((result["stats"]["failed"], result["stats"]["queued"]), (len(submitted), len(submitted)))
        self.assertNotIn("password", str(logs.output))

    def test_slow_db_and_full_queue_do_not_block_delivery(self):
        started, release = Event(), Event()
        def factory():
            started.set()
            release.wait(5)
            raise RuntimeError("unavailable")
        writer = shadow.GeopoliticalShadowWriter(engine_factory=factory, capacity=1)
        try:
            self.assertTrue(writer.submit(sample()))
            self.assertTrue(started.wait(1))
            self.assertTrue(writer.submit(sample()))
            baseline, _ = replay_steps(False)
            with self.assertLogs("geopolitical_shadow", level="WARNING"):
                enabled, _ = replay_steps(True, submit=writer.submit)
            self.assertEqual(enabled, baseline)
            self.assertFalse(release.is_set())  # Every delivery completed while DB was blocked.
            self.assertGreater(writer.get_persistence_stats()["dropped_queue_full"], 0)
        finally:
            release.set()
            self.assertTrue(writer.shutdown(timeout=5)["stopped"])

    def test_config_opt_in_default_and_invalid_fail_disabled(self):
        for value, expected in ((None, False), ("true", True), ("TRUE", True), ("false", False), ("1", False)):
            env = {} if value is None else {"GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED": value}
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, env, clear=True):
                config = runpy.run_path("shared/config.py")
            self.assertIs(config["GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED"], expected)
            self.assertIs(config["TREASURY_PERSISTENCE_SHADOW_ENABLED"], False)
            self.assertIs(config["MACRO_PERSISTENCE_SHADOW_ENABLED"], False)

    def test_runtime_timeout_caps_and_lazy_engine(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:secret@localhost/mias_test"}), \
             patch.object(macro_shadow, "make_engine") as make:
            shadow.runtime_engine()
        settings = make.call_args.args[0]
        self.assertEqual((settings.pool_size, settings.max_overflow, settings.connect_timeout_seconds,
                          settings.statement_timeout_ms, settings.lock_timeout_ms), (1, 0, 2, 1000, 500))
        self.assertEqual(settings.application_name, "mias_geopolitical_shadow")
        self.assertNotIn("secret", repr(settings))


class GeopoliticalWriterOperationsTests(unittest.TestCase):
    def writer(self, **kwargs):
        writer = shadow.GeopoliticalShadowWriter(engine_factory=FakeEngine, **kwargs)
        self.addCleanup(writer.shutdown)
        return writer

    def assert_balanced(self, stats):
        self.assertEqual(stats["queued"], sum(stats[k] for k in (
            "persisted", "failed", "dropped_shutdown", "queue_depth", "in_flight")))

    def blocked(self):
        entered, release = Event(), Event()
        def write(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"duplicate": False}
        return entered, release, write

    def test_stats_contract_and_labels(self):
        outcomes = [{"duplicate": False, "promotion": "first"}, {"duplicate": True, "promotion": "duplicate"},
                    {"duplicate": False, "promotion": "older"}, PersistenceError("private")]
        with patch.object(shadow, "persist_geopolitical", side_effect=outcomes), \
             patch.object(macro_shadow, "persist_macro", side_effect=AssertionError("macro path")):
            writer = self.writer()
            self.assertEqual(writer.thread.name, "mias-geopolitical-shadow")
            with self.assertLogs("geopolitical_shadow", level="INFO") as logs:
                for _ in range(4): self.assertTrue(writer.submit(sample()))
                result = writer.shutdown()
        stats = result["stats"]
        self.assertEqual(set(stats), set(macro_shadow.empty_stats()))
        self.assertEqual((stats["queued"], stats["persisted"], stats["duplicate"], stats["failed"]), (4, 3, 1, 1))
        self.assertEqual((stats["promotion_held"], stats["promotion_ambiguous"]), (1, 0))
        self.assertEqual((stats["worker_started"], stats["worker_stopped"], stats["queue_depth"], stats["in_flight"]), (1, 1, 0, 0))
        self.assertIsNotNone(stats["last_success_at"])
        self.assertIsNotNone(stats["last_failure_at"])
        self.assertTrue(all("Geopolitical shadow" in line for line in logs.output))
        self.assertNotIn("private", str(logs.output) + str(stats))
        self.assert_balanced(stats)

    def test_queue_full_counted_and_non_blocking(self):
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_geopolitical", side_effect=write):
            writer = self.writer(capacity=1)
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                self.assertTrue(writer.submit(sample()))
                started = monotonic()
                with self.assertLogs("geopolitical_shadow", level="WARNING") as logs:
                    for _ in range(10): self.assertFalse(writer.submit(sample()))
                self.assertLess(monotonic() - started, 1)
                self.assertEqual(len(logs.output), 1)
                stats = writer.get_persistence_stats()
                self.assertEqual((stats["queued"], stats["dropped_queue_full"], stats["queue_depth"], stats["in_flight"]), (2, 10, 1, 1))
                self.assert_balanced(stats)
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])

    def test_graceful_drain(self):
        with patch.object(shadow, "persist_geopolitical", return_value={"duplicate": False}):
            writer = self.writer(capacity=32)
            for _ in range(20): writer.submit(sample())
            result = writer.shutdown(drain=True, timeout=5)
        self.assertEqual((result["stopped"], result["timed_out"], result["unprocessed"]), (True, False, 0))
        self.assertEqual(result["stats"]["persisted"], 20)

    def test_drain_timeout_bounded_and_in_flight_reported(self):
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_geopolitical", side_effect=write):
            writer = self.writer()
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                writer.submit(sample())
                started = monotonic()
                with self.assertLogs("geopolitical_shadow", level="WARNING"):
                    result = writer.shutdown(timeout=0.02)
                self.assertLess(monotonic() - started, 1)
                self.assertEqual((result["stopped"], result["timed_out"], result["unprocessed"]), (False, True, 2))
                self.assertEqual((result["stats"]["dropped_shutdown"], result["stats"]["in_flight"]), (1, 1))
                again = writer.shutdown(timeout=0)
                self.assertEqual(again["stats"]["drain_timeouts"], 1)
                self.assert_balanced(again["stats"])
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])

    def test_immediate_stop_discards_pending(self):
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_geopolitical", side_effect=write):
            writer = self.writer()
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                for _ in range(3): writer.submit(sample())
                started = monotonic()
                result = writer.shutdown(drain=False, timeout=10)
                self.assertLess(monotonic() - started, 1)
                self.assertEqual((result["stats"]["dropped_shutdown"], result["stats"]["in_flight"], result["unprocessed"]), (3, 1, 4))
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])

    def test_idempotent_shutdown_and_restart(self):
        with patch.object(shadow, "persist_geopolitical", return_value={"duplicate": True}):
            writer = self.writer()
            first = writer.shutdown()
            self.assertEqual(writer.shutdown(), first)
            self.assertFalse(writer.submit(sample()))
            restarted = self.writer()
            self.assertTrue(restarted.submit(sample()))
            stats = restarted.shutdown()["stats"]
        self.assertEqual((stats["persisted"], stats["duplicate"], stats["worker_started"]), (1, 1, 1))

    def test_worker_survives_task_and_logging_failure(self):
        with patch.object(shadow, "persist_geopolitical", side_effect=[RuntimeError("private"), ValueError("bad"), {"duplicate": False}]), \
             patch.object(shadow.logger, "warning", side_effect=RuntimeError("log unavailable")):
            writer = self.writer()
            for _ in range(3): writer.submit(sample())
            result = writer.shutdown()
        self.assertEqual((result["stopped"], result["stats"]["failed"], result["stats"]["persisted"]), (True, 2, 1))

    def test_concurrent_producers_and_shutdown_race(self):
        with patch.object(shadow, "persist_geopolitical", return_value={"duplicate": False}):
            writer = self.writer(capacity=256)
            event = sample()
            with ThreadPoolExecutor(max_workers=8) as pool:
                submissions = [pool.submit(writer.submit, event) for _ in range(100)]
                writer.shutdown(drain=False)
                accepted = sum(f.result() for f in submissions)
            result = writer.shutdown()
        self.assertEqual((result["stats"]["queued"], result["stats"]["rejected_shutdown"]), (accepted, 100 - accepted))
        self.assert_balanced(result["stats"])

    def test_module_lifecycle_independent_of_other_writers(self):
        from persistence import treasury_shadow
        fresh = dict(dropped_initializing=0, failed_initializing=0, rejected_shutdown=0, last_failure_at=None)
        with patch.object(shadow, "_writer", None), patch.object(shadow, "_shutdown_requested", False), \
             patch.object(shadow, "_submission_stats", dict(fresh)), \
             patch.object(shadow, "GeopoliticalShadowWriter", side_effect=RuntimeError("secret")):
            others = (macro_shadow.get_persistence_stats(), treasury_shadow.get_persistence_stats())
            self.assertFalse(shadow.submit_geopolitical(sample()))
            self.assertEqual(shadow.get_persistence_stats()["failed_initializing"], 1)
            with shadow._lock:
                self.assertFalse(shadow.submit_geopolitical(sample()))
            self.assertEqual(shadow.get_persistence_stats()["dropped_initializing"], 1)
            self.assertTrue(shadow.shutdown()["stopped"])
            self.assertFalse(shadow.submit_geopolitical(sample()))
            self.assertEqual(shadow.get_persistence_stats()["rejected_shutdown"], 1)
            self.assertEqual((macro_shadow.get_persistence_stats(), treasury_shadow.get_persistence_stats()), others)
