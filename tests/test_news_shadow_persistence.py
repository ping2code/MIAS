"""News shadow must not change collector output, Redis dedup/near-duplicate state, AI, Telegram or logs."""
import os
import runpy
from threading import Event
import unittest
from unittest.mock import Mock, patch

from persistence import news_shadow as shadow
from persistence import macro_shadow
from tests import news_readiness_corpus as corpus
from tests import test_shadow_lifecycle as lifecycle
from tests.test_news_persistence_adapter import sample
from tests.test_treasury_shadow_persistence import FakeEngine

news = corpus.news
NEWS_MODULE = (shadow, "submit_news", "NewsShadowWriter", "persist_news", "News", "news_shadow", sample)


def replay(enabled, submit=None, steps=None):
    """Whole corpus step sequence; returns every observable collector output per step."""
    redis, outputs, submitted = corpus.NewsMemoryRedis(), [], []
    for label, feed, names, clock, mode, telegram_ok in steps or corpus.STEPS:
        if mode == "fresh":
            redis = corpus.NewsMemoryRedis()
        target = corpus.FailingRedis() if mode == "failing" else redis
        result, captured = corpus.run_collector(feed, [corpus.ENTRIES[n] for n in names], target, now=clock,
                                                telegram_ok=telegram_ok, enabled=enabled, submit=submit)
        outputs.append((label, result))
        submitted.extend((label, event, kwargs) for event, kwargs in captured)
    return outputs, submitted


