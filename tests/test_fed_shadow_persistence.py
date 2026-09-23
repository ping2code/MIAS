"""Fed shadow must not change collector output, Redis processed/delivered state, AI or delivery."""
from contextlib import ExitStack
import os
import runpy
from threading import Event
import unittest
from unittest.mock import patch

from persistence import fed_shadow as shadow
from persistence import macro_shadow
from tests import fed_readiness_corpus as corpus
from tests import test_shadow_lifecycle as lifecycle
from tests.test_fed_persistence_adapter import sample
from tests.test_fed_pipeline import fed
from tests.test_treasury_shadow_persistence import FakeEngine

FED_MODULE = (shadow, "submit_fed", "FedShadowWriter", "persist_fed", "Fed", "fed_shadow", sample)


def replay(enabled, submit=None, steps=None):
    """Whole corpus step sequence; returns every observable collector output per step."""
    redis, outputs, submitted = corpus.FedMemoryRedis(), [], []
    for label, names, clock, send_alerts, ok, ai in steps or corpus.STEPS:
        deliver = None if ok else (lambda message: {"ok": False})
        result, captured = corpus.run_collector([corpus.ENTRIES[n] for n in names], redis, now=clock, ai=ai,
                                                send_alerts=send_alerts, deliver=deliver, enabled=enabled, submit=submit)
        outputs.append((label, result))
        submitted.extend((label, event, kwargs) for event, kwargs in captured)
    return outputs, submitted


def delivered_keys(result):
    return sorted(k for k, _ in result[2] if k.startswith("mias:fed:delivered:"))


