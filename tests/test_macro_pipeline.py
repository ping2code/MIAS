"""Offline macro ingestion tests. No dotenv reads or live service calls."""

import os
import json
import runpy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "ALERT_THRESHOLD": "70", "DISPLAY_THRESHOLD": "40", "MACRO_MAX_AGE_HOURS": "48",
    "REDIS_HOST": "localhost", "REDIS_PORT": "6379", "DEDUP_TTL_SECONDS": "86400",
}, clear=True):
    from collector import macro_collector as macro
    from collector import multi_source_collector as multi
    from collector.macro_normalizer import (
        MACRO_SOURCES, discover_bea_release, macro_identity, normalize_macro_release, official_url,
    )
    from analyzer.macro_scoring import score_macro_event
    from collector import bls_source as bls

FIXTURES = Path(__file__).parent / "fixtures" / "macro"
SOURCES = {source["category"]: source for source in MACRO_SOURCES}


def fixture(category):
    return (FIXTURES / f"{category}.html").read_text()


def normalize(category, html=None):
    source = SOURCES[category]
    url = source["url"]
    if source["agency"] == "bea":
        url = discover_bea_release(fixture(category + "_index"), source)
    return normalize_macro_release(html or fixture(category), source, url)


def pipeline_fixture(category):
    if SOURCES.get(category, {}).get("agency") == "bls":
        return (FIXTURES / f"{category}.rss").read_text()
    return fixture(category)


def pipeline_event(category):
    if SOURCES[category]["agency"] == "bls":
        return bls.normalize_bls_feed(pipeline_fixture(category), SOURCES[category])
    return normalize(category)


class MemoryRedis:
    """Models NX/TTL and compare-and-delete leases; never opens a socket."""

    def __init__(self, now):
        self.now, self.store, self.calls = now, {}, []

    def get(self, key):
        self.calls.append(("get", key))
        value, expires = self.store.get(key, (None, self.now()))
        if expires <= self.now():
            self.store.pop(key, None)
            return None
        return value

    def set(self, key, value, *, ex, nx=False):
        self.calls.append(("set", key))
        if nx and self.get(key) is not None:
            return None
        self.store[key] = (value, self.now() + timedelta(seconds=ex))
        return True

    def eval(self, script, key_count, *args):
        key = args[0]
        self.calls.append(("eval", key))
        if script == macro.RELEASE_LEASE:
            assert key_count == 1
            token = args[1]
            if self.get(key) == token:
                del self.store[key]
                return 1
            return 0
        assert script == macro.CACHE_IF_OWNER and key_count == 2
        _, cache_key, token, value, ttl = args
        if self.get(key) != token:
            return 0
        self.set(cache_key, value, ex=ttl)
        return 1


