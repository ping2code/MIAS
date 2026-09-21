"""Offline coverage: dotenv loading and all service calls are mocked."""

import importlib
import json
import os
import runpy
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Prevent configuration imports from consulting local secrets.
with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "ALERT_THRESHOLD": "70", "DISPLAY_THRESHOLD": "40",
    "RSS_ENTRY_LIMIT": "10", "REDIS_HOST": "localhost", "REDIS_PORT": "6379",
    "DEDUP_TTL_SECONDS": "86400", "HEADLINE_TTL_SECONDS": "86400",
    "NEAR_DUPLICATE_THRESHOLD": "0.80",
}, clear=True):
    from collector import fed_collector as fed
    from collector import multi_source_collector as multi
    from collector import rss_reader
    from collector.fed_normalizer import normalize_fed_entry
    from analyzer import deduplicator, openai_analyzer
    from analyzer.fed_scoring import score_fed_event
    from alert_engine.decision_engine import evaluate_alert
    from alert_engine.formatter import format_alert


def entry(title="Federal Reserve issues FOMC statement", suffix="a", **kwargs):
    return dict(title=title,
                link=f"https://www.federalreserve.gov/newsevents/pressreleases/{suffix}.htm",
                summary="<p>Monetary policy &amp; the economy.</p>",
                published="Wed, 16 Sep 2026 14:00:00 -0400", **kwargs)


class FedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 17, 18, tzinfo=timezone.utc)
        self.clock = self.enterContext(patch.object(fed, "datetime", wraps=datetime))
        self.clock.now.side_effect = lambda tz: self.now
        self.http = self.enterContext(patch.object(fed.requests, "get"))
        self.http.return_value.status_code = 200
        self.parse = self.enterContext(patch.object(fed.feedparser, "parse"))
        self.parse.return_value = {"entries": [entry()], "bozo": False}
        self.redis = self.enterContext(patch.object(deduplicator, "redis_client"))
        self.redis.set.return_value = True
        self.redis.get.return_value = None
        self.ai = self.enterContext(patch.object(fed, "analyze_fed_event", side_effect=lambda e: e))
        self.send = self.enterContext(patch.object(fed, "deliver_fed_alert"))
        self.send.return_value = {"ok": True, "result": {"message_id": 1}}

    def use_redis_store(self):
        self.store = {}
        def get(key):
            value, expires = self.store.get(key, (None, self.now))
            return value if expires > self.now else None
        def put(key, value, *, ex, nx=False):
            if nx and get(key) is not None:
                return None
            self.store[key] = (value, self.now + timedelta(seconds=ex))
            return True
        self.redis.get.side_effect = get
        self.redis.set.side_effect = put

    def test_normalization_and_macro_relevance(self):
        event = normalize_fed_entry(entry())
        self.assertEqual(event["published_at"], "2026-09-16T18:00:00+00:00")
        self.assertEqual(event["summary"], "Monetary policy & the economy.")
        self.assertEqual(event["publisher"], "Federal Reserve")
        self.assertTrue(event["relevant"])
        for field in ("symbols", "direct_symbols", "related_symbols"):
            self.assertEqual(event[field], [])
        self.assertEqual(event["market_scope"], "US macro")
        self.assertEqual(event["event_type"], "fed_policy")

    def test_actual_company_mentions(self):
        event = normalize_fed_entry(entry(title="Federal Reserve discusses NVIDIA"))
        self.assertEqual(event["direct_symbols"], ["NVDA"])

    def test_invalid_and_naive_dates(self):
        for value, expected in (("invalid", None), (None, None),
                                ("2026-09-16T18:00:00", "2026-09-16T18:00:00+00:00")):
            item = entry()
            item["published"] = value
            self.assertEqual(normalize_fed_entry(item)["published_at"], expected)

    def test_reject_unofficial_or_missing_identity(self):
        for url in ("", "https://example.com/a", "http://www.federalreserve.gov/a",
                    "https://www.federalreserve.gov.evil.example/a",
                    "https://user:password@www.federalreserve.gov/a"):
            item = entry()
            item["link"] = url
            with self.assertRaises(ValueError):
                normalize_fed_entry(item)
        with self.assertRaises(ValueError):
            normalize_fed_entry(entry(title=""))

    def test_category_scores(self):
        for title, score in (("Federal Reserve issues FOMC statement", 85),
                             ("Federal Reserve raises federal funds rate", 85),
                             ("FOMC releases economic projections", 80),
                             ("Minutes of the Federal Open Market Committee", 70),
                             ("Federal Reserve policy framework review", 40)):
            event = score_fed_event(normalize_fed_entry(entry(title=title)))
            self.assertEqual(event["impact_score"], score)
            self.assertTrue(event["score_reasons"])

    def test_decision_boundaries_unchanged(self):
        for score, decision in ((39, "IGNORE"), (40, "DISPLAY_ONLY"),
                                (69, "DISPLAY_ONLY"), (70, "ALERT")):
            self.assertEqual(evaluate_alert({"impact_score": score})["alert_decision"], decision)

    def test_candidate_flow_and_opt_in_delivery(self):
        events, stats = fed.collect_fed_events()
        self.assertEqual(stats, dict(fetched=1, relevant=1, duplicates=0, processed=1))
        self.ai.assert_called_once()
        self.send.assert_not_called()
        self.assertEqual(events[0]["alert_decision"], "ALERT")
        self.http.assert_called_once_with(fed.FED_FEED_URL, timeout=15, allow_redirects=False)
        key = self.redis.set.call_args_list[0].args[0]
        self.assertTrue(key.startswith("mias:fed:event:"))
        self.assertEqual(self.redis.set.call_args_list[0].kwargs, {"nx": True, "ex": 86400})
        self.redis.scan_iter.assert_not_called()

    def test_duplicate_skips_ai_and_delivery(self):
        self.redis.set.return_value = None
        events, stats = fed.collect_fed_events(send_alerts=True)
        self.assertEqual(events, [])
        self.assertEqual(stats["duplicates"], 1)
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_repeated_poll_and_distinct_release_urls(self):
        seen = set()
        def set_once(key, *args, **kwargs):
            if key in seen:
                return None
            seen.add(key)
            return True
        self.redis.set.side_effect = set_once
        self.parse.return_value["entries"] = [entry(), entry(suffix="b")]
        self.assertEqual(len(fed.collect_fed_events(enable_ai=False)[0]), 2)
        self.assertEqual(fed.collect_fed_events()[1]["duplicates"], 2)

    def test_redis_failure_keeps_processing(self):
        self.redis.set.side_effect = deduplicator.redis.RedisError("offline")
        with self.assertLogs("deduplicator", level="ERROR"):
            events, _ = fed.collect_fed_events(enable_ai=False)
        self.assertEqual(len(events), 1)

    def test_ai_disabled_and_display_only_skip_analysis(self):
        fed.collect_fed_events(enable_ai=False)
        self.parse.return_value["entries"] = [entry(title="Policy framework review")]
        events, _ = fed.collect_fed_events(send_alerts=True)
        self.assertEqual(events[0]["alert_decision"], "DISPLAY_ONLY")
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_ai_downgrade_prevents_delivery(self):
        self.parse.return_value["entries"] = [entry(title="Minutes of the FOMC")]
        self.ai.side_effect = lambda e: dict(e, ai_event_type="opinion")
        events, _ = fed.collect_fed_events(send_alerts=True)
        self.assertEqual(events[0]["impact_score"], 60)
        self.assertEqual(events[0]["alert_decision"], "DISPLAY_ONLY")
        self.send.assert_not_called()

    def test_ai_failure_retains_original_and_redacts_exception(self):
        def fail(event):
            event["impact_score"] = 0
            event["score_reasons"].append("partial mutation")
            raise RuntimeError("sensitive-value")
        self.ai.side_effect = fail
        with self.assertLogs("fed_collector", level="ERROR") as logs:
            events, _ = fed.collect_fed_events(send_alerts=True)
        self.assertEqual(events[0]["impact_score"], 85)
        self.assertNotIn("partial mutation", events[0]["score_reasons"])
        self.assertNotIn("sensitive-value", str(logs.output))
        self.send.assert_called_once()

    def test_delivery_failure_does_not_drop_event_or_expose_details(self):
        self.send.side_effect = RuntimeError("secret-token")
        with self.assertLogs("fed_collector", level="ERROR") as logs:
            events, _ = fed.collect_fed_events(send_alerts=True)
        self.assertEqual(len(events), 1)
        self.assertNotIn("secret-token", str(logs.output))

    def test_fetch_failure_and_redirect(self):
        self.http.side_effect = fed.requests.Timeout("private-detail")
        with self.assertLogs("fed_collector", level="ERROR"):
            self.assertEqual(fed.collect_fed_events()[0], [])
        self.http.side_effect = None
        self.http.return_value.status_code = 302
        with self.assertLogs("fed_collector", level="ERROR"):
            self.assertEqual(fed.collect_fed_events()[1]["fetched"], 0)
        self.ai.assert_not_called()

    def test_empty_malformed_and_entry_limit(self):
        self.parse.return_value = {"bozo": True, "entries": [entry(title=""), entry()]}
        with patch.object(fed, "RSS_ENTRY_LIMIT", 1), self.assertLogs("fed_collector"):
            events, stats = fed.collect_fed_events()
        self.assertEqual(events, [])
        self.assertEqual(stats["fetched"], 1)
        self.assertEqual(stats["processed"], 0)
        self.parse.return_value = {"entries": []}
        self.assertEqual(fed.collect_fed_events()[0], [])

    def test_actual_rss_parsing(self):
        # Temporarily remove the parser mock, keeping HTTP mocked.
        self.parse.side_effect = None
        xml = b'''<rss version="2.0"><channel><title>Monetary Policy</title>
        <item><title>Federal Reserve issues FOMC statement</title>
        <link>https://www.federalreserve.gov/newsevents/pressreleases/a.htm</link>
        <pubDate>Wed, 16 Sep 2026 14:00:00 -0400</pubDate>
        <description>Policy announcement</description></item></channel></rss>'''
        import feedparser.api
        self.parse.side_effect = feedparser.api.parse
        self.http.return_value.content = xml
        events, _ = fed.collect_fed_events(enable_ai=False)
        self.assertEqual(events[0]["fed_category"], "policy_statement")

    def test_multi_source_default_and_opt_in(self):
        stats = dict(fetched=1, relevant=1, duplicates=0, processed=1)
        with patch.object(multi, "read_feed", return_value=([{"source": "news"}], stats)), \
                patch.object(fed, "collect_fed_events", return_value=([{"source": "fed"}], stats)) as collect:
            events, totals = multi.collect_all_sources()
            self.assertEqual(len(events), len(multi.RSS_SOURCES))
            collect.assert_not_called()
            events, totals = multi.collect_all_sources(include_fed=True, fed_enable_ai=False)
            self.assertEqual(totals["processed"], len(multi.RSS_SOURCES) + 1)
            collect.assert_called_once_with(enable_ai=False, send_alerts=False)

    def test_stale_entries_skip_before_redis_ai_and_delivery(self):
        self.now = datetime(2026, 9, 18, 18, 0, 1, tzinfo=timezone.utc)
        with self.assertLogs("fed_collector", level="INFO") as logs:
            events, stats = fed.collect_fed_events(send_alerts=True)
        self.assertEqual(events, [])
        self.assertEqual(stats["processed"], 0)
        self.assertIn("Skipping stale Fed event", str(logs.output))
        self.redis.set.assert_not_called()
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_freshness_boundary_and_configured_cutoff(self):
        self.now = datetime(2026, 9, 18, 18, tzinfo=timezone.utc)
        self.assertEqual(len(fed.collect_fed_events(enable_ai=False)[0]), 1)
        with patch.object(fed, "FED_MAX_AGE_HOURS", 24), self.assertLogs("fed_collector"):
            self.assertEqual(fed.collect_fed_events(enable_ai=False)[0], [])
        with patch.object(fed, "FED_MAX_AGE_HOURS", 72):
            self.assertEqual(len(fed.collect_fed_events(enable_ai=False)[0]), 1)

    def test_unknown_date_cannot_bypass_freshness(self):
        for raw in (None, "invalid"):
            self.parse.return_value["entries"][0]["published"] = raw
            with self.assertLogs("fed_collector"):
                self.assertEqual(fed.collect_fed_events(send_alerts=True)[0], [])
        self.send.assert_not_called()
        self.ai.assert_not_called()

    def test_dry_run_preserves_delivery_without_reprocessing(self):
        self.use_redis_store()
        fed.collect_fed_events(send_alerts=False)
        self.send.assert_not_called()
        self.assertFalse(any(":delivered:" in key for key in self.store))
        events, stats = fed.collect_fed_events(send_alerts=True)
        self.assertEqual(events, [])
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["processed"], 0)
        self.ai.assert_called_once()
        self.send.assert_called_once()
        fed.collect_fed_events(send_alerts=True)
        self.send.assert_called_once()

    def test_failed_delivery_retries_cached_event(self):
        self.use_redis_store()
        self.send.side_effect = [RuntimeError("offline"), {"ok": True}]
        with self.assertLogs("fed_collector", level="ERROR"):
            fed.collect_fed_events(send_alerts=True)
        self.assertFalse(any(":delivered:" in key for key in self.store))
        _, stats = fed.collect_fed_events(send_alerts=True)
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(self.send.call_count, 2)
        self.ai.assert_called_once()
        fed.collect_fed_events(send_alerts=True)
        self.assertEqual(self.send.call_count, 2)

    def test_telegram_negative_ack_remains_retryable(self):
        self.use_redis_store()
        self.send.return_value = {"ok": False}
        with self.assertLogs("fed_collector", level="ERROR"):
            fed.collect_fed_events(send_alerts=True)
        self.send.return_value = {"ok": True}
        fed.collect_fed_events(send_alerts=True)
        self.assertEqual(self.send.call_count, 2)

    def test_processing_ttl_expiry_does_not_repeat_delivery_then_stale_is_skipped(self):
        self.use_redis_store()
        self.now = datetime(2026, 9, 16, 18, tzinfo=timezone.utc)
        fed.collect_fed_events(send_alerts=True)
        self.now += timedelta(hours=25)
        _, stats = fed.collect_fed_events(send_alerts=True)
        self.assertEqual(stats["duplicates"], 1)
        self.send.assert_called_once()
        self.ai.assert_called_once()
        self.now += timedelta(hours=24)
        with self.assertLogs("fed_collector"):
            _, stats = fed.collect_fed_events(send_alerts=True)
        self.assertEqual(stats["processed"], 0)
        self.send.assert_called_once()

    def test_failed_delivery_expires_with_freshness_window(self):
        self.use_redis_store()
        self.send.side_effect = RuntimeError("offline")
        with self.assertLogs("fed_collector", level="ERROR"):
            fed.collect_fed_events(send_alerts=True)
        self.now += timedelta(hours=25)
        with self.assertLogs("fed_collector"):
            fed.collect_fed_events(send_alerts=True)
        self.send.assert_called_once()