class FedCollectorShadowTests(unittest.TestCase):
    def test_disabled_does_not_initialize_or_submit(self):
        with patch.object(shadow, "runtime_engine", side_effect=AssertionError("DB forbidden")), \
             patch.object(shadow, "FedShadowWriter", side_effect=AssertionError("worker forbidden")):
            outputs, submitted = replay(False)
        self.assertEqual(submitted, [])
        self.assertIsNone(shadow._writer)

    def test_enabled_identical_output_redis_ai_and_delivery(self):
        baseline, _ = replay(False)
        enabled, submitted = replay(True)
        self.assertEqual(enabled, baseline)  # Events, stats, all Redis keys/values/TTLs, Telegram and AI counts.
        processed = {label: event for label, event, kwargs in submitted if kwargs["make_current"]}
        for label, (events, stats, *_rest) in enabled:
            if stats["processed"]:
                self.assertEqual({k: v for k, v in processed[label].items() if k not in ("fed_fingerprint", "fed_source_feed")}, events[0])
        skipped = sorted(label for label, _, kwargs in submitted if not kwargs["make_current"])
        self.assertEqual(skipped, ["missing_date", "stale", "stale_rediscovery"])
        keys = {k for _, result in enabled for k, _ in result[2]}
        self.assertTrue(keys and all(k.startswith(("mias:fed:event:", "mias:fed:processed:", "mias:fed:delivered:")) for k in keys))

    def test_dry_run_does_not_consume_delivery_eligibility(self):
        outputs, submitted = replay(True)
        results = dict(outputs)
        self.assertEqual(results["policy_statement_dry_run"][3], 0)
        self.assertEqual(delivered_keys(results["policy_statement_dry_run"]), [])
        self.assertTrue(any(label == "policy_statement_dry_run" for label, _, _ in submitted))  # Shadowed anyway.
        cached = results["cached_delivery_after_dry_run"]
        self.assertEqual((cached[1]["duplicates"], cached[3], cached[4]), (1, 1, 0))  # Delivered from cache, no AI.

    def test_failed_delivery_remains_retryable_and_cached_retry_skips_ai(self):
        outputs, _ = replay(True)
        results = dict(outputs)
        failure, retry = results["delivery_failure"], results["delivery_retry"]
        self.assertEqual((failure[3], failure[4]), (1, 0))
        action_key = [k for k in delivered_keys(retry) if k not in delivered_keys(failure)]
        self.assertEqual(len(action_key), 1)  # Marker only after the confirmed retry.
        self.assertEqual((retry[3], retry[4]), (1, 0))
        self.assertEqual(results["provenance_duplicate"][3], 0)  # Delivered marker prevents resend.

    def test_persistence_failure_does_not_change_redis_or_delivery(self):
        baseline, _ = replay(False)
        with patch.object(fed, "_shadow_last_failure", float("-inf")), \
             self.assertLogs("fed_collector", level="WARNING") as logs:
            failing, _ = replay(True, submit=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret-password")))
        self.assertEqual(failing, baseline)
        self.assertNotIn("secret-password", str(logs.output))
        self.assertEqual(sum("Fed shadow submission failed" in line for line in logs.output), 1)

    def test_real_writer_success_and_db_unavailable_leave_redis_identical(self):
        baseline, _ = replay(False)
        with patch.object(shadow, "persist_fed", return_value={"duplicate": False}):
            writer = shadow.FedShadowWriter(engine_factory=FakeEngine)
            succeeded, submitted = replay(True, submit=writer.submit)
            stats = writer.shutdown(timeout=5)["stats"]
        self.assertEqual(succeeded, baseline)
        self.assertEqual((stats["persisted"], stats["failed"]), (len(submitted), 0))
        with self.assertLogs("fed_shadow", level="WARNING") as logs:
            down = shadow.FedShadowWriter(engine_factory=lambda: (_ for _ in ()).throw(RuntimeError("password")))
            failed, submitted = replay(True, submit=down.submit)
            stats = down.shutdown(timeout=5)["stats"]
        self.assertEqual(failed, baseline)
        self.assertEqual((stats["failed"], stats["persisted"]), (len(submitted), 0))
        self.assertNotIn("password", str(logs.output))

    def test_slow_db_and_full_queue_do_not_block_delivery(self):
        started, release = Event(), Event()
        def factory():
            started.set()
            release.wait(5)
            raise RuntimeError("unavailable")
        writer = shadow.FedShadowWriter(engine_factory=factory, capacity=1)
        try:
            self.assertTrue(writer.submit(sample()))
            self.assertTrue(started.wait(1))
            self.assertTrue(writer.submit(sample()))
            baseline, _ = replay(False)
            with self.assertLogs("fed_shadow", level="WARNING"):
                enabled, _ = replay(True, submit=writer.submit)
            self.assertEqual(enabled, baseline)
            self.assertFalse(release.is_set())  # All deliveries completed while the DB was blocked.
            self.assertGreater(writer.get_persistence_stats()["dropped_queue_full"], 0)
        finally:
            release.set()
            self.assertTrue(writer.shutdown(timeout=5)["stopped"])

    def test_invalid_cached_json_leaves_collector_outputs_unchanged(self):
        redis = corpus.FedMemoryRedis()
        corpus.run_collector([corpus.ENTRIES["policy_statement"]], redis, enabled=False)
        key = next(k for k in redis.data if k.startswith("mias:fed:processed:"))
        redis.data[key] = ("{not json", redis.data[key][1])
        results = []
        for enabled in (False, True):
            copy = corpus.FedMemoryRedis()
            copy.data = dict(redis.data)
            with patch.object(fed, "_shadow_last_failure", float("-inf")):
                results.append(corpus.run_collector([corpus.ENTRIES["policy_statement"]], copy, enabled=enabled,
                                                    send_alerts=True)[0])
        self.assertEqual(results[0], results[1])

    def test_config_opt_in_default(self):
        for value, expected in ((None, False), ("true", True), ("TRUE", True), ("false", False), ("1", False)):
            env = {} if value is None else {"FED_PERSISTENCE_SHADOW_ENABLED": value}
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, env, clear=True):
                config = runpy.run_path("shared/config.py")
            self.assertIs(config["FED_PERSISTENCE_SHADOW_ENABLED"], expected)
            for other in ("MACRO", "TREASURY", "GEOPOLITICAL"):
                self.assertIs(config[f"{other}_PERSISTENCE_SHADOW_ENABLED"], False)

    def test_runtime_engine_caps(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:secret@localhost/mias_test"}), \
             patch.object(macro_shadow, "make_engine") as make:
            shadow.runtime_engine()
        settings = make.call_args.args[0]
        self.assertEqual((settings.pool_size, settings.max_overflow, settings.connect_timeout_seconds,
                          settings.statement_timeout_ms, settings.lock_timeout_ms, settings.application_name),
                         (1, 0, 2, 1000, 500, "mias_fed_shadow"))
        self.assertNotIn("secret", repr(settings))


class FedLifecycleEquivalenceTests(lifecycle.LifecycleEquivalenceTests):
    """The Phase 2G lifecycle equivalence suite, re-run with the Fed module included."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(lifecycle, "MODULES", lifecycle.MODULES + (FED_MODULE,)))
        self.enterContext(patch.dict(lifecycle.MODULES_BY, {shadow: FED_MODULE}))

    def test_fed_counters_match_macro_for_identical_script(self):
        from persistence.database import PersistenceError
        results = []
        for module, _, writer_name, persist, label, logger, factory in (lifecycle.MODULES[0], FED_MODULE):
            with patch.object(module, persist, side_effect=PersistenceError("private")), self.assertLogs(logger, level="WARNING"):
                writer = getattr(module, writer_name)(engine_factory=FakeEngine, capacity=2)
                for _ in range(2): writer.submit(factory())
                stopped = writer.shutdown(timeout=5)
                self.assertFalse(writer.submit(factory()))
                results.append(lifecycle.scrub(writer.get_persistence_stats()))
        self.assertEqual(results[0], results[1])


class FedWriterOperationsTests(unittest.TestCase):
    def writer(self, **kwargs):
        writer = shadow.FedShadowWriter(engine_factory=FakeEngine, **kwargs)
        self.addCleanup(writer.shutdown)
        return writer

    def blocked(self):
        entered, release = Event(), Event()
        def write(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"duplicate": False}
        return entered, release, write

    def test_stats_contract_labels_and_worker_survives_failures(self):
        from persistence.database import PersistenceError
        outcomes = [{"duplicate": False, "promotion": "first"}, {"duplicate": True, "promotion": "duplicate"},
                    RuntimeError("private"), PersistenceError("private"), {"duplicate": False, "promotion": "older"}]
        with patch.object(shadow, "persist_fed", side_effect=outcomes), \
             patch.object(macro_shadow, "persist_macro", side_effect=AssertionError("macro path")):
            writer = self.writer()
            self.assertEqual(writer.thread.name, "mias-fed-shadow")
            with self.assertLogs("fed_shadow", level="INFO") as logs:
                for _ in range(5): self.assertTrue(writer.submit(sample()))
                result = writer.shutdown()
        stats = result["stats"]
        self.assertEqual(set(stats), set(macro_shadow.empty_stats()))
        self.assertEqual((stats["queued"], stats["persisted"], stats["duplicate"], stats["failed"], stats["promotion_held"]),
                         (5, 3, 1, 2, 1))
        self.assertEqual((stats["worker_started"], stats["worker_stopped"], stats["queue_depth"], stats["in_flight"]), (1, 1, 0, 0))
        self.assertTrue(stats["last_success_at"] and stats["last_failure_at"])
        self.assertTrue(all("Fed shadow" in line for line in logs.output))
        self.assertNotIn("private", str(logs.output) + str(stats))

    def test_queue_full_drain_timeout_immediate_stop_and_restart(self):
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_fed", side_effect=write):
            writer = self.writer(capacity=1)
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                self.assertTrue(writer.submit(sample()))
                with self.assertLogs("fed_shadow", level="WARNING"):
                    self.assertFalse(writer.submit(sample()))
                    result = writer.shutdown(timeout=0.02)
                self.assertEqual((result["timed_out"], result["unprocessed"], result["stats"]["dropped_queue_full"],
                                  result["stats"]["dropped_shutdown"], result["stats"]["in_flight"]), (True, 2, 1, 1, 1))
                self.assertEqual(writer.shutdown(timeout=0)["stats"]["drain_timeouts"], 1)  # Idempotent.
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_fed", side_effect=write):
            writer = self.writer()
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                for _ in range(3): writer.submit(sample())
                result = writer.shutdown(drain=False, timeout=10)
                self.assertEqual((result["stats"]["dropped_shutdown"], result["unprocessed"]), (3, 4))
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])
        with patch.object(shadow, "persist_fed", return_value={"duplicate": True}):
            restarted = self.writer()
            self.assertTrue(restarted.submit(sample()))
            self.assertEqual(restarted.shutdown(drain=True, timeout=5)["stats"]["duplicate"], 1)