class Response:
    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def __init__(self, html):
        self.content = html.encode()
        self.headers = {"Content-Type": "application/rss+xml" if "<rss" in html else "text/html; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise macro.requests.HTTPError("synthetic source error")

    def iter_content(self, chunk_size):
        yield self.content


class MacroNormalizerTests(unittest.TestCase):
    def test_all_six_sources_and_schema(self):
        expected = {
            "cpi": ("2026-08", "initial", "USDL-26-1496"),
            "ppi": ("2026-08", "initial", "USDL-26-1488"),
            "employment": ("2026-08", "initial", "USDL-26-1450"),
            "gdp": ("2026-Q2", "second", "BEA-26-38"),
            "pce": ("2026-07", "initial", "BEA-26-39"),
            "retail_sales": ("2026-08", "advance", "CB26-153"),
        }
        for category, values in expected.items():
            with self.subTest(category=category):
                event = normalize(category)
                self.assertEqual(tuple(event[k] for k in ("reference_period", "release_stage", "release_id")), values)
                self.assertEqual(event["market_scope"], "US macro")
                self.assertEqual(event["event_type"], "macro_release")
                self.assertTrue(event["relevant"])
                self.assertEqual(event["symbols"], [])
                self.assertEqual(event["direct_symbols"], [])
                self.assertEqual(event["related_symbols"], [])
                self.assertEqual(event["source"], event["publisher"])
                self.assertTrue(event["published_at"].endswith("+00:00"))
                self.assertGreater(len(event["summary"]), 60)
                self.assertEqual(len(event["event_id"]), 64)

    def test_core_and_combined_release_facts_are_preserved(self):
        cpi = normalize("cpi")
        self.assertIn("less food and energy", cpi["summary"])
        self.assertIn("seasonally adjusted", cpi["summary"])
        employment = normalize("employment")
        for value in ("162,000", "4.1 percent", "hourly earnings", "revised"):
            self.assertIn(value, employment["summary"])
        pce = normalize("pce")
        self.assertIn("Excluding food and energy", pce["summary"])
        self.assertIn("one year ago", pce["summary"])
        self.assertEqual(pce["release_category"], "pce")

    def test_inline_technical_notes_does_not_cut_off_gdp_facts(self):
        event = normalize("gdp")
        self.assertIn("Technical Notes", event["summary"])
        self.assertIn("Real final sales to private domestic purchasers", event["summary"])
        self.assertIn("corporate profits", event["summary"])

    def test_bea_stops_at_next_release_and_technical_section(self):
        for tail in ("<h2>Technical Notes</h2>", "<p>Next release: October 1, 2026</p>"):
            html = fixture("pce").replace("</div></article>", tail + "<p>DO NOT INGEST FUTURE RELEASE</p></div></article>")
            self.assertNotIn("DO NOT INGEST", normalize("pce", html)["summary"])

    def test_html_and_footer_do_not_change_identity(self):
        html = fixture("cpi")
        baseline = normalize("cpi")
        changed = html.replace("0.4 percent", "<b>0.4</b> percent").replace("September 21, 2026", "September 22, 2026")
        self.assertEqual(normalize("cpi", changed)["event_id"], baseline["event_id"])
        renamed = dict(baseline, headline="Cosmetic new title", url="https://www.bls.gov/new-url")
        self.assertEqual(macro_identity(renamed), baseline["event_id"])

    def test_reused_url_new_month_is_distinct(self):
        original = normalize("cpi")
        updated = normalize("cpi", fixture("cpi").replace("AUGUST 2026", "SEPTEMBER 2026"))
        self.assertEqual(original["url"], updated["url"])
        self.assertNotEqual(original["event_id"], updated["event_id"])

    def test_all_gdp_stages_and_scores(self):
        identities = set()
        for stage, score in (("Advance", 85), ("Second", 70), ("Third", 70)):
            event = normalize("gdp", fixture("gdp").replace("Second Estimate", stage + " Estimate"))
            self.assertEqual(event["release_stage"], stage.lower())
            self.assertEqual(score_macro_event(event)["impact_score"], score)
            identities.add(event["event_id"])
        self.assertEqual(len(identities), 3)
        with self.assertRaises(ValueError):
            normalize("gdp", fixture("gdp").replace("Second Estimate", "Unspecified Estimate"))

    def test_explicit_correction_gets_new_identity_and_publication(self):
        original = normalize("cpi")
        html = fixture("cpi") + "<p>Correction: September 12, 2026 at 9:00 a.m. ET. Corrected table.</p>"
        corrected = normalize("cpi", html)
        self.assertNotEqual(original["event_id"], corrected["event_id"])
        self.assertEqual(corrected["original_published_at"], original["published_at"])
        self.assertEqual(corrected["published_at"], "2026-09-12T13:00:00+00:00")
        self.assertEqual(corrected["revision_id"], corrected["published_at"])

    def test_prior_period_revision_is_not_a_correction(self):
        event = normalize("employment")
        self.assertEqual(event["revision_id"], "original")
        self.assertEqual(normalize("retail_sales")["revision_id"], "original")
        changed = fixture("employment").replace("110,000", "115,000")
        self.assertEqual(normalize("employment", changed)["event_id"], event["event_id"])

    def test_latest_dated_correction_wins_independent_of_notice_length(self):
        html = fixture("cpi") + (
            "<p>Correction: September 12, 2026 at 9:00 a.m. ET.</p>"
            "<p>Correction: September 13, 2026 at 10:00 a.m. ET. A later, more detailed correction.</p>"
        )
        event = normalize("cpi", html)
        self.assertEqual(event["revision_id"], "2026-09-13T14:00:00+00:00")
        self.assertEqual(event["published_at"], event["revision_id"])

    def test_correction_before_original_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize("cpi", fixture("cpi") + "<p>Correction: September 1, 2026 at 9:00 a.m. ET.</p>")

    def test_dst_and_date_only_precision(self):
        self.assertEqual(normalize("cpi")["published_at"], "2026-09-11T12:30:00+00:00")
        winter = normalize("cpi", fixture("cpi").replace("September 11, 2026", "January 13, 2026"))
        self.assertEqual(winter["published_at"], "2026-01-13T13:30:00+00:00")
        retail = normalize("retail_sales")
        self.assertEqual(retail["timestamp_precision"], "date")
        self.assertEqual(retail["published_at"], "2026-09-16T04:00:00+00:00")

    def test_missing_dates_do_not_use_footer_or_next_release(self):
        html = fixture("cpi").replace("Friday, September 11, 2026", "publication unavailable")
        self.assertIsNone(normalize("cpi", html)["published_at"])
        html = fixture("gdp").replace("Wednesday, August 26, 2026", "publication unavailable")
        html += "<footer>Last modified September 21, 2026</footer>"
        self.assertIsNone(normalize("gdp", html)["published_at"])

    def test_bea_discovery_rejects_external_and_unlabeled_links(self):
        for html in ('<a href="https://evil.example/news/2026/gdp">Current Release</a>',
                     '<a href="/news/2026/old-release">Previous Release</a>',
                     '<a href="/help">Current Release</a>'):
            with self.assertRaises(ValueError):
                discover_bea_release(html, SOURCES["gdp"])
        self.assertIn("/news/2026/", discover_bea_release(fixture("gdp_index"), SOURCES["gdp"]))

    def test_url_validation_and_malformed_pages(self):
        for url in ("http://www.bls.gov/a", "https://www.bls.gov.evil.example/a",
                    "https://user:pass@www.bls.gov/a", "https://www.bls.gov:444/a"):
            with self.assertRaises(ValueError):
                official_url(url, "bls")
        for category in SOURCES:
            with self.assertRaises(ValueError):
                normalize(category, "<html>Access denied</html>")

    def test_scoring_table_and_no_news_penalties(self):
        for category, score in (("cpi", 90), ("ppi", 80), ("employment", 90),
                                ("pce", 90), ("retail_sales", 80)):
            event = score_macro_event(dict(normalize(category), ai_event_type="prediction article"))
            self.assertEqual(event["impact_score"], score)
            self.assertEqual(event["quality_adjustment"], 0)
            self.assertEqual(event["impact_level"], "HIGH")


class MacroPipelineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 11, 13, tzinfo=timezone.utc)
        clock = self.enterContext(patch.object(macro, "datetime", wraps=datetime))
        clock.now.side_effect = lambda tz: self.now
        self.redis = MemoryRedis(lambda: self.now)
        self.enterContext(patch.object(macro.deduplicator, "redis_client", self.redis))
        self.enterContext(patch.object(macro, "MACRO_SOURCES", (SOURCES["cpi"],)))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("Live HTTP forbidden")))
        self.http = self.enterContext(patch.object(macro.requests, "get", return_value=Response(pipeline_fixture("cpi"))))
        self.data = self.enterContext(patch.object(macro, "enrich_bls_event", side_effect=lambda event: event))
        self.ai = self.enterContext(patch.object(macro, "analyze_macro_event", side_effect=self.enrich))
        self.send = self.enterContext(patch.object(macro, "deliver_macro_alert", return_value={"ok": True}))
        self.near = self.enterContext(patch.object(macro.deduplicator, "is_near_duplicate_headline",
                                                  side_effect=AssertionError("News fuzzy dedup forbidden")))

    @staticmethod
    def enrich(event):
        return dict(event, ai_summary="Official statistical update", ai_sentiment="NEUTRAL",
                    ai_confidence=80, ai_why_it_matters="Rates and equities may react",
                    ai_event_type="official statistics")

    def test_default_flow_and_isolated_namespace(self):
        events, stats = macro.collect_macro_events()
        self.assertEqual(stats["fetched"], 1)
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(events[0]["alert_decision"], "ALERT")
        self.assertEqual(events[0]["impact_score"], 90)
        self.assertEqual(events[0]["ai_confidence"], 80)
        self.ai.assert_called_once()
        self.send.assert_not_called()
        self.near.assert_not_called()
        self.assertTrue(all(key.startswith("mias:macro:") for _, key in self.redis.calls))
        self.assertFalse(any(":delivered:" in key for key in self.redis.store))
        self.assertEqual(self.http.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(self.http.call_args.kwargs["timeout"], (5, 20))

    def test_dry_run_then_delivery_without_reanalysis(self):
        macro.collect_macro_events(send_alerts=False)
        events, stats = macro.collect_macro_events(send_alerts=True)
        self.assertEqual(events, [])
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["delivered"], 1)
        self.ai.assert_called_once()
        self.send.assert_called_once()
        self.assertIn("Ticker         : N/A", self.send.call_args.args[0])
        self.assertIn("Market Scope   : US macro", self.send.call_args.args[0])
        macro.collect_macro_events(send_alerts=True)
        self.send.assert_called_once()

    def test_failed_delivery_retry_and_negative_ack(self):
        self.send.side_effect = [RuntimeError("private token"), {"ok": False}, {"ok": True}]
        with self.assertLogs("macro_collector", level="ERROR") as logs:
            _, stats = macro.collect_macro_events(send_alerts=True)
        self.assertEqual(stats["delivery_errors"], 1)
        self.assertNotIn("private token", str(logs.output))
        with self.assertLogs("macro_collector", level="ERROR"):
            macro.collect_macro_events(send_alerts=True)
        self.assertFalse(any(":delivered:" in key for key in self.redis.store))
        macro.collect_macro_events(send_alerts=True)
        macro.collect_macro_events(send_alerts=True)
        self.assertEqual(self.send.call_count, 3)
        self.ai.assert_called_once()

    def test_stale_future_and_unknown_dates_skip_before_state(self):
        for now, html, reason in (
            (self.now + timedelta(days=3), pipeline_fixture("cpi"), "stale"),
            (self.now - timedelta(days=1), pipeline_fixture("cpi"), "future"),
            (self.now, pipeline_fixture("cpi").replace("Fri, 11 Sep 2026 08:30:00 -0400", ""), "missing_date"),
        ):
            with self.subTest(reason=reason):
                self.now = now
                self.http.return_value = Response(html)
                with self.assertLogs("macro_collector", level="INFO"):
                    events, stats = macro.collect_macro_events(send_alerts=True)
                self.assertEqual(events, [])
                self.assertEqual(stats[reason], 1)
        self.assertEqual(self.redis.calls, [])
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_freshness_exact_boundary_and_override(self):
        self.now = datetime(2026, 9, 13, 12, 30, tzinfo=timezone.utc)
        events, _ = macro.collect_macro_events(enable_ai=False)
        self.assertEqual(len(events), 1)
        self.now += timedelta(seconds=1)
        with self.assertLogs("macro_collector"):
            self.assertEqual(macro.collect_macro_events()[1]["stale"], 1)
        with patch.object(macro, "MACRO_MAX_AGE_HOURS", 72):
            self.assertEqual(macro.collect_macro_events()[1]["duplicates"], 1)

    def test_cached_retry_stops_when_stale(self):
        self.send.side_effect = RuntimeError("offline")
        with self.assertLogs("macro_collector", level="ERROR"):
            macro.collect_macro_events(send_alerts=True)
        self.now += timedelta(hours=49)
        with self.assertLogs("macro_collector"):
            _, stats = macro.collect_macro_events(send_alerts=True)
        self.assertEqual(stats["stale"], 1)
        self.send.assert_called_once()

    def test_short_dedup_ttl_does_not_repeat_delivery(self):
        with patch.object(macro, "DEDUP_TTL_SECONDS", 1):
            macro.collect_macro_events(send_alerts=True)
        self.now += timedelta(hours=25)
        self.assertEqual(macro.collect_macro_events(send_alerts=True)[1]["duplicates"], 1)
        self.send.assert_called_once()

    def test_ai_disabled_and_non_alert_skip_analysis(self):
        macro.collect_macro_events(enable_ai=False)
        self.redis.store.clear()
        with patch("alert_engine.decision_engine.ALERT_THRESHOLD", 95):
            events, _ = macro.collect_macro_events(send_alerts=True)
        self.assertEqual(events[0]["alert_decision"], "DISPLAY_ONLY")
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_opinion_label_cannot_penalize_official_statistics(self):
        self.ai.side_effect = lambda e: dict(self.enrich(e), ai_event_type="prediction article", impact_score=0)
        events, _ = macro.collect_macro_events(send_alerts=True)
        self.assertEqual(events[0]["impact_score"], 90)
        self.assertEqual(events[0]["original_impact_score"], 90)
        self.assertEqual(events[0]["quality_adjustment"], 0)
        self.assertEqual(events[0]["alert_decision"], "ALERT")
        self.send.assert_called_once()

    def test_ai_failure_and_invalid_response_preserve_deterministic_event(self):
        def fail(event):
            event["score_reasons"].append("partial mutation")
            raise RuntimeError("private detail")
        for action in (fail, lambda e: dict(self.enrich(e), ai_confidence=150)):
            self.redis.store.clear()
            self.ai.side_effect = action
            with self.assertLogs("macro_collector", level="ERROR") as logs:
                events, stats = macro.collect_macro_events()
            self.assertEqual(stats["analysis_errors"], 1)
            self.assertEqual(events[0]["impact_score"], 90)
            self.assertNotIn("ai_summary", events[0])
            self.assertNotIn("partial mutation", events[0]["score_reasons"])
            self.assertNotIn("private detail", str(logs.output))

    def test_freshness_rechecked_after_slow_analysis(self):
        def slow(event):
            self.now += timedelta(hours=49)
            return self.enrich(event)
        self.ai.side_effect = slow
        with self.assertLogs("macro_collector", level="ERROR"):
            _, stats = macro.collect_macro_events(send_alerts=True)
        self.assertEqual(stats["state_errors"], 1)
        self.send.assert_not_called()

    def test_redis_outage_fails_closed_and_does_not_analyze_or_send(self):
        with patch.object(self.redis, "get", side_effect=macro.deduplicator.redis.RedisError("secret")), \
                self.assertLogs("macro_collector", level="ERROR") as logs:
            events, stats = macro.collect_macro_events(send_alerts=True)
        self.assertEqual(events, [])
        self.assertEqual(stats["state_errors"], 1)
        self.assertNotIn("secret", str(logs.output))
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_cache_write_failure_releases_claim_for_retry(self):
        original_set = self.redis.set
        def fail_cache(key, *args, **kwargs):
            if ":processed:" in key:
                raise macro.deduplicator.redis.RedisError("offline")
            return original_set(key, *args, **kwargs)
        with patch.object(self.redis, "set", side_effect=fail_cache), self.assertLogs("macro_collector", level="ERROR"):
            macro.collect_macro_events(send_alerts=True)
        self.send.assert_not_called()
        self.assertFalse(any(":event:" in k for k in self.redis.store))
        self.assertEqual(macro.collect_macro_events(send_alerts=True)[1]["delivered"], 1)

    def test_existing_processing_lease_and_expiry(self):
        key = macro.state_key(pipeline_event("cpi"), "event")
        self.redis.set(key, "another-worker", ex=20)
        self.assertEqual(macro.collect_macro_events()[1]["duplicates"], 1)
        self.ai.assert_not_called()
        self.now += timedelta(seconds=21)
        self.assertEqual(macro.collect_macro_events()[1]["processed"], 1)

    def test_expired_processing_owner_cannot_overwrite_new_worker_cache(self):
        event = pipeline_event("cpi")
        lease = macro.state_key(event, "event")
        cache = macro.state_key(event, "processed")
        def replaced_owner(event):
            self.redis.set(lease, "new-worker", ex=100)
            self.redis.set(cache, "new-worker-result", ex=100)
            return self.enrich(event)
        self.ai.side_effect = replaced_owner
        with self.assertLogs("macro_collector", level="ERROR"):
            _, stats = macro.collect_macro_events(send_alerts=True)
        self.assertEqual(stats["state_errors"], 1)
        self.assertEqual(self.redis.get(cache), "new-worker-result")
        self.assertEqual(self.redis.get(lease), "new-worker")
        self.send.assert_not_called()

    def test_delivery_lease_prevents_concurrent_send_and_old_owner_delete(self):
        macro.collect_macro_events(enable_ai=False)
        key = macro.state_key(pipeline_event("cpi"), "event") + ":delivery"
        self.redis.set(key, "other-worker", ex=20)
        macro.collect_macro_events(send_alerts=True)
        self.send.assert_not_called()
        macro._release(key, "old-worker")
        self.assertEqual(self.redis.get(key), "other-worker")
        self.now += timedelta(seconds=21)
        macro.collect_macro_events(send_alerts=True)
        self.send.assert_called_once()

    def test_bad_cache_does_not_deliver(self):
        key = macro.state_key(pipeline_event("cpi"), "processed")
        self.redis.set(key, '{"event_id":"wrong"}', ex=100)
        with self.assertLogs("macro_collector", level="ERROR"):
            self.assertEqual(macro.collect_macro_events(send_alerts=True)[1]["state_errors"], 1)
        self.send.assert_not_called()

    def test_http_failure_isolated_to_one_source(self):
        self.http.side_effect = [macro.requests.Timeout("private detail"), Response(pipeline_fixture("ppi"))]
        with patch.object(macro, "MACRO_SOURCES", (SOURCES["cpi"], SOURCES["ppi"])), \
                self.assertLogs("macro_collector", level="ERROR") as logs:
            events, stats = macro.collect_macro_events(enable_ai=False)
        self.assertEqual(stats["fetch_errors"], 1)
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(events[0]["release_category"], "ppi")
        self.assertNotIn("private detail", str(logs.output))

    def test_http_redirect_non_html_and_oversized_rejected(self):
        for status, headers, content in ((302, {"Content-Type": "text/html"}, "redirect"),
                                         (200, {"Content-Type": "application/json"}, "{}"),
                                         (200, {"Content-Type": "text/html"}, "a" * 101)):
            response = Response(content)
            response.status_code, response.headers = status, headers
            self.http.return_value = response
            with patch.object(macro, "MAX_DOCUMENT_BYTES", 100), self.assertLogs("macro_collector", level="ERROR"):
                self.assertEqual(macro.collect_macro_events()[1]["fetch_errors"], 1)
        self.ai.assert_not_called()

    def test_schema_error_has_explicit_counter(self):
        self.http.return_value = Response("<rss>New unsupported layout</rss>")
        with self.assertLogs("macro_collector", level="WARNING"):
            events, stats = macro.collect_macro_events()
        self.assertEqual(stats["invalid"], 1)
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(events, [])

    def test_each_source_end_to_end_with_mocked_http(self):
        for category, source in SOURCES.items():
            with self.subTest(category=category):
                self.redis.store.clear()
                self.now = datetime.fromisoformat(pipeline_event(category)["published_at"]) + timedelta(hours=1)
                self.http.side_effect = ([Response(pipeline_fixture(category + "_index")), Response(pipeline_fixture(category))]
                                         if source["agency"] == "bea" else [Response(pipeline_fixture(category))])
                with patch.object(macro, "MACRO_SOURCES", (source,)):
                    events, stats = macro.collect_macro_events(send_alerts=True)
                self.assertEqual(stats["processed"], 1)
                self.assertEqual(stats["delivered"], 1)
                self.assertEqual(events[0]["release_category"], category)

    def test_multi_source_opt_in_preserves_defaults(self):
        counts = dict(fetched=1, relevant=1, duplicates=0, processed=1)
        with patch.object(multi, "read_feed", return_value=([{"source": "news"}], counts)), \
                patch.object(macro, "collect_macro_events", return_value=([{"source": "macro"}], counts)) as collect:
            multi.collect_all_sources()
            collect.assert_not_called()
            events, stats = multi.collect_all_sources(include_macro=True, macro_enable_ai=False)
            collect.assert_called_once_with(enable_ai=False, send_alerts=False)
            self.assertEqual(stats["processed"], len(multi.RSS_SOURCES) + 1)
            self.assertEqual(events[-1]["source"], "macro")

    def test_macro_config_defaults_override_and_invalid(self):
        with patch("dotenv.load_dotenv") as dotenv, patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runpy.run_path("shared/config.py")["MACRO_MAX_AGE_HOURS"], 48)
            with patch.dict(os.environ, {"MACRO_MAX_AGE_HOURS": "12"}):
                self.assertEqual(runpy.run_path("shared/config.py")["MACRO_MAX_AGE_HOURS"], 12)
            with patch.dict(os.environ, {"MACRO_MAX_AGE_HOURS": "0"}), self.assertRaises(ValueError):
                runpy.run_path("shared/config.py")
            self.assertEqual(dotenv.call_count, 3)


