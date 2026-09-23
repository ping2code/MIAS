"""Shadow failure must not change collector output, state calls, or delivery."""
from copy import deepcopy
from datetime import datetime, timedelta
import os
import runpy
from threading import Event
import unittest
from unittest.mock import patch

from tests import test_macro_pipeline as fixtures
from tests.test_macro_persistence_adapter import sample
from persistence import macro_shadow as shadow

macro = fixtures.macro


def collect(enabled, event=None, submit=None, ai=False, duplicate=False):
    event = deepcopy(event or sample("retail_sales", scored=False))
    now = datetime.fromisoformat(sample("retail_sales")["published_at"]) + timedelta(hours=1)
    redis = fixtures.MemoryRedis(lambda: now)
    submitted = []
    def capture(value, **kwargs):
        submitted.append((deepcopy(value), kwargs))
        if submit:
            return submit(value, **kwargs)
    with patch.object(macro, "MACRO_PERSISTENCE_SHADOW_ENABLED", enabled), \
         patch.object(macro, "MACRO_SOURCES", (fixtures.SOURCES["retail_sales"],)), \
         patch.object(macro, "fetch_document", return_value="fixture"), \
         patch.object(macro, "normalize_macro_release", return_value=event), \
         patch.object(macro.deduplicator, "redis_client", redis), \
         patch.object(macro, "datetime", wraps=datetime) as clock, \
         patch.object(shadow, "submit_macro", side_effect=capture), \
         patch.object(macro, "analyze_macro_event", return_value=dict(ai_summary="Visible", ai_sentiment="NEUTRAL",
             ai_confidence=80, ai_why_it_matters="Context", ai_event_type="macro")) as analyze, \
         patch.object(macro, "deliver_macro_alert", return_value={"ok": True}) as send:
        clock.now.return_value = now
        result = macro.collect_macro_events(enable_ai=ai, send_alerts=True)
        if duplicate:
            result = macro.collect_macro_events(enable_ai=ai, send_alerts=True)
        return (result, redis.calls, redis.store, send.call_args_list, analyze.call_count), submitted