class ExistingBehaviorTests(unittest.TestCase):
    def test_freshness_environment_default_and_override(self):
        # Execute config in an isolated namespace, leaving imported settings intact.
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, {}, clear=True):
            defaults = runpy.run_path("shared/config.py")
            self.assertEqual(defaults["FED_MAX_AGE_HOURS"], 48)
            with patch.dict(os.environ, {"FED_MAX_AGE_HOURS": "12"}):
                self.assertEqual(runpy.run_path("shared/config.py")["FED_MAX_AGE_HOURS"], 12)

    def test_macro_and_equity_formatting(self):
        message = format_alert({"symbols": [], "market_scope": "US macro"})
        self.assertIn("Ticker         : N/A", message)
        self.assertIn("Market Scope   : US macro", message)
        self.assertIn("Ticker         : N/A", format_alert({}))
        equity = format_alert({"symbols": ["NVDA", "META"]})
        self.assertIn("Ticker         : NVDA, META", equity)
        self.assertNotIn("Market Scope", equity)

    def test_openai_initialization_is_lazy_and_cached(self):
        with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv") as dotenv, \
                patch("openai.OpenAI") as constructor:
            importlib.reload(openai_analyzer)
            importlib.reload(fed)
            constructor.assert_not_called()
            dotenv.assert_not_called()
            result = dict(summary="Policy update", sentiment="NEUTRAL", confidence=75,
                          why_it_matters="Policy affects markets", event_type="official announcement")
            constructor.return_value.responses.create.return_value.output_text = json.dumps(result)
            try:
                event = openai_analyzer.analyze_market_event({"headline": "FOMC statement"})
                openai_analyzer.analyze_market_event({"headline": "FOMC statement"})
                constructor.assert_called_once()
                self.assertEqual(event["ai_confidence"], 75)
                self.assertEqual(event["ai_event_type"], "official announcement")
            finally:
                openai_analyzer.client = None

    def test_existing_news_alert_flow(self):
        feed = SimpleNamespace(bozo=False, feed={"title": "News"}, entries=[
            dict(title="NVIDIA earnings guidance partnership", summary="",
                 link="https://example.com/story")])
        with patch.object(rss_reader.feedparser, "parse", return_value=feed), \
                patch.object(rss_reader, "is_duplicate", return_value=False), \
                patch.object(rss_reader, "is_near_duplicate_headline", return_value=False), \
                patch.object(rss_reader, "analyze_market_event", side_effect=lambda e: e) as ai, \
                patch.object(rss_reader, "send_telegram_alert", return_value={"result": {"message_id": 1}}) as send, \
                redirect_stdout(StringIO()):
            events, stats = rss_reader.read_feed("https://example.com/feed")
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(events[0]["alert_decision"], "ALERT")
        ai.assert_called_once()
        send.assert_called_once()

    def test_existing_smoke_scripts_with_mocked_services(self):
        response = dict(summary="Test", sentiment="NEUTRAL", confidence=75,
                        why_it_matters="Test", event_type="official announcement")
        client = Mock()
        client.responses.create.return_value.output_text = json.dumps(response)
        with patch("dotenv.load_dotenv"), patch("openai.OpenAI", return_value=client), \
                patch.object(openai_analyzer, "client", client), \
                patch("alert_engine.telegram_notifier.send_telegram_alert",
                      return_value={"result": {"message_id": 1}}) as send, \
                redirect_stdout(StringIO()):
            for module in ("test_alert_formatter", "test_headline_similarity", "test_openai",
                           "test_openai_analyzer", "test_full_alert_flow", "test_telegram"):
                runpy.run_module(f"tests.{module}", run_name="__main__")
        self.assertEqual(send.call_count, 2)
        self.assertIn("MIAS MARKET ALERT", format_alert({"symbols": []}))


if __name__ == "__main__":
    unittest.main()