class BLSMachineSourceTests(unittest.TestCase):
    def test_captured_public_api_values_with_synthetic_release_metadata(self):
        payload = json.loads((FIXTURES / "bls_api.json").read_text())
        for category in bls.SERIES:
            event = bls.enrich_bls_event(pipeline_event(category), payload)
            self.assertEqual(event["reference_period"], "2026-08")
            self.assertEqual(event["event_id"], pipeline_event(category)["event_id"])
        cpi = bls.extract_bls_metrics(payload, "cpi", "2026-08")
        self.assertEqual(cpi["headline_cpi_sa"]["value"], 334.131)
        self.assertEqual(cpi["headline_cpi_nsa"]["yoy_percent"], 3.3965)
        ppi = bls.enrich_bls_event(pipeline_event("ppi"), payload)
        self.assertEqual(ppi["metrics"]["final_demand_ppi_sa"]["mom_percent"], 0.3999)
        self.assertIn("Preliminary", ppi["summary"])
        employment = bls.extract_bls_metrics(payload, "employment", "2026-08")
        self.assertEqual(employment["payrolls"]["monthly_change_persons"], 162000)
        self.assertEqual(employment["unemployment_rate"]["value"], 4.1)
        self.assertEqual(employment["average_hourly_earnings"]["value"], 37.75)

    def setUp(self):
        self.now = datetime(2026, 9, 11, 13, tzinfo=timezone.utc)
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("Live HTTP forbidden")))
        self.http = self.enterContext(patch.object(bls.requests, "get", return_value=Response(pipeline_fixture("cpi"))))
        self.post = self.enterContext(patch.object(bls.requests, "post", side_effect=self.api_response))
        self.redis = MemoryRedis(lambda: self.now)
        self.enterContext(patch.object(macro.deduplicator, "redis_client", self.redis))
        self.enterContext(patch.object(macro, "MACRO_SOURCES", (SOURCES["cpi"],)))
        clock = self.enterContext(patch.object(macro, "datetime", wraps=datetime))
        clock.now.side_effect = lambda tz: self.now
        self.ai = self.enterContext(patch.object(macro, "analyze_macro_event", side_effect=AssertionError("AI forbidden")))
        self.send = self.enterContext(patch.object(macro, "deliver_macro_alert", return_value={"ok": True}))

    @staticmethod
    def payload():
        series = []
        for category in bls.SERIES.values():
            for name, series_id, units, adjustment in category:
                values = ("159075", "158913", "157000") if name == "payrolls" else (
                    ("4.1", "4.0", "3.9") if name == "unemployment_rate" else (
                    ("37.75", "37.65", "36.50") if name == "average_hourly_earnings" else ("110", "100", "100")))
                rows = [dict(year=y, period=m, value=v, footnotes=[{"code": "P", "text": "preliminary"}])
                        for y, m, v in zip(("2026", "2026", "2025"), ("M08", "M07", "M08"), values)]
                series.append({"seriesID": series_id, "data": rows})
        return {"status": "REQUEST_SUCCEEDED", "message": [], "Results": {"series": series}}

    def api_response(self, *args, **kwargs):
        response = Response(json.dumps(self.payload()))
        response.headers = {"Content-Type": "text/plain;charset=ISO-8859-1"}
        return response

    def test_three_categories_full_rss_api_pipeline(self):
        for category in bls.SERIES:
            self.redis.store.clear()
            self.http.return_value = Response(pipeline_fixture(category))
            self.now = datetime.fromisoformat(pipeline_event(category)["published_at"]) + timedelta(hours=1)
            with patch.object(macro, "MACRO_SOURCES", (SOURCES[category],)):
                events, stats = macro.collect_macro_events(enable_ai=False)
            self.assertEqual(stats["processed"], 1)
            event = events[0]
            self.assertEqual(event["reference_period"], "2026-08")
            self.assertEqual(event["release_category"], category)
            self.assertEqual(event["quality_adjustment"], 0)
            self.assertEqual(event["alert_decision"], "ALERT")
            self.assertEqual(event["data_source_url"], bls.API_URL)
            self.assertEqual(set(event["metrics"]), {s[0] for s in bls.SERIES[category]})
            self.assertEqual(self.http.call_args.args[0], SOURCES[category]["url"])
            self.assertEqual(self.post.call_args.args[0], bls.API_URL)
            self.assertEqual(self.post.call_args.kwargs["json"]["seriesid"], [s[1] for s in bls.SERIES[category]])
            self.assertNotIn("registrationkey", self.post.call_args.kwargs["json"])
            self.assertFalse(self.post.call_args.kwargs["allow_redirects"])
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_metric_units_changes_and_footnotes(self):
        metrics = bls.extract_bls_metrics(self.payload(), "employment", "2026-08")
        self.assertEqual(metrics["payrolls"]["monthly_change_persons"], 162000)
        self.assertEqual(metrics["unemployment_rate"]["monthly_change_percentage_points"], 0.1)
        self.assertEqual(metrics["average_hourly_earnings"]["units"], "USD/hour")
        self.assertEqual(metrics["payrolls"]["footnotes"][0]["code"], "P")
        cpi = bls.extract_bls_metrics(self.payload(), "cpi", "2026-08")
        self.assertEqual(cpi["headline_cpi_sa"]["mom_percent"], 10)
        self.assertEqual(cpi["headline_cpi_nsa"]["yoy_percent"], 10)
        self.assertEqual(cpi["headline_cpi_nsa"]["comparison_period"], "2025-08")

    def test_missing_current_or_comparison_or_series_is_retryable(self):
        for mutate in (lambda d: d["Results"]["series"].pop(0),
                       lambda d: d["Results"]["series"][0]["data"].pop(0),
                       lambda d: d["Results"]["series"][0]["data"].pop(1)):
            payload = self.payload()
            mutate(payload)
            with self.assertRaises(bls.BLSDataError):
                bls.extract_bls_metrics(payload, "cpi", "2026-08")

    def test_annual_averages_unordered_and_later_observations_not_substituted(self):
        payload = self.payload()
        payload["Results"]["series"][0]["data"].extend([
            {"year": "2026", "period": "M13", "value": "9999"},
            {"year": "2026", "period": "M09", "value": "9999"}])
        payload["Results"]["series"][0]["data"].reverse()
        self.assertEqual(bls.extract_bls_metrics(payload, "cpi", "2026-08")["headline_cpi_sa"]["value"], 110)

    def test_bad_numeric_values_status_warnings_duplicates(self):
        for value in ("-", "NaN", "Infinity", "-1"):
            payload = self.payload()
            payload["Results"]["series"][0]["data"][0]["value"] = value
            with self.assertRaises(bls.BLSDataError):
                bls.extract_bls_metrics(payload, "cpi", "2026-08")
        for mutate in (lambda d: d.update(status="REQUEST_NOT_PROCESSED"),
                       lambda d: d.update(message=["Daily quota exceeded"]),
                       lambda d: d["Results"]["series"].append(d["Results"]["series"][0]),
                       lambda d: d["Results"]["series"][0]["data"].append(d["Results"]["series"][0]["data"][0])):
            payload = self.payload()
            mutate(payload)
            with self.assertRaises(bls.BLSDataError):
                bls.extract_bls_metrics(payload, "cpi", "2026-08")

    def test_api_failure_no_processing_and_retry_after_cooldown(self):
        self.post.side_effect = macro.requests.Timeout("private detail")
        with self.assertLogs("macro_collector", level="WARNING") as logs:
            events, stats = macro.collect_macro_events(enable_ai=False, send_alerts=True)
        self.assertEqual(stats["data_errors"], 1)
        self.assertEqual(events, [])
        self.assertNotIn("private detail", str(logs.output))
        self.assertFalse(any(":processed:" in k or ":delivered:" in k for k in self.redis.store))
        self.send.assert_not_called()
        self.post.side_effect = self.api_response
        with self.assertLogs("macro_collector", level="WARNING"):
            macro.collect_macro_events(enable_ai=False)
        self.post.assert_called_once()
        self.now += timedelta(seconds=macro.BLS_RETRY_SECONDS)
        events, stats = macro.collect_macro_events(enable_ai=False)
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(self.post.call_count, 2)

    def test_cached_dry_run_and_delivery_retry_do_not_repeat_api(self):
        macro.collect_macro_events(enable_ai=False)
        self.send.return_value = {"ok": False}
        with self.assertLogs("macro_collector", level="ERROR"):
            macro.collect_macro_events(enable_ai=False, send_alerts=True)
        self.send.return_value = {"ok": True}
        self.assertEqual(macro.collect_macro_events(enable_ai=False, send_alerts=True)[1]["delivered"], 1)
        self.assertEqual(macro.collect_macro_events(enable_ai=False, send_alerts=True)[1]["delivered"], 0)
        self.post.assert_called_once()
        self.assertEqual(self.send.call_count, 2)

    def test_stale_future_missing_dates_skip_api_and_redis(self):
        for change, now, reason in ((lambda x: x, self.now + timedelta(days=3), "stale"),
                                    (lambda x: x, self.now - timedelta(days=1), "future"),
                                    (lambda x: x.replace("Fri, 11 Sep 2026 08:30:00 -0400", ""), self.now, "missing_date")):
            self.now = now
            self.http.return_value = Response(change(pipeline_fixture("cpi")))
            self.assertEqual(macro.collect_macro_events(enable_ai=False)[1][reason], 1)
        self.assertEqual(self.redis.calls, [])
        self.post.assert_not_called()
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_redis_failure_prevents_api_and_ai(self):
        with patch.object(self.redis, "get", side_effect=macro.deduplicator.redis.RedisError("private")), self.assertLogs("macro_collector"):
            self.assertEqual(macro.collect_macro_events()[1]["state_errors"], 1)
        self.post.assert_not_called()
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_identity_month_url_cosmetic_numeric_revisions_and_correction(self):
        base = pipeline_event("cpi")
        xml = pipeline_fixture("cpi")
        changed = xml.replace("cpi.htm", "archives/cpi_09112026.htm").replace("Consumer Price Index -", "CPI rises in")
        self.assertEqual(bls.normalize_bls_feed(changed, SOURCES["cpi"])["event_id"], base["event_id"])
        updated = bls.normalize_bls_feed(xml.replace("August 2026", "September 2026"), SOURCES["cpi"])
        self.assertNotEqual(updated["event_id"], base["event_id"])
        corrected = bls.normalize_bls_feed(xml.replace("<title>Consumer", "<title>Correction: Consumer"), SOURCES["cpi"])
        self.assertNotEqual(corrected["event_id"], base["event_id"])
        self.assertEqual(corrected["revision_id"], corrected["published_at"])
        payload = self.payload()
        first = bls.enrich_bls_event(base, payload)
        payload["Results"]["series"][0]["data"][1]["value"] = "101"
        second = bls.enrich_bls_event(base, payload)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertNotEqual(first["metrics"], second["metrics"])

    def test_month_only_year_rollover_and_no_fetch_time_inference(self):
        xml = pipeline_fixture("cpi").replace("August 2026", "December").replace("Fri, 11 Sep 2026 08:30:00 -0400", "Tue, 13 Jan 2026 08:30:00 -0500")
        event = bls.normalize_bls_feed(xml, SOURCES["cpi"])
        self.assertEqual(event["reference_period"], "2025-12")
        self.assertEqual(event["published_at"], "2026-01-13T13:30:00+00:00")
        with self.assertRaises(ValueError):
            bls.normalize_bls_feed(xml.replace("Tue, 13 Jan 2026 08:30:00 -0500", ""), SOURCES["cpi"])

    def test_feed_order_does_not_choose_old_month_or_hide_missing_date(self):
        from xml.etree import ElementTree
        xml = pipeline_fixture("cpi")
        root = ElementTree.fromstring(xml)
        old = ElementTree.fromstring(xml.replace("August 2026", "July 2026")).find("./channel/item")
        root.find("channel").append(old)
        self.assertEqual(bls.normalize_bls_feed(ElementTree.tostring(root, encoding="unicode"), SOURCES["cpi"])["reference_period"], "2026-08")
        root.find("./channel/item/pubDate").text = ""
        event = bls.normalize_bls_feed(ElementTree.tostring(root, encoding="unicode"), SOURCES["cpi"])
        self.assertEqual(event["reference_period"], "2026-08")
        self.assertIsNone(event["published_at"])

    def test_malformed_rss_untrusted_links_dates_and_entities(self):
        xml = pipeline_fixture("cpi")
        for changed in ("<rss>", xml.replace("www.bls.gov/news", "evil.example/news"),
                        xml.replace("cpi.htm", "ppi.htm"),
                        xml.replace("08:30:00 -0400", "08:30:00"),
                        '<!DOCTYPE rss [<!ENTITY bad "bad">]>' + xml):
            with self.assertRaises(ValueError):
                bls.normalize_bls_feed(changed, SOURCES["cpi"])

    def test_http_rejections_and_no_html_fallback(self):
        for status, mime in ((403, "text/html"), (302, "application/rss+xml"), (200, "text/html")):
            response = Response(pipeline_fixture("cpi"))
            response.status_code, response.headers = status, {"Content-Type": mime}
            self.http.return_value = response
            with self.assertLogs("macro_collector", level="ERROR"):
                self.assertEqual(macro.collect_macro_events(enable_ai=False)[1]["fetch_errors"], 1)
        for call in self.http.call_args_list:
            self.assertEqual(call.args[0], bls.RSS_URLS["cpi"])
        self.post.assert_not_called()
        with self.assertRaises(ValueError):
            macro.fetch_document("https://www.bls.gov/news.release/cpi.htm", "bls")

    def test_api_mime_and_size_rejected(self):
        for status, mime, body in ((302, "application/json", "{}"), (200, "text/html", "<html>denied</html>"),
                                   (200, "text/plain", "not json"), (200, "application/json", "x" * 101)):
            response = Response(body)
            response.status_code, response.headers = status, {"Content-Type": mime}
            self.post.side_effect = None
            self.post.return_value = response
            with patch.object(bls, "MAX_BYTES", 100), self.assertRaises(bls.BLSDataError):
                bls.fetch_bls_data("cpi", "2026-08")


if __name__ == "__main__":
    unittest.main()
