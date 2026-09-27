"""Explicit collector side-effect controls (Phase 7E):
- news ``read_feed(enable_ai, send_alerts)``, ``collect_all_sources(news_enable_ai, news_send_alerts)`` and
  ``--no-ai``/``--no-send-alerts``;
- SEC ``process_sec_filings(send_alerts)`` and ``--no-send-alerts``.

Every external side effect is a dependency-boundary tripwire: the OpenAI entry point, the AI quality adjustment,
alert formatting and Telegram delivery are patched to raise if reached. Sockets refuse to connect. Nothing relies on
missing credentials. Redis is in memory, and shadow submissions are captured, never written to a database.
"""
from copy import deepcopy
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from hashlib import sha256
import os
import runpy
import socket
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Prevent configuration imports from consulting local secrets (same pattern as the existing collector tests).
with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "ALERT_THRESHOLD": "70", "DISPLAY_THRESHOLD": "40", "RSS_ENTRY_LIMIT": "10",
    "REDIS_HOST": "localhost", "REDIS_PORT": "6379", "DEDUP_TTL_SECONDS": "86400",
    "HEADLINE_TTL_SECONDS": "86400", "NEAR_DUPLICATE_THRESHOLD": "0.80",
}, clear=True):
    from collector import multi_source_collector as multi
    from collector import rss_reader as news
    from collector import sec_collector as sec
    from analyzer import deduplicator, scoring_engine

from persistence.adapters.news import adapt_news
from persistence.adapters.sec import adapt_sec

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 9, 24, 12, 5, tzinfo=timezone.utc)
PUBLISHED = "Thu, 24 Sep 2026 11:00:00 +0000"


def entry(title, link, summary="", publisher="Reuters"):
    return {"title": title, "link": link, "published": PUBLISHED, "summary": summary,
            "source": {"title": publisher}}


ALERT = entry("Meta earnings guidance beat as AI demand grows", "https://news.example/meta-alert")
DISPLAY = entry("Meta launches a new feature", "https://news.example/meta-display", publisher="Some Blog")
IGNORE = entry("Chip stocks move", "https://news.example/meta-ignore", summary="Instagram usage", publisher="Some Blog")


class MemoryRedis:
    def __init__(self):
        self.data = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def get(self, key):
        return self.data.get(key)

    def scan_iter(self, match="*"):
        return [k for k in list(self.data) if fnmatchcase(k, match)]


def tripwire(name):
    return Mock(side_effect=AssertionError(f"{name} must not be reached"))


class Base(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("network access")))
        self.enterContext(patch.object(deduplicator, "redis_client", MemoryRedis()))


