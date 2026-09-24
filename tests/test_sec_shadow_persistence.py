"""SEC shadow must not change collector output, Redis dedup, Telegram, logging or the configured header."""
import json
import os
import runpy
from threading import Event
import unittest
from unittest.mock import Mock, patch

import requests

from persistence import sec_shadow as shadow
from persistence import macro_shadow
from tests import sec_readiness_corpus as corpus
from tests import test_shadow_lifecycle as lifecycle
from tests.test_sec_persistence_adapter import sample
from tests.test_sec_pipeline import sec, submissions
from tests.test_treasury_shadow_persistence import FakeEngine

SEC_MODULE = (shadow, "submit_sec", "SecShadowWriter", "persist_sec", "SEC", "sec_shadow", sample)


def replay(enabled, submit=None, steps=None):
    """Whole corpus step sequence; returns every observable collector output per step."""
    redis, outputs, submitted = corpus.SecMemoryRedis(), [], []
    for label, names, clock, mode, telegram_ok in steps or corpus.STEPS:
        if mode == "fresh":
            redis = corpus.SecMemoryRedis()
        target = corpus.FailingRedis() if mode == "failing" else redis
        result, captured = corpus.run_collector([corpus.FILINGS[n] for n in names], target, now=clock,
                                                telegram_ok=telegram_ok, enabled=enabled, submit=submit)
        outputs.append((label, result))
        submitted.extend((label, event, kwargs) for event, kwargs in captured)
    return outputs, submitted


def contact():
    return sec.SEC_HEADERS["User-Agent"]