class NewsCollectorShadowTests(unittest.TestCase):
    def test_disabled_does_not_initialize_or_submit(self):
        with patch.object(shadow, "runtime_engine", side_effect=AssertionError("DB forbidden")), \
             patch.object(shadow, "NewsShadowWriter", side_effect=AssertionError("worker forbidden")):
            outputs, submitted = replay(False)
        self.assertEqual(submitted, [])
        self.assertIsNone(shadow._writer)
        self.assertEqual(corpus.build_rows()[1], corpus.load_corpus()[0]["expected_collector_outcomes"])

    def test_enabled_identical_outputs_redis_ai_and_telegram(self):
        baseline, _ = replay(False)
        enabled, submitted = replay(True)
        self.assertEqual(enabled, baseline)  # Events, stats, stdout, logs, Redis, Telegram, AI calls, feed reads.
        self.assertEqual(len(submitted), corpus.load_corpus()[0]["expected_observations"])
        for _, result in enabled:
            for event in result["events"]:
                self.assertFalse({"news_fingerprint", "news_collector_outcome"} & set(event))
        keys = {key.rsplit(":", 1)[0] for _, result in enabled for key, _ in result["redis"]}
        self.assertEqual(keys, {"mias:news:event", "mias:news:headline"})  # No persistence keys, ever.

    def test_only_processed_and_near_duplicate_outcomes_are_submitted(self):
        _, submitted = replay(True)
        by_label = {}
        for label, event, _ in submitted:
            by_label.setdefault(label, []).append(event["news_collector_outcome"])
        for skipped in ("no_symbol", "exact_duplicate", "cross_feed_duplicate"):
            self.assertNotIn(skipped, by_label)  # Irrelevant items and exact duplicates are never scored.
        self.assertEqual(by_label["near_duplicate_above"], ["processed", "near_duplicate_suppressed"])
        self.assertEqual(by_label["near_duplicate_below"], ["processed", "processed"])
        self.assertEqual(by_label["same_headline_other_publisher"], ["near_duplicate_suppressed"])

    def test_hook_observes_final_state_after_ai_and_telegram(self):
        order = []
        def ai(event):
            order.append("ai")
            return corpus.fake_ai(event)
        def telegram(message):
            order.append("telegram")
            return {"result": {"message_id": 1}}
        def submit(event, **kwargs):
            order.append(("shadow", event["alert_decision"], event.get("ai_event_type"), event["impact_score"]))
        redis = corpus.NewsMemoryRedis()
        with patch.object(corpus.deduplicator, "redis_client", redis), \
             patch.object(news, "NEWS_PERSISTENCE_SHADOW_ENABLED", True), \
             patch.object(shadow, "submit_news", side_effect=submit), \
             patch.object(news, "analyze_market_event", side_effect=ai), \
             patch.object(news, "send_telegram_alert", side_effect=telegram), \
             patch.object(news.feedparser, "parse", return_value=corpus.SimpleNamespace(
                 bozo=False, feed={}, entries=[corpus.ENTRIES["ai_penalty"], corpus.ENTRIES["meta_reuters"]])), \
             patch.object(corpus.scoring_engine, "datetime", wraps=corpus.datetime) as clock, \
             patch("sys.stdout"), self.assertLogs("collector", "INFO"):
            clock.now.side_effect = lambda tz=None: corpus.NOW
            news.read_feed("https://feeds.example/mias-fixture", source_label="Google News META")
        # The penalty turned ALERT into DISPLAY_ONLY before delivery; the shadow copy saw the final state.
        self.assertEqual(order, ["ai", ("shadow", "DISPLAY_ONLY", "prediction article", 65),
                                 "ai", "telegram", ("shadow", "ALERT", "product launch", 90)])

    def test_submission_failure_does_not_change_outputs(self):
        baseline, _ = replay(False)
        with patch.object(news, "_shadow_last_failure", float("-inf")):
            failed, _ = replay(True, submit=Mock(side_effect=RuntimeError("password=secret")))
        for (label, on), (_, off) in zip(failed, baseline):
            warnings = [entry for entry in on["logs"] if entry[0] == "warning"]
            self.assertTrue(all(entry == ("warning", ("News shadow submission failed",)) for entry in warnings))
            self.assertEqual(dict(on, logs=[e for e in on["logs"] if e[0] != "warning"]), off, label)
        self.assertEqual(sum(e[0] == "warning" for _, r in failed for e in r["logs"]), 1)  # Rate limited.
        self.assertNotIn("secret", str(failed))

    def test_real_writer_success_and_db_unavailable_leave_outputs_identical(self):
        baseline, _ = replay(False)
        with patch.object(shadow, "persist_news", return_value={"duplicate": False}):
            writer = shadow.NewsShadowWriter(engine_factory=FakeEngine)
            succeeded, submitted = replay(True, submit=writer.submit)
            stats = writer.shutdown(timeout=5)["stats"]
        self.assertEqual(succeeded, baseline)
        self.assertEqual((stats["persisted"], stats["failed"]), (len(submitted), 0))
        with self.assertLogs("news_shadow", level="WARNING") as logs:
            down = shadow.NewsShadowWriter(engine_factory=lambda: (_ for _ in ()).throw(RuntimeError("password")))
            failed, submitted = replay(True, submit=down.submit)
            stats = down.shutdown(timeout=5)["stats"]
        self.assertEqual(failed, baseline)
        self.assertEqual((stats["failed"], stats["persisted"]), (len(submitted), 0))
        self.assertNotIn("password", str(logs.output))

    def test_slow_db_and_full_queue_do_not_block_ai_or_telegram(self):
        started, release = Event(), Event()
        def factory():
            started.set()
            release.wait(5)
            raise RuntimeError("unavailable")
        writer = shadow.NewsShadowWriter(engine_factory=factory, capacity=1)
        try:
            self.assertTrue(writer.submit(sample()))
            self.assertTrue(started.wait(1))
            self.assertTrue(writer.submit(sample()))
            baseline, _ = replay(False)
            with self.assertLogs("news_shadow", level="WARNING"):
                enabled, _ = replay(True, submit=writer.submit)
            self.assertEqual(enabled, baseline)
            self.assertFalse(release.is_set())  # Every AI call and Telegram send completed while the DB was blocked.
            self.assertGreater(writer.get_persistence_stats()["dropped_queue_full"], 0)
        finally:
            release.set()
            self.assertTrue(writer.shutdown(timeout=5)["stopped"])

    def test_config_opt_in_default(self):
        for value, expected in ((None, False), ("true", True), ("TRUE", True), ("false", False), ("1", False)):
            env = {} if value is None else {"NEWS_PERSISTENCE_SHADOW_ENABLED": value}
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, env, clear=True):
                config = runpy.run_path("shared/config.py")
            self.assertIs(config["NEWS_PERSISTENCE_SHADOW_ENABLED"], expected)
            for other in ("MACRO", "TREASURY", "GEOPOLITICAL", "FED", "SEC"):
                self.assertIs(config[f"{other}_PERSISTENCE_SHADOW_ENABLED"], False)
            self.assertEqual((config["NEAR_DUPLICATE_THRESHOLD"], config["DEDUP_TTL_SECONDS"],
                              config["HEADLINE_TTL_SECONDS"]), (0.80, 86400, 86400))
        self.assertIs(news.NEWS_PERSISTENCE_SHADOW_ENABLED, False)

    def test_runtime_engine_caps(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:secret@localhost/mias_test"}), \
             patch.object(macro_shadow, "make_engine") as make:
            shadow.runtime_engine()
        settings = make.call_args.args[0]
        self.assertEqual((settings.pool_size, settings.max_overflow, settings.connect_timeout_seconds,
                          settings.statement_timeout_ms, settings.lock_timeout_ms, settings.application_name),
                         (1, 0, 2, 1000, 500, "mias_news_shadow"))
        self.assertNotIn("secret", repr(settings))


class NewsLifecycleEquivalenceTests(lifecycle.LifecycleEquivalenceTests):
    """The Phase 2G lifecycle equivalence suite, re-run with the News module included."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(lifecycle, "MODULES", lifecycle.MODULES + (NEWS_MODULE,)))
        self.enterContext(patch.dict(lifecycle.MODULES_BY, {shadow: NEWS_MODULE}))

    def test_news_counters_match_macro_for_identical_script(self):
        from persistence.database import PersistenceError
        results = []
        for module, _, writer_name, persist, label, logger, factory in (lifecycle.MODULES[0], NEWS_MODULE):
            with patch.object(module, persist, side_effect=PersistenceError("private")), self.assertLogs(logger, level="WARNING"):
                writer = getattr(module, writer_name)(engine_factory=FakeEngine, capacity=2)
                for _ in range(2): writer.submit(factory())
                writer.shutdown(timeout=5)
                self.assertFalse(writer.submit(factory()))
                results.append(lifecycle.scrub(writer.get_persistence_stats()))
        self.assertEqual(results[0], results[1])


class NewsWriterOperationsTests(unittest.TestCase):
    def writer(self, **kwargs):
        writer = shadow.NewsShadowWriter(engine_factory=FakeEngine, **kwargs)
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
        with patch.object(shadow, "persist_news", side_effect=outcomes), \
             patch.object(macro_shadow, "persist_macro", side_effect=AssertionError("macro path")):
            writer = self.writer()
            self.assertEqual(writer.thread.name, "mias-news-shadow")
            with self.assertLogs("news_shadow", level="INFO") as logs:
                for _ in range(5): self.assertTrue(writer.submit(sample()))
                result = writer.shutdown()
        stats = result["stats"]
        self.assertEqual(set(stats), set(macro_shadow.empty_stats()))
        self.assertEqual((stats["queued"], stats["persisted"], stats["duplicate"], stats["failed"],
                          stats["promotion_held"], stats["promotion_ambiguous"]), (5, 3, 1, 2, 1, 1))
        self.assertEqual((stats["worker_started"], stats["worker_stopped"], stats["queue_depth"], stats["in_flight"]), (1, 1, 0, 0))
        self.assertTrue(stats["last_success_at"] and stats["last_failure_at"])
        self.assertTrue(all("News shadow" in line for line in logs.output))
        self.assertNotIn("private", str(logs.output) + str(stats))

    def test_queue_full_drain_timeout_immediate_stop_and_restart(self):
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_news", side_effect=write):
            writer = self.writer(capacity=1)
            try:
                writer.submit(sample())
                self.assertTrue(entered.wait(1))
                self.assertTrue(writer.submit(sample()))
                with self.assertLogs("news_shadow", level="WARNING"):
                    self.assertFalse(writer.submit(sample()))
                    result = writer.shutdown(timeout=0.02)
                self.assertEqual((result["timed_out"], result["unprocessed"], result["stats"]["dropped_queue_full"],
                                  result["stats"]["dropped_shutdown"], result["stats"]["in_flight"]), (True, 2, 1, 1, 1))
                self.assertEqual(writer.shutdown(timeout=0)["stats"]["drain_timeouts"], 1)  # Idempotent.
            finally:
                release.set()
                self.assertTrue(writer.shutdown()["stopped"])
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_news", side_effect=write):
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
        with patch.object(shadow, "persist_news", return_value={"duplicate": True}):
            restarted = self.writer()
            self.assertTrue(restarted.submit(sample()))
            self.assertEqual(restarted.shutdown(drain=True, timeout=5)["stats"]["duplicate"], 1)

    def test_graceful_drain_persists_everything_queued(self):
        entered, release, write = self.blocked()
        with patch.object(shadow, "persist_news", side_effect=write):
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