class NewsControlTests(Base):
    def run_feed(self, entries, *, ai=None, adjust=None, send=None, fmt=None, **controls):
        """One real read_feed pass; returns (events, captured shadow submissions, side-effect mocks, stdout)."""
        from persistence import news_shadow
        deduplicator.redis_client.data.clear()  # Each pass is an independent run (dedupe state never carries over).
        submitted = []
        mocks = dict(ai=ai or Mock(side_effect=lambda e: dict(e, ai_summary="s", ai_sentiment="NEUTRAL",
                                                             ai_confidence=50, ai_why_it_matters="w",
                                                             ai_event_type="earnings")),
                     adjust=adjust, send=send or Mock(return_value={"ok": True, "result": {"message_id": 1}}),
                     fmt=fmt)
        parsed = SimpleNamespace(bozo=False, feed={"title": "Feed"}, entries=deepcopy(entries))
        out = StringIO()
        with patch.object(news.feedparser, "parse", return_value=parsed), \
                patch.object(news, "NEWS_PERSISTENCE_SHADOW_ENABLED", True), \
                patch.object(news_shadow, "submit_news", side_effect=lambda v, **k: submitted.append(deepcopy(v))), \
                patch.object(news, "analyze_market_event", mocks["ai"]), \
                patch.object(news, "send_telegram_alert", mocks["send"]), \
                patch.object(scoring_engine, "datetime", wraps=datetime) as clock, redirect_stdout(out), \
                patch.object(news, "adjust_alert_quality", mocks["adjust"]) if adjust else _nothing(), \
                patch.object(news, "format_alert", mocks["fmt"]) if fmt else _nothing():
            clock.now.side_effect = lambda tz=None: NOW
            events, stats = news.read_feed("https://feeds.example/x", source_label="Google News META", **controls)
        return events, submitted, mocks, out.getvalue()

    def test_scores_fixture_as_expected(self):
        _, stored, _, _ = self.run_feed([ALERT, DISPLAY, IGNORE], enable_ai=False, send_alerts=False)
        self.assertEqual([e["alert_decision"] for e in stored], ["ALERT", "DISPLAY_ONLY", "IGNORE"])

    def test_send_alerts_false_never_delivers_but_persists(self):
        _, stored, mocks, out = self.run_feed([ALERT], send=tripwire("telegram"), fmt=tripwire("format_alert"),
                                              send_alerts=False)
        mocks["send"].assert_not_called()
        self.assertEqual([(e["news_collector_outcome"], e["alert_decision"]) for e in stored], [("processed", "ALERT")])
        self.assertEqual(mocks["ai"].call_count, 1)  # AI stays on: only delivery was disabled.
        self.assertNotIn("MIAS MARKET ALERT", out)

    def test_enable_ai_false_never_enriches_and_persists_pre_ai_state(self):
        _, stored, mocks, _ = self.run_feed([ALERT], ai=tripwire("openai"), adjust=tripwire("quality_adjustment"),
                                            enable_ai=False)
        mocks["ai"].assert_not_called()
        mocks["adjust"].assert_not_called()
        event = stored[0]
        self.assertEqual((event["alert_decision"], event["news_collector_outcome"]), ("ALERT", "processed"))
        for key in ("original_impact_score", "quality_adjustment"):
            self.assertNotIn(key, event)
        self.assertFalse([k for k in event if k.startswith("ai_")])
        self.assertEqual(mocks["send"].call_count, 1)  # Delivery stays on: only AI was disabled.

    def test_both_disabled_no_side_effects_persistence_and_identity(self):
        _, stored, mocks, out = self.run_feed([ALERT, DISPLAY, IGNORE], ai=tripwire("openai"),
                                              adjust=tripwire("quality_adjustment"), send=tripwire("telegram"),
                                              fmt=tripwire("format_alert"), enable_ai=False, send_alerts=False)
        for name in ("ai", "adjust", "send", "fmt"):
            mocks[name].assert_not_called()
        self.assertEqual(len(stored), 3)
        adapted = adapt_news(stored[0], OBSERVED)
        self.assertEqual(adapted["record"]["identity_version"], "news-url-v1")
        self.assertEqual(adapted["record"]["event_key"],
                         sha256(b"news-url-v1|https://news.example/meta-alert").hexdigest())
        self.assertNotIn("MIAS MARKET ALERT", out)
        _, default_stored, _, _ = self.run_feed([ALERT])  # Production defaults: identity is independent of mode.
        self.assertEqual(adapt_news(default_stored[0], OBSERVED)["record"]["event_key"], adapted["record"]["event_key"])

    def test_display_only_and_ignore_identical_with_ai_on_or_off(self):
        _, with_ai, mocks_on, _ = self.run_feed([DISPLAY, IGNORE])
        _, without_ai, _, _ = self.run_feed([DISPLAY, IGNORE], ai=tripwire("openai"), enable_ai=False,
                                            send_alerts=False)
        self.assertEqual(with_ai, without_ai)
        mocks_on["ai"].assert_not_called()  # AI only ever runs for ALERT items.
        self.assertEqual([adapt_news(e, OBSERVED) for e in with_ai], [adapt_news(e, OBSERVED) for e in without_ai])

    def test_no_ai_alert_equals_existing_ai_failure_state(self):
        _, no_ai, _, _ = self.run_feed([ALERT], enable_ai=False, send_alerts=False)
        _, failed, mocks, _ = self.run_feed([ALERT], ai=Mock(side_effect=RuntimeError("OpenAI unavailable")),
                                            send_alerts=False)
        self.assertEqual(mocks["ai"].call_count, 1)
        self.assertEqual(no_ai, failed)
        self.assertEqual(adapt_news(no_ai[0], OBSERVED), adapt_news(failed[0], OBSERVED))

    def test_defaults_preserve_ai_and_delivery(self):
        _, stored, mocks, out = self.run_feed([ALERT, DISPLAY])
        self.assertEqual((mocks["ai"].call_count, mocks["send"].call_count), (1, 1))
        self.assertIn("MIAS MARKET ALERT", out)
        self.assertIn("quality_adjustment", stored[0])  # The existing AI quality step ran.
        self.assertIn("original_impact_score", stored[0])

    def test_collect_all_sources_forwards_controls(self):
        counts = dict(fetched=0, relevant=0, duplicates=0, processed=0)
        with patch.object(multi, "read_feed", return_value=([], counts)) as read:
            multi.collect_all_sources()
            multi.collect_all_sources(news_enable_ai=False, news_send_alerts=False)
        kwargs = [c.kwargs for c in read.call_args_list]
        half = len(kwargs) // 2
        self.assertTrue(half >= 1)
        self.assertTrue(all((k["enable_ai"], k["send_alerts"]) == (True, True) for k in kwargs[:half]))
        self.assertTrue(all((k["enable_ai"], k["send_alerts"]) == (False, False) for k in kwargs[half:]))