class SecCollectorShadowTests(unittest.TestCase):
    def test_disabled_does_not_initialize_or_submit(self):
        with patch.object(shadow, "runtime_engine", side_effect=AssertionError("DB forbidden")), \
             patch.object(shadow, "SecShadowWriter", side_effect=AssertionError("worker forbidden")):
            outputs, submitted = replay(False)
        self.assertEqual(submitted, [])
        self.assertIsNone(shadow._writer)
        self.assertEqual(sum(len(o["telegram"]) for _, o in outputs), 11)

    def test_enabled_identical_outputs_redis_and_telegram(self):
        baseline, _ = replay(False)
        enabled, submitted = replay(True)
        self.assertEqual(enabled, baseline)
        self.assertEqual(len(submitted), corpus.load_corpus()[0]["expected_observations"])
        for (_, result) in enabled:
            for event in result["events"]:
                self.assertNotIn("sec_fingerprint", event)  # The shadow copy never leaks into outputs.
        keys = {key for _, result in enabled for key, _ in result["redis"]}
        self.assertTrue(keys and all(key.startswith("mias:sec:event:") for key in keys))

    def test_hook_runs_after_decision_and_telegram(self):
        order = []
        def telegram(message):
            order.append("telegram")
            return {"result": {"message_id": 1}}
        def submit(event, **kwargs):
            order.append(("shadow", event["alert_decision"]))
        filings = [corpus.FILINGS["meta_8k"], corpus.FILINGS["meta_8k"], corpus.FILINGS["meta_npx"]]
        with patch.object(corpus.deduplicator, "redis_client", corpus.SecMemoryRedis()), \
             patch.object(sec, "SEC_PERSISTENCE_SHADOW_ENABLED", True), \
             patch.object(shadow, "submit_sec", side_effect=submit), \
             patch.object(sec, "send_telegram_alert", side_effect=telegram), \
             patch("sys.stdout"), self.assertLogs("sec_collector", "INFO"):
            sec.process_sec_filings(filings)
        # Redis-skipped duplicates are never scored, decided or shadowed.
        self.assertEqual(order, ["telegram", ("shadow", "ALERT"), ("shadow", "IGNORE")])

    def test_submission_failure_does_not_change_outputs(self):
        baseline, _ = replay(False)
        with patch.object(sec, "_shadow_last_failure", float("-inf")), \
             self.assertLogs("sec_collector", level="WARNING") as logs:
            failed, submitted = replay(True, submit=Mock(side_effect=RuntimeError("password=secret")))
        self.assertEqual(failed, baseline)
        self.assertEqual(logs.output, ["WARNING:sec_collector:SEC shadow submission failed"])  # Rate limited.
        self.assertNotIn("secret", str(logs.output))

    def test_real_writer_success_and_db_unavailable_leave_outputs_identical(self):
        baseline, _ = replay(False)
        with patch.object(shadow, "persist_sec", return_value={"duplicate": False}):
            writer = shadow.SecShadowWriter(engine_factory=FakeEngine)
            succeeded, submitted = replay(True, submit=writer.submit)
            stats = writer.shutdown(timeout=5)["stats"]
        self.assertEqual(succeeded, baseline)
        self.assertEqual((stats["persisted"], stats["failed"]), (len(submitted), 0))
        with self.assertLogs("sec_shadow", level="WARNING") as logs:
            down = shadow.SecShadowWriter(engine_factory=lambda: (_ for _ in ()).throw(RuntimeError("password")))
            failed, submitted = replay(True, submit=down.submit)
            stats = down.shutdown(timeout=5)["stats"]
        self.assertEqual(failed, baseline)
        self.assertEqual((stats["failed"], stats["persisted"]), (len(submitted), 0))
        self.assertNotIn("password", str(logs.output))

    def test_slow_db_and_full_queue_do_not_block_telegram(self):
        started, release = Event(), Event()
        def factory():
            started.set()
            release.wait(5)
            raise RuntimeError("unavailable")
        writer = shadow.SecShadowWriter(engine_factory=factory, capacity=1)
        try:
            self.assertTrue(writer.submit(sample()))
            self.assertTrue(started.wait(1))
            self.assertTrue(writer.submit(sample()))
            baseline, _ = replay(False)
            with self.assertLogs("sec_shadow", level="WARNING"):
                enabled, _ = replay(True, submit=writer.submit)
            self.assertEqual(enabled, baseline)
            self.assertFalse(release.is_set())  # Every Telegram send completed while the DB was blocked.
            self.assertGreater(writer.get_persistence_stats()["dropped_queue_full"], 0)
        finally:
            release.set()
            self.assertTrue(writer.shutdown(timeout=5)["stopped"])

    def test_config_opt_in_default(self):
        for value, expected in ((None, False), ("true", True), ("TRUE", True), ("false", False), ("1", False)):
            env = {} if value is None else {"SEC_PERSISTENCE_SHADOW_ENABLED": value}
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, env, clear=True):
                config = runpy.run_path("shared/config.py")
            self.assertIs(config["SEC_PERSISTENCE_SHADOW_ENABLED"], expected)
            for other in ("MACRO", "TREASURY", "GEOPOLITICAL", "FED"):
                self.assertIs(config[f"{other}_PERSISTENCE_SHADOW_ENABLED"], False)
        self.assertIs(sec.SEC_PERSISTENCE_SHADOW_ENABLED, False)

    def test_runtime_engine_caps(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:secret@localhost/mias_test"}), \
             patch.object(macro_shadow, "make_engine") as make:
            shadow.runtime_engine()
        settings = make.call_args.args[0]
        self.assertEqual((settings.pool_size, settings.max_overflow, settings.connect_timeout_seconds,
                          settings.statement_timeout_ms, settings.lock_timeout_ms, settings.application_name),
                         (1, 0, 2, 1000, 500, "mias_sec_shadow"))
        self.assertNotIn("secret", repr(settings))


class SecSensitiveHeaderTests(unittest.TestCase):
    """The configured User-Agent contact is used for requests but never surfaces anywhere else."""

    def exposed(self, text):
        return contact() in text or "@" in text

    def test_header_unchanged_and_used_only_for_requests(self):
        response = Mock()
        response.json.return_value = submissions(corpus.FILINGS["meta_8k"])
        with patch.object(sec.requests, "get", return_value=response) as get, self.assertLogs("sec_collector", "INFO") as logs:
            filings = sec.collect_sec_filings()
        self.assertTrue(all(c.kwargs["headers"] is sec.SEC_HEADERS for c in get.call_args_list), "header object changed")
        self.assertEqual(set(sec.SEC_HEADERS), {"User-Agent"})
        self.assertFalse(self.exposed(json.dumps(filings) + str(logs.output)), "contact exposed")

    def test_http_error_logs_do_not_echo_headers(self):
        failure = requests.Response()
        failure.status_code, failure.url, failure.reason = 403, "https://data.sec.gov/submissions/CIK0001326801.json", "Forbidden"
        failure.request = requests.Request("GET", failure.url, headers=sec.SEC_HEADERS).prepare()
        with patch.object(sec.requests, "get", return_value=failure), self.assertLogs("sec_collector", "INFO") as logs:
            self.assertEqual(sec.collect_sec_filings(), [])
        self.assertIn("403 Client Error", str(logs.output))
        self.assertFalse(self.exposed(str(logs.output)), "contact exposed in request error")

    def test_shadow_snapshots_rows_and_logs_never_contain_contact(self):
        outputs, submitted = replay(True)
        self.assertFalse(self.exposed(json.dumps([e for _, e, _ in submitted])), "contact exposed in snapshot")
        self.assertFalse(self.exposed(str(outputs)), "contact exposed in collector outputs")
        from alembic import command
        import sqlalchemy as sa
        from persistence.config import DatabaseSettings
        from persistence.database import make_engine
        from persistence.models import events, event_versions, event_provenance, event_history
        from tests.test_persistence import migration_config
        from tests.test_sec_persistence_adapter import replay as persist_replay
        engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        try:
            with engine.begin() as connection:
                command.upgrade(migration_config(connection), "head")
            result = persist_replay(engine)
            with engine.connect() as connection:
                dump = json.dumps([[dict(r) for r in connection.execute(sa.select(t)).mappings()]
                                   for t in (events, event_versions, event_provenance, event_history)], default=str)
        finally:
            engine.dispose()
        self.assertEqual(result["mismatches"], [])
        self.assertFalse(self.exposed(dump), "contact exposed in persisted rows")
        with patch.object(shadow, "persist_sec", side_effect=RuntimeError("boom")), \
             self.assertLogs("sec_shadow", level="INFO") as logs:
            writer = shadow.SecShadowWriter(engine_factory=FakeEngine)
            writer.submit(sample())
            stats = writer.shutdown(timeout=5)["stats"]
        self.assertFalse(self.exposed(str(logs.output) + json.dumps(stats)), "contact exposed in writer logs")


class SecLifecycleEquivalenceTests(lifecycle.LifecycleEquivalenceTests):
    """The Phase 2G lifecycle equivalence suite, re-run with the SEC module included."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(lifecycle, "MODULES", lifecycle.MODULES + (SEC_MODULE,)))
        self.enterContext(patch.dict(lifecycle.MODULES_BY, {shadow: SEC_MODULE}))

    def test_sec_counters_match_macro_for_identical_script(self):
        from persistence.database import PersistenceError
        results = []
        for module, _, writer_name, persist, label, logger, factory in (lifecycle.MODULES[0], SEC_MODULE):
            with patch.object(module, persist, side_effect=PersistenceError("private")), self.assertLogs(logger, level="WARNING"):
                writer = getattr(module, writer_name)(engine_factory=FakeEngine, capacity=2)
                for _ in range(2): writer.submit(factory())
                writer.shutdown(timeout=5)
                self.assertFalse(writer.submit(factory()))
                results.append(lifecycle.scrub(writer.get_persistence_stats()))
        self.assertEqual(results[0], results[1])


class SecWriterOperationsTests(unittest.TestCase):
    def writer(self, **kwargs):
        writer = shadow.SecShadowWriter(engine_factory=FakeEngine, **kwargs)
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
                    RuntimeError("private"), PersistenceError("private"), {"duplicate": False, "promotion": "ambiguous"}]
        with patch.object(shadow, "persist_sec", side_effect=outcomes), \
             patch.object(macro_shadow, "persist_macro", side_effect=AssertionError("macro path")):
            writer = self.writer()
            self.assertEqual(writer.thread.name, "mias-sec-shadow")
            with self.assertLogs("sec_shadow", level="INFO") as logs:
                for _ in range(5): self.assertTrue(writer.submit(sample()))
                result = writer.shutdown()
        stats = result["stats"]
        self.assertEqual(set(stats), set(macro_shadow.empty_stats()))
        self.assertEqual((stats["queued"], stats["persisted"], stats["duplicate"], stats["failed"],
                          stats["promotion_held"], stats["promotion_ambiguous"]), (5, 3, 1, 2, 1, 1))
        self.assertEqual((stats["worker_started"], stats["worker_stopped"], stats["queue_depth"], stats["in_flight"]), (1, 1, 0, 0))
        self.assertTrue(stats["last_success_at"] and stats["last_failure_at"])
        self.assertTrue(all("SEC shadow" in line for line in logs.output))
        self.assertNotIn("private", str(logs.output) + str(stats))

    def test_queue_full_drain_timeout_immediate_stop_and_restart(self):
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_sec", side_effect=write):
            writer = self.writer(capacity=1)
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                self.assertTrue(writer.submit(sample()))
                with self.assertLogs("sec_shadow", level="WARNING"):
                    self.assertFalse(writer.submit(sample()))
                    result = writer.shutdown(timeout=0.02)
                self.assertEqual((result["timed_out"], result["unprocessed"], result["stats"]["dropped_queue_full"],
                                  result["stats"]["dropped_shutdown"], result["stats"]["in_flight"]), (True, 2, 1, 1, 1))
                self.assertEqual(writer.shutdown(timeout=0)["stats"]["drain_timeouts"], 1)  # Idempotent.
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_sec", side_effect=write):
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
        with patch.object(shadow, "persist_sec", return_value={"duplicate": True}):
            restarted = self.writer()
            self.assertTrue(restarted.submit(sample()))
            self.assertEqual(restarted.shutdown(drain=True, timeout=5)["stats"]["duplicate"], 1)

    def test_graceful_drain_persists_everything_queued(self):
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_sec", side_effect=write):
            writer = self.writer()
            writer.submit(sample())
            self.assertTrue(entered.wait(1))
            for _ in range(3): writer.submit(sample())
            release.set()
            result = writer.shutdown(drain=True, timeout=5)
        self.assertEqual((result["stopped"], result["timed_out"], result["unprocessed"], result["stats"]["persisted"]),
                         (True, False, 0, 4))


if __name__ == "__main__":
    unittest.main()