class ShadowTests(unittest.TestCase):
    def test_disabled_does_not_initialize_or_submit(self):
        with patch.object(shadow, "runtime_engine", side_effect=AssertionError("DB forbidden")), \
             patch.object(shadow, "ShadowWriter", side_effect=AssertionError("worker forbidden")):
            baseline, submitted = collect(False)
        self.assertEqual(submitted, [])
        self.assertEqual(baseline[0][1]["delivered"], 1)

    def test_enabled_identical_output_redis_and_delivery(self):
        baseline, _ = collect(False)
        enabled, submitted = collect(True)
        self.assertEqual(enabled, baseline)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0][0], enabled[0][0][0])

    def test_submit_exception_has_no_effect_or_secret_logs(self):
        baseline, _ = collect(False)
        with patch.object(macro, "_shadow_last_failure", float("-inf")), self.assertLogs("macro_collector", level="WARNING") as logs:
            enabled, _ = collect(True, submit=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret-password")))
        self.assertEqual(enabled, baseline)
        self.assertNotIn("secret-password", str(logs.output))

    def test_duplicate_captures_cached_result_without_rescoring(self):
        baseline, _ = collect(False, duplicate=True)
        enabled, submitted = collect(True, duplicate=True)
        self.assertEqual(enabled, baseline)
        self.assertEqual(len(submitted), 2)
        self.assertEqual(submitted[0][0], submitted[1][0])

    def test_ai_is_existing_validated_result_only(self):
        baseline, _ = collect(False, ai=True)
        enabled, submitted = collect(True, ai=True)
        self.assertEqual(enabled, baseline)
        self.assertEqual(enabled[-1], 1)
        self.assertEqual(submitted[0][0]["ai_summary"], "Visible")
        self.assertEqual(submitted[0][0]["impact_score"], 80)

    def test_stale_and_missing_date_unchanged_unscored(self):
        for missing in (False, True):
            event = sample("retail_sales", scored=False)
            event["published_at"] = None if missing else "2000-01-01T12:00:00+00:00"
            if missing:
                event["timestamp_precision"] = "unknown"
            baseline, _ = collect(False, event)
            enabled, submitted = collect(True, event)
            self.assertEqual(enabled, baseline)
            self.assertEqual(enabled[0][0], [])
            self.assertEqual(enabled[0][1]["missing_date" if missing else "stale"], 1)
            self.assertEqual(enabled[1], [])  # No Redis calls.
            self.assertEqual(len(submitted), 1)
            self.assertNotIn("impact_score", submitted[0][0])
            self.assertFalse(submitted[0][1]["make_current"])

    def test_db_unavailable_worker_does_not_affect_collector(self):
        with self.assertLogs("macro_shadow", level="WARNING") as logs:
            writer = shadow.ShadowWriter(engine_factory=lambda: (_ for _ in ()).throw(RuntimeError("password")))
            baseline, _ = collect(False)
            enabled, _ = collect(True, submit=writer.submit)
            self.assertTrue(writer.close())
        self.assertEqual(enabled, baseline)
        self.assertNotIn("password", str(logs.output))

    def test_slow_db_and_full_queue_do_not_block_delivery(self):
        started, release = Event(), Event()
        def factory():
            started.set()
            release.wait(5)
            raise RuntimeError("unavailable")
        writer = shadow.ShadowWriter(engine_factory=factory, capacity=1)
        try:
            self.assertTrue(writer.submit(sample()))
            self.assertTrue(started.wait(1))
            self.assertTrue(writer.submit(sample()))
            baseline, _ = collect(False)
            with self.assertLogs("macro_shadow", level="WARNING"):
                enabled, _ = collect(True, submit=writer.submit)
            self.assertEqual(enabled, baseline)
            self.assertTrue(writer.thread.is_alive())
            self.assertFalse(release.is_set())  # Delivery completed while DB was still blocked.
        finally:
            release.set()
            self.assertTrue(writer.close())

    def test_worker_snapshot_is_independent_and_failure_is_bounded(self):
        captured = []
        class Engine:
            disposed = False
            def dispose(self): self.disposed = True
        engine = Engine()
        with patch.object(shadow, "persist_macro", side_effect=lambda e, event, *a, **k: captured.append(event)):
            writer = shadow.ShadowWriter(engine_factory=lambda: engine)
            event = sample()
            writer.submit(event)
            event["headline"] = "changed after submission"
            self.assertTrue(writer.close())
        self.assertTrue(engine.disposed)
        self.assertNotEqual(captured[0]["headline"], event["headline"])
        with self.assertLogs("macro_shadow", level="WARNING") as logs:
            for _ in range(100): writer.failure()
        self.assertEqual(len(logs.output), 1)

    def test_config_opt_in_default_and_invalid_fail_disabled(self):
        for value, expected in ((None, False), ("true", True), ("false", False), ("typo", False)):
            env = {} if value is None else {"MACRO_PERSISTENCE_SHADOW_ENABLED": value}
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, env, clear=True):
                self.assertIs(runpy.run_path("shared/config.py")["MACRO_PERSISTENCE_SHADOW_ENABLED"], expected)

    def test_runtime_timeout_caps_and_lazy_engine(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:secret@localhost/mias_test"}), \
             patch.object(shadow, "make_engine") as make:
            shadow.runtime_engine()
        settings = make.call_args.args[0]
        self.assertEqual(settings.pool_size, 1)
        self.assertEqual(settings.connect_timeout_seconds, 2)
        self.assertEqual(settings.statement_timeout_ms, 1000)
        self.assertEqual(settings.lock_timeout_ms, 500)
        self.assertNotIn("secret", repr(settings))