class SecControlTests(Base):
    def filing(self, form):
        return dict(symbol="META", form=form, accession_number="0001326801-26-000101", filing_date="2026-09-22",
                    primary_document="meta-20260922.htm")

    def run_process(self, filings, **controls):
        from persistence import sec_shadow
        deduplicator.redis_client.data.clear()
        submitted = []
        send = self.send if hasattr(self, "send") else Mock(return_value={"ok": True, "result": {"message_id": 1}})
        out = StringIO()
        with patch.object(sec, "SEC_PERSISTENCE_SHADOW_ENABLED", True), \
                patch.object(sec_shadow, "submit_sec", side_effect=lambda v, **k: submitted.append(deepcopy(v))), \
                patch.object(sec, "send_telegram_alert", send), redirect_stdout(out):
            events = sec.process_sec_filings(filings, **controls)
        return events, submitted, send, out.getvalue()

    def test_send_alerts_false_never_delivers_and_stored_content_is_identical(self):
        self.send = tripwire("telegram")
        with patch.object(sec, "format_alert", tripwire("format_alert")):
            _, suppressed, send, out = self.run_process([self.filing("10-K"), self.filing("4")], send_alerts=False)
        send.assert_not_called()
        self.assertNotIn("MIAS MARKET ALERT", out)
        del self.send
        _, delivered, send_on, _ = self.run_process([self.filing("10-K"), self.filing("4")])
        self.assertEqual(send_on.call_count, 1)  # Default: the 10-K ALERT is delivered.
        self.assertEqual(suppressed, delivered)
        self.assertEqual([e["alert_decision"] for e in suppressed], ["ALERT", "DISPLAY_ONLY"])

    def test_sec_identity_unchanged(self):
        _, stored, _, _ = self.run_process([self.filing("10-K")], send_alerts=False)
        record = adapt_sec(stored[0], OBSERVED)["record"]
        self.assertEqual(record["identity_version"], "sec-v1")
        self.assertEqual(record["event_key"], deduplicator.create_fingerprint(stored[0]))
        self.assertEqual(stored[0]["sec_fingerprint"], record["event_key"])

    def test_sec_has_no_ai_parameter(self):
        import inspect
        self.assertEqual(list(inspect.signature(sec.process_sec_filings).parameters), ["filings", "send_alerts"])
        self.assertFalse(hasattr(sec.parse_args([]), "no_ai"))


class CliTests(Base):
    def test_parse_args(self):
        self.assertEqual(vars(multi.parse_args([])), dict(no_ai=False, no_send_alerts=False))
        self.assertEqual(vars(multi.parse_args(["--no-ai", "--no-send-alerts"])), dict(no_ai=True, no_send_alerts=True))
        self.assertEqual(vars(sec.parse_args([])), dict(no_send_alerts=False))
        self.assertEqual(vars(sec.parse_args(["--no-send-alerts"])), dict(no_send_alerts=True))
        for parse, bad in ((sec.parse_args, ["--no-ai"]), (sec.parse_args, ["--no-send-alert-typo"]),
                           (multi.parse_args, ["--no-aii"]), (multi.parse_args, ["--send-alerts"])):
            with redirect_stdout(StringIO()), patch("sys.stderr", StringIO()), self.assertRaises(SystemExit) as caught:
                parse(bad)  # Unknown options fail closed (SEC has no AI control at all).
            self.assertEqual(caught.exception.code, 2)
        # Stray positional arguments stay ignored, as these entry points always did (e.g. a test runner's argv).
        self.assertEqual(vars(sec.parse_args(["tests.test_sec_pipeline"])), dict(no_send_alerts=False))
        self.assertEqual(vars(multi.parse_args(["extra", "--no-ai"])), dict(no_ai=True, no_send_alerts=False))

    def run_news_main(self, argv):
        counts = dict(fetched=0, relevant=0, duplicates=0, processed=0)
        with patch("collector.rss_reader.read_feed", return_value=([], counts)) as read, \
                patch.object(sys, "argv", ["multi_source_collector", *argv]), redirect_stdout(StringIO()):
            runpy.run_module("collector.multi_source_collector", run_name="__main__")
        return {(c.kwargs["enable_ai"], c.kwargs["send_alerts"]) for c in read.call_args_list}

    def test_news_cli_reaches_function_arguments(self):
        self.assertEqual(self.run_news_main([]), {(True, True)})
        self.assertEqual(self.run_news_main(["--no-ai", "--no-send-alerts"]), {(False, False)})
        self.assertEqual(self.run_news_main(["--no-send-alerts"]), {(True, False)})

    def run_sec_main(self, argv):
        body = {"filings": {"recent": {"form": ["10-K"], "accessionNumber": ["0001326801-26-000101"],
                                       "filingDate": ["2026-09-22"], "primaryDocument": ["meta-20260922.htm"]}}}
        response = Mock(json=Mock(return_value=body), raise_for_status=Mock())
        send = Mock(return_value={"ok": True, "result": {"message_id": 1}})
        deduplicator.redis_client.data.clear()
        with patch("requests.get", return_value=response), \
                patch("alert_engine.telegram_notifier.send_telegram_alert", send), \
                patch.object(sys, "argv", ["sec_collector", *argv]), redirect_stdout(StringIO()):
            runpy.run_module("collector.sec_collector", run_name="__main__")
        return send.call_count

    def test_sec_cli_reaches_function_arguments(self):
        self.assertEqual(self.run_sec_main(["--no-send-alerts"]), 0)
        self.assertEqual(self.run_sec_main([]), 2)  # One ALERT 10-K per configured company (META, NVDA).


class _nothing:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
