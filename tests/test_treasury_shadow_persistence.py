"""Treasury shadow must not change collector output, Redis, AI or delivery; writer lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timezone
import os
import runpy
from threading import Event
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from tests.test_treasury_pipeline import (
    FIXTURES, RELEASE_URL, MemoryRedis, treasury, sources, auction, yield_rows, normalize_yield_events,
)
from tests.test_treasury_persistence_adapter import sample
from persistence import macro_shadow
from persistence import treasury_shadow as shadow
from persistence.database import PersistenceError

NOW = datetime(2026, 9, 21, 14, tzinfo=timezone.utc)


def enrich(event):
    return dict(event, ai_summary="Official financing policy update", ai_sentiment="NEUTRAL", ai_confidence=85,
                ai_why_it_matters="Relevant to rates", ai_event_type="official policy")


def collect(enabled, *, submit=None, ai=True, runs=1, now=NOW, html=None, auctions=(), yields=(), release=True):
    """Run the real collector with isolated HTTP/Redis/AI/Telegram and capture submissions."""
    redis = MemoryRedis(lambda: now)
    submitted = []
    def capture(value, **kwargs):
        submitted.append((deepcopy(value), kwargs))
        if submit:
            return submit(value, **kwargs)
    data = html if html is not None else (FIXTURES / "refunding.html").read_bytes()
    with patch("requests.sessions.Session.request", side_effect=AssertionError("Live HTTP forbidden")), \
         patch.object(treasury, "TREASURY_PERSISTENCE_SHADOW_ENABLED", enabled), \
         patch.object(treasury, "datetime", wraps=datetime) as clock, \
         patch.object(treasury.deduplicator, "redis_client", redis), \
         patch.object(sources, "fetch_index_documents", return_value=[{"url": RELEASE_URL, "kind": "release"}] if release else []), \
         patch.object(sources, "fetch", return_value=data), \
         patch.object(sources, "fetch_auctions", return_value=[deepcopy(r) for r in auctions]), \
         patch.object(sources, "fetch_yields", return_value=deepcopy(list(yields))), \
         patch.object(shadow, "submit_treasury", side_effect=capture), \
         patch.object(treasury, "analyze_treasury_event", side_effect=enrich) as analyze, \
         patch.object(treasury, "deliver_treasury_alert", return_value={"ok": True}) as send:
        clock.now.side_effect = lambda tz=None: now.astimezone(tz) if tz else now
        results = [treasury.collect_treasury_events(enable_ai=ai, send_alerts=True) for _ in range(runs)]
        return (results, redis.calls, redis.store, send.call_args_list, analyze.call_count), submitted


def mixed():
    return dict(auctions=[auction()], yields=normalize_yield_events(yield_rows(), date(2026, 9, 22)))


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


class TreasuryCollectorShadowTests(unittest.TestCase):
    def test_disabled_does_not_initialize_or_submit(self):
        with patch.object(shadow, "runtime_engine", side_effect=AssertionError("DB forbidden")), \
             patch.object(shadow, "TreasuryShadowWriter", side_effect=AssertionError("worker forbidden")):
            baseline, submitted = collect(False, **mixed())
        self.assertEqual(submitted, [])
        self.assertEqual(baseline[0][0][1]["delivered"], 1)
        self.assertEqual(shadow._writer, None)

    def test_enabled_identical_output_redis_ai_and_delivery(self):
        baseline, _ = collect(False, **mixed())
        enabled, submitted = collect(True, **mixed())
        self.assertEqual(enabled, baseline)
        events, stats = enabled[0][0]
        processed = [value for value, kwargs in submitted if kwargs["make_current"]]
        self.assertEqual(processed, events)  # Exactly the collector's own already-computed results.
        skipped = [value for value, kwargs in submitted if not kwargs["make_current"]]
        self.assertEqual(len(skipped), stats["stale"] + stats["future"] + stats["missing_date"])
        self.assertTrue(all("impact_score" not in value for value in skipped))
        self.assertTrue(all(k.startswith("mias:treasury:") for _, k in enabled[1]))

    def test_submit_exception_has_no_effect_or_secret_logs(self):
        baseline, _ = collect(False, **mixed())
        with patch.object(treasury, "_shadow_last_failure", float("-inf")), \
             self.assertLogs("treasury_collector", level="WARNING") as logs:
            enabled, _ = collect(True, submit=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret-password")), **mixed())
        self.assertEqual(enabled, baseline)
        self.assertNotIn("secret-password", str(logs.output))
        self.assertEqual(sum("Treasury shadow submission failed" in line for line in logs.output), 1)

    def test_duplicate_submits_cached_result_without_rescoring_or_ai(self):
        baseline, _ = collect(False, runs=2)
        enabled, submitted = collect(True, runs=2)
        self.assertEqual(enabled, baseline)
        self.assertEqual(enabled[4], 1)  # AI called once across both runs.
        self.assertEqual(enabled[0][1][1]["duplicates"], 1)
        self.assertEqual(len(submitted), 2)
        self.assertEqual(submitted[0][0], submitted[1][0])

    def test_ai_present_and_absent(self):
        enabled, submitted = collect(True)
        self.assertEqual(submitted[0][0]["ai_summary"], "Official financing policy update")
        self.assertEqual((submitted[0][0]["impact_score"], submitted[0][0]["quality_adjustment"]), (85, 0))
        baseline, _ = collect(False, ai=False)
        without, submitted = collect(True, ai=False)
        self.assertEqual(without, baseline)
        self.assertFalse(any(key.startswith("ai_") for key in submitted[0][0]))

    def test_stale_and_missing_date_unchanged_unscored_and_not_current(self):
        html = (FIXTURES / "refunding.html").read_bytes()
        for now, data, reason in ((datetime(2026, 9, 30, tzinfo=timezone.utc), html, "stale"),
                                  (NOW, html.replace(b'datetime="2026-09-21T08:30:00-04:00"', b""), "missing_date")):
            baseline, _ = collect(False, now=now, html=data)
            enabled, submitted = collect(True, now=now, html=data)
            self.assertEqual(enabled, baseline)
            self.assertEqual(enabled[0][0][1][reason], 1)
            self.assertEqual(enabled[1], [])  # No Redis calls.
            self.assertEqual(len(submitted), 1)
            self.assertNotIn("impact_score", submitted[0][0])
            self.assertFalse(submitted[0][1]["make_current"])

    def test_yield_threshold_is_retained_not_alerted(self):
        yields = normalize_yield_events(yield_rows(), date(2026, 9, 22))
        baseline, _ = collect(False, release=False, yields=yields)
        enabled, submitted = collect(True, release=False, yields=yields)
        self.assertEqual(enabled, baseline)
        self.assertEqual(enabled[3], [])  # No delivery for unverified observation publication.
        processed = [value for value, kwargs in submitted if kwargs["make_current"]]
        self.assertEqual([(e["impact_score"], e["initial_decision"], e["alert_decision"], e["alert_eligible"])
                          for e in processed], [(70, "ALERT", "DISPLAY_ONLY", False)])

    def test_auction_announcement_and_result_submitted_separately(self):
        enabled, submitted = collect(True, release=False, auctions=[auction()])
        stages = sorted(value["release_stage"] for value, _ in submitted)
        self.assertEqual(stages, ["announcement", "result"])
        self.assertEqual(len({value["event_id"] for value, _ in submitted}), 2)

    def test_db_unavailable_worker_does_not_affect_collector(self):
        with self.assertLogs("treasury_shadow", level="WARNING") as logs:
            writer = shadow.TreasuryShadowWriter(engine_factory=lambda: (_ for _ in ()).throw(RuntimeError("password")))
            baseline, _ = collect(False, **mixed())
            enabled, _ = collect(True, submit=writer.submit, **mixed())
            result = writer.shutdown(timeout=5)
        self.assertEqual(enabled, baseline)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["stats"]["failed"], result["stats"]["queued"])
        self.assertNotIn("password", str(logs.output))

    def test_slow_db_and_full_queue_do_not_block_delivery(self):
        started, release = Event(), Event()
        def factory():
            started.set()
            release.wait(5)
            raise RuntimeError("unavailable")
        writer = shadow.TreasuryShadowWriter(engine_factory=factory, capacity=1)
        try:
            self.assertTrue(writer.submit(sample()))
            self.assertTrue(started.wait(1))
            self.assertTrue(writer.submit(sample()))
            baseline, _ = collect(False, **mixed())
            with self.assertLogs("treasury_shadow", level="WARNING"):
                enabled, _ = collect(True, submit=writer.submit, **mixed())
            self.assertEqual(enabled, baseline)
            self.assertEqual(enabled[0][0][1]["delivered"], 1)
            self.assertFalse(release.is_set())  # Delivery completed while DB was still blocked.
            self.assertGreater(writer.get_persistence_stats()["dropped_queue_full"], 0)
        finally:
            release.set()
            self.assertTrue(writer.shutdown(timeout=5)["stopped"])

    def test_config_opt_in_default_and_invalid_fail_disabled(self):
        for value, expected in ((None, False), ("true", True), ("TRUE", True), ("false", False), ("typo", False)):
            env = {} if value is None else {"TREASURY_PERSISTENCE_SHADOW_ENABLED": value}
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, env, clear=True):
                config = runpy.run_path("shared/config.py")
            self.assertIs(config["TREASURY_PERSISTENCE_SHADOW_ENABLED"], expected)
            self.assertIs(config["MACRO_PERSISTENCE_SHADOW_ENABLED"], False)  # Independent switches.

    def test_runtime_timeout_caps_and_lazy_engine(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:secret@localhost/mias_test"}), \
             patch.object(macro_shadow, "make_engine") as make:
            shadow.runtime_engine()
        settings = make.call_args.args[0]
        self.assertEqual((settings.pool_size, settings.max_overflow), (1, 0))
        self.assertEqual((settings.connect_timeout_seconds, settings.statement_timeout_ms, settings.lock_timeout_ms), (2, 1000, 500))
        self.assertEqual(settings.application_name, "mias_treasury_shadow")
        self.assertNotIn("secret", repr(settings))
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "DB_ALLOW_SQLITE": "true"}), self.assertRaises(ValueError):
            shadow.runtime_engine()


class TreasuryWriterOperationsTests(unittest.TestCase):
    def writer(self, **kwargs):
        writer = shadow.TreasuryShadowWriter(engine_factory=FakeEngine, **kwargs)
        self.addCleanup(writer.shutdown)
        return writer

    def assert_balanced(self, stats):
        self.assertEqual(stats["queued"], sum(stats[k] for k in (
            "persisted", "failed", "dropped_shutdown", "queue_depth", "in_flight")))

    def test_stats_contract_and_labels(self):
        outcomes = [{"duplicate": False, "promotion": "first"}, {"duplicate": True, "promotion": "duplicate"},
                    {"duplicate": False, "promotion": "ambiguous"}, PersistenceError("private")]
        with patch.object(shadow, "persist_treasury", side_effect=outcomes), \
             patch.object(macro_shadow, "persist_macro", side_effect=AssertionError("macro path")):
            writer = self.writer()
            self.assertEqual(writer.thread.name, "mias-treasury-shadow")
            with self.assertLogs("treasury_shadow", level="INFO") as logs:
                for _ in range(4): self.assertTrue(writer.submit(sample()))
                result = writer.shutdown()
        stats = result["stats"]
        self.assertEqual(set(macro_shadow.empty_stats()), set(stats))
        self.assertEqual((stats["queued"], stats["persisted"], stats["duplicate"], stats["failed"]), (4, 3, 1, 1))
        self.assertEqual((stats["promotion_held"], stats["promotion_ambiguous"]), (1, 1))
        self.assertEqual((stats["worker_started"], stats["worker_stopped"], stats["queue_depth"], stats["in_flight"]), (1, 1, 0, 0))
        self.assertIsNotNone(stats["last_success_at"])
        self.assertIsNotNone(stats["last_failure_at"])
        self.assertTrue(all("Treasury shadow" in line for line in logs.output))
        self.assertNotIn("private", str(logs.output) + str(stats))
        self.assert_balanced(stats)

    def test_queue_full_is_counted_and_non_blocking(self):
        entered, release = Event(), Event()
        def write(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"duplicate": False}
        with patch.object(shadow, "persist_treasury", side_effect=write):
            writer = self.writer(capacity=1)
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                self.assertTrue(writer.submit(sample()))
                started = monotonic()
                with self.assertLogs("treasury_shadow", level="WARNING") as logs:
                    for _ in range(10): self.assertFalse(writer.submit(sample()))
                self.assertLess(monotonic() - started, 1)
                self.assertEqual(len(logs.output), 1)
                self.assertIn("queue full", logs.output[0])
                stats = writer.get_persistence_stats()
                self.assertEqual((stats["queued"], stats["dropped_queue_full"], stats["queue_depth"], stats["in_flight"]), (2, 10, 1, 1))
                self.assert_balanced(stats)
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])

    def test_graceful_drain_commits_all_pending(self):
        with patch.object(shadow, "persist_treasury", return_value={"duplicate": False}):
            writer = self.writer(capacity=32)
            for _ in range(20): writer.submit(sample())
            result = writer.shutdown(drain=True, timeout=5)
        self.assertTrue(result["stopped"])
        self.assertFalse(result["timed_out"])
        self.assertEqual((result["unprocessed"], result["stats"]["persisted"]), (0, 20))
        self.assert_balanced(result["stats"])

    def test_drain_timeout_bounded_counts_pending_and_in_flight(self):
        entered, release = Event(), Event()
        def write(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"duplicate": False}
        with patch.object(shadow, "persist_treasury", side_effect=write):
            writer = self.writer()
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                writer.submit(sample())
                started = monotonic()
                with self.assertLogs("treasury_shadow", level="WARNING"):
                    result = writer.shutdown(timeout=0.02)
                self.assertLess(monotonic() - started, 1)
                self.assertEqual((result["stopped"], result["timed_out"], result["unprocessed"]), (False, True, 2))
                self.assertEqual((result["stats"]["dropped_shutdown"], result["stats"]["in_flight"]), (1, 1))
                self.assertFalse(writer.submit(sample()))
                again = writer.shutdown(timeout=0)
                self.assertEqual((again["stats"]["drain_timeouts"], again["stats"]["dropped_shutdown"]), (1, 1))
                self.assert_balanced(again["stats"])
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])
            self.assertEqual(writer.get_persistence_stats()["persisted"], 1)

    def test_forced_stop_discards_pending_without_waiting(self):
        entered, release = Event(), Event()
        def write(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"duplicate": False}
        with patch.object(shadow, "persist_treasury", side_effect=write):
            writer = self.writer()
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                for _ in range(3): writer.submit(sample())
                started = monotonic()
                result = writer.shutdown(drain=False, timeout=10)
                self.assertLess(monotonic() - started, 1)
                self.assertEqual((result["stats"]["dropped_shutdown"], result["stats"]["in_flight"]), (3, 1))
                self.assertEqual(result["unprocessed"], 4)
                self.assertFalse(result["timed_out"])
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])

    def test_shutdown_idempotent_and_restart_with_new_writer(self):
        with patch.object(shadow, "persist_treasury", return_value={"duplicate": True}):
            writer = self.writer()
            first = writer.shutdown()
            self.assertTrue(first["stopped"])
            self.assertEqual(writer.shutdown(), first)
            self.assertFalse(writer.submit(sample()))
            restarted = self.writer()
            self.assertTrue(restarted.submit(sample()))
            stats = restarted.shutdown()["stats"]
        self.assertEqual((stats["persisted"], stats["duplicate"], stats["worker_started"]), (1, 1, 1))

    def test_worker_survives_task_and_logging_failure(self):
        with patch.object(shadow, "persist_treasury", side_effect=[RuntimeError("private"), ValueError("bad"), {"duplicate": False}]), \
             patch.object(shadow.logger, "warning", side_effect=RuntimeError("log unavailable")):
            writer = self.writer()
            for _ in range(3): writer.submit(sample())
            result = writer.shutdown()
        self.assertTrue(result["stopped"])
        self.assertEqual((result["stats"]["failed"], result["stats"]["persisted"]), (2, 1))

    def test_concurrent_producers_and_shutdown_race(self):
        with patch.object(shadow, "persist_treasury", return_value={"duplicate": False}):
            writer = self.writer(capacity=256)
            event = sample()
            with ThreadPoolExecutor(max_workers=8) as pool:
                submissions = [pool.submit(writer.submit, event) for _ in range(100)]
                writer.shutdown(drain=False)
                accepted = sum(f.result() for f in submissions)
            result = writer.shutdown()
        self.assertEqual(result["stats"]["queued"], accepted)
        self.assertEqual(result["stats"]["rejected_shutdown"], 100 - accepted)
        self.assert_balanced(result["stats"])

    def test_invalid_limits_and_snapshot(self):
        for capacity in (0, 5000, True):
            with self.assertRaises(ValueError): shadow.TreasuryShadowWriter(capacity=capacity)
        writer = self.writer()
        for timeout in (None, float("inf"), -1, 31):
            with self.assertRaises(ValueError): writer.shutdown(timeout=timeout)
        self.assertFalse(writer.submit({"invalid": float("nan")}))
        self.assertEqual(writer.get_persistence_stats()["dropped_invalid"], 1)

    def test_module_lifecycle_is_independent_of_macro(self):
        fresh = dict(dropped_initializing=0, failed_initializing=0, rejected_shutdown=0, last_failure_at=None)
        with patch.object(shadow, "_writer", None), patch.object(shadow, "_shutdown_requested", False), \
             patch.object(shadow, "_submission_stats", dict(fresh)), \
             patch.object(macro_shadow, "ShadowWriter", side_effect=AssertionError("macro writer")), \
             patch.object(shadow, "TreasuryShadowWriter", side_effect=RuntimeError("secret")):
            macro_before = macro_shadow.get_persistence_stats()
            self.assertFalse(shadow.submit_treasury(sample()))
            self.assertEqual(shadow.get_persistence_stats()["failed_initializing"], 1)
            with shadow._lock:
                self.assertFalse(shadow.submit_treasury(sample()))
            self.assertEqual(shadow.get_persistence_stats()["dropped_initializing"], 1)
            self.assertTrue(shadow.shutdown()["stopped"])
            self.assertFalse(shadow.submit_treasury(sample()))
            stats = shadow.get_persistence_stats()
            self.assertEqual((stats["rejected_shutdown"], stats["worker_started"]), (1, 0))
            self.assertEqual(macro_shadow.get_persistence_stats(), macro_before)

    def test_module_submit_uses_lazy_singleton(self):
        created = []
        class Writer:
            def __init__(self):
                created.append(self)
            def submit(self, event, make_current=True):
                return make_current
        with patch.object(shadow, "_writer", None), patch.object(shadow, "_shutdown_requested", False), \
             patch.object(shadow, "TreasuryShadowWriter", Writer):
            self.assertEqual(created, [])
            self.assertTrue(shadow.submit_treasury(sample()))
            self.assertFalse(shadow.submit_treasury(sample(), make_current=False))
        self.assertEqual(len(created), 1)
