"""Isolated official-policy regression tests; no network or dotenv loading."""

import copy
import json
import os
import runpy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "ALERT_THRESHOLD": "70", "DISPLAY_THRESHOLD": "40", "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379", "GEOPOLITICAL_MAX_AGE_HOURS": "48", "GEOPOLITICAL_ALIAS_TTL_DAYS": "365",
}, clear=True):
    from collector import geopolitical_collector as geo
    from collector import geopolitical_sources as sources
    from collector import geopolitical_identity as identity
    from collector.geopolitical_normalizer import normalize_document, official_url, publication
    from analyzer.geopolitical_relevance import detect_relevance
    from analyzer.geopolitical_scoring import score_geopolitical_event
    from collector import multi_source_collector as multi

FIXTURES = json.loads((Path(__file__).parent / "fixtures/geopolitical/actions.json").read_text())
NOW = datetime(2026, 9, 22, 14, tzinfo=timezone.utc)


def document(index=0, **changes):
    d = copy.deepcopy(FIXTURES[index])
    d.update(changes)
    return d


def event(index=0, **changes):
    return detect_relevance(normalize_document(document(index, **changes)))


class MemoryRedis:
    def __init__(self):
        self.data, self.now = {}, NOW

    def get(self, key):
        value, expiry = self.data.get(key, (None, self.now))
        return value if expiry > self.now else None

    def set(self, key, value, *, nx=False, ex):
        if nx and self.get(key) is not None:
            return None
        self.data[key] = (value, self.now + timedelta(seconds=ex))
        return True

    def eval(self, script, n, *args):
        keys, values = args[:n], args[n:]
        if script == identity.RESOLVE:
            candidate, prefix, raw, ttl = values
            roots = {self.get(k) for k in keys if self.get(k)}
            if len(roots) > 1:
                raise geo.deduplicator.redis.RedisError("Conflicting aliases")
            root = next(iter(roots), candidate)
            record = json.loads(raw)
            previous = self.get(prefix + root)
            if previous:
                old = json.loads(previous)
                record["published_at"] = min(record["published_at"], old["published_at"])
                ids = {p["document_id"] for p in record["provenance"]}
                record["provenance"].extend(p for p in old["provenance"] if p["document_id"] not in ids)
            raw = json.dumps(record)
            self.set(prefix + root, raw, ex=int(ttl))
            for key in keys:
                self.set(key, root, ex=int(ttl))
            return root, raw
        if script == geo.RELEASE:
            if self.get(keys[0]) == values[0]:
                self.data.pop(keys[0], None)
                return 1
            return 0
        if script == geo.CACHE:
            token, value, ttl = values
            if self.get(keys[0]) != token:
                return 0
            self.set(keys[1], value, ex=ttl)
            return 1
        raise AssertionError("Unexpected script")


class Response:
    def __init__(self, text, mime="text/html", status=200):
        self.content = text.encode() if isinstance(text, str) else text
        self.headers, self.status_code = {"Content-Type": mime}, status
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def raise_for_status(self):
        if self.status_code >= 400:
            raise sources.requests.HTTPError("failure")
    def iter_content(self, size):
        yield self.content


class NormalizerTests(unittest.TestCase):
    def test_common_schema_and_evidence(self):
        e = event()
        self.assertEqual(e["related_symbols"], ["NVDA"])
        self.assertEqual(e["event_type"], "policy_action")
        evidence = e["evidence"][0]
        self.assertEqual(e["body"][evidence["start"]:evidence["end"]], evidence["quote"])
        self.assertEqual(evidence["matched_jurisdictions"], ["China"])
        self.assertEqual(evidence["rule"], "nvda_advanced_compute_supply_v1")

    def test_meta_direct_and_indirect_rules(self):
        self.assertEqual(event(2)["direct_symbols"], ["META"])
        indirect = event(2, body=FIXTURES[2]["body"].replace("Meta Platforms", "social media platforms"))
        self.assertEqual(indirect["direct_symbols"], [])
        self.assertEqual(indirect["related_symbols"], ["META"])

    def test_generic_keywords_never_suffice(self):
        for word in ("China", "Taiwan", "AI", "chip", "semiconductor", "sanctions", "export controls", "Meta"):
            with self.subTest(word=word):
                self.assertFalse(event(body=f"The agency discusses {word} and reports a meeting about future economic cooperation. No action is announced.")["relevant"])

    def test_scope_without_action_is_irrelevant(self):
        self.assertFalse(event(body="Advanced computing chips are important to China and the United States. Officials discussed export controls at a meeting.")["relevant"])

    def test_action_without_scope_is_irrelevant(self):
        self.assertFalse(event(body="The agency imposes export licensing requirements on agricultural products in China. The final rule covers these agricultural goods.")["relevant"])

    def test_negation_and_hypothetical(self):
        for qualifier in ("does not", "might", "could", "previously"):
            with self.subTest(qualifier=qualifier):
                self.assertFalse(event(body=f"The Department {qualifier} imposes export controls on advanced computing chips in China. This discussion provides background only.")["relevant"])

    def test_speech_is_not_adopted_policy(self):
        self.assertFalse(event(headline="Remarks about export controls")["relevant"])

    def test_family_separation(self):
        for text, family in (
            ("The agency imposes sanctions on NVIDIA. The designation sets out binding obligations for the named organization.", "sanctions_action"),
            ("The agency imposes tariffs on AI accelerators from China. The measure sets out binding obligations for the covered goods.", "trade_action"),
            (FIXTURES[2]["body"], "regulatory_action"),
        ):
            with self.subTest(family=family):
                self.assertEqual(event(body=text)["event_type"], family)

    def test_taiwan_operational_only(self):
        e = event(3)
        self.assertEqual(e["event_type"], "operational_disruption")
        self.assertEqual(score_geopolitical_event(e)["impact_score"], 95)
        self.assertFalse(event(3, body="Taiwan hosts a semiconductor conference about AI chips and the global economy. Officials discuss a possible future supply chain risk.")["relevant"])

    def test_multi_action_notice_withheld(self):
        e = event(body=FIXTURES[0]["body"] + " The agency imposes tariffs on AI accelerators imported from China.")
        self.assertFalse(e["relevant"])

    def test_proposed_and_complaint_scores(self):
        e = score_geopolitical_event(event(1, document_type="Proposed Rule"))
        self.assertEqual(e["impact_score"], 60)
        e = score_geopolitical_event(event(2, body="The Commission files a complaint against Meta Platforms concerning targeted advertising. The allegations concern the company's data practices."))
        self.assertEqual(e["impact_score"], 75)
        self.assertEqual(e["legal_status"], "complaint")

    def test_direction_does_not_change_score(self):
        a, b = event(), event(body=FIXTURES[0]["body"].replace("imposes", "removes"))
        a["ai_sentiment"], b["ai_sentiment"] = "BEARISH", "BULLISH"
        self.assertEqual(score_geopolitical_event(a)["impact_score"], score_geopolitical_event(b)["impact_score"])
        self.assertEqual(a["quality_adjustment"], 0)

    def test_publication_timezones_and_missing(self):
        self.assertEqual(publication("2026-09-22", "moea"), ("2026-09-21T16:00:00+00:00", "date"))
        self.assertEqual(publication(None, "fr"), (None, "unknown"))
        with self.assertRaises(ValueError):
            publication("2026-09-22T12:00:00", "fr")

    def test_url_security_and_article_queries(self):
        for u in ("http://www.bis.gov/x", "https://www.bis.gov.evil/x", "https://u:p@www.bis.gov/x", "https://www.bis.gov:444/x"):
            with self.assertRaises(ValueError):
                official_url(u)
        self.assertIn("news_id=99903", event(3)["url"])

    def test_missing_full_text_rejected(self):
        with self.assertRaises(ValueError):
            event(body="Export controls")


class SourceTests(unittest.TestCase):
    def test_ustr_date_is_bound_to_single_release(self):
        url = 'https://ustr.gov/about/policy-offices/press-office/press-releases/2026/september/test'
        index = f'<ul class="listing"><li>2026-09-22<br><a href="{url}">Policy</a></li></ul>'
        article = '<h1>Policy</h1><div class="field--name-body">' + FIXTURES[0]['body'] + '</div>'
        with patch.object(sources, 'fetch', side_effect=[index.encode(), article.encode()]):
            docs = sources.fetch_documents('ustr')
        self.assertEqual(docs[0]['published_at'], '2026-09-22')

    def test_ofac_body_date_and_native_identity(self):
        html = '<main><h1>Official sanctions action</h1><div class="field--name-field-release-date">Release Date 09/22/2026</div><div class="field--name-field-body">The agency imposes sanctions on NVIDIA. The designation sets out binding obligations for the named organization.</div></main>'
        doc = sources.parse_article(html, 'ofac', 'https://ofac.treasury.gov/recent-actions/20260922')
        e = detect_relevance(normalize_document(doc))
        self.assertEqual(e['event_type'], 'sanctions_action')
        self.assertEqual(e['identity_anchors'], ['ofac:20260922'])
        self.assertEqual(e['published_at'], '2026-09-22T04:00:00+00:00')

    def test_whitehouse_eo_links_join_fr_metadata(self):
        html = '<meta property="article:published_time" content="2026-09-22T12:00:00Z"><main><h1>Technology policy order</h1><div class="entry-content">' + FIXTURES[0]['body'] + '<a href="https://www.whitehouse.gov/wp-content/uploads/2026/09/eo-99999.pdf">Download</a></div></main>'
        doc = sources.parse_article(html, 'whitehouse', 'https://www.whitehouse.gov/presidential-actions/2026/09/test/')
        self.assertEqual(doc['identity_anchors'], ['eo:99999'])
        redis = MemoryRedis()
        wh = identity.resolve_identity(detect_relevance(normalize_document(doc)), redis, 365*86400)
        fr = identity.resolve_identity(event(1, identity_anchors=['eo:99999']), redis, 365*86400)
        self.assertEqual(wh['event_id'], fr['event_id'])

    def test_treasury_manifest_reused_read_only(self):
        html = '<main><h1>Policy</h1><div class="field--name-field-news-body">' + FIXTURES[0]['body'] + '</div><div class="field--name-field-news-publication-date"><time datetime="2026-09-22T12:00:00Z"></time></div></main>'
        url = 'https://home.treasury.gov/news/press-releases/test'
        with patch('collector.treasury_sources.fetch_index_documents', return_value=[{'url':url}]), patch.object(sources, 'fetch', return_value=html.encode()):
            docs = sources.fetch_documents('treasury')
        self.assertEqual(docs[0]['published_at'], '2026-09-22T12:00:00Z')

    def test_bis_live_markup_shape(self):
        html = '<main><h2>Bureau of Industry &amp; Security</h2><h2 class="text-primary-600">Actual policy title</h2><span class="date">September 22, 2026</span><div class="press-release-container">' + FIXTURES[0]["body"] + '</div></main>'
        d = sources.parse_article(html, "bis", FIXTURES[0]["url"])
        self.assertEqual(d["headline"], "Actual policy title")
        self.assertEqual(d["published_at"], "2026-09-22T04:00:00+00:00")

    def test_ftc_case_anchor(self):
        from collector.macro_normalizer import Document
        root = Document('<a href="https://www.ftc.gov/legal-library/browse/cases-proceedings/1234567-example">complaint</a>').root
        self.assertEqual(sources._anchors(root, FIXTURES[2]["url"]), ["ftc-case:1234567"])

    def test_multiple_instrument_links_withheld(self):
        from collector.macro_normalizer import Document
        root = Document(''.join(f'<a href="https://www.federalregister.gov/documents/2026/09/22/{n}/x">this rule</a>' for n in ('2026-99901', '2026-99902'))).root
        self.assertEqual(sources._anchors(root, SOURCES_URL), [])

    def test_config_defaults_and_validation(self):
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, {}, clear=True):
            config = runpy.run_path("shared/config.py")
            self.assertEqual(config["GEOPOLITICAL_MAX_AGE_HOURS"], 48)
            self.assertEqual(config["GEOPOLITICAL_ALIAS_TTL_DAYS"], 365)
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, {"GEOPOLITICAL_ALIAS_TTL_DAYS": "1"}, clear=True), self.assertRaises(ValueError):
            runpy.run_path("shared/config.py")

    def test_ftc_feed_requires_article_content(self):
        xml = '<rss version="2.0"><channel><title>FTC</title><item><title>Policy</title><guid isPermaLink="false">123</guid><link>' + FIXTURES[2]["url"] + '</link><description>Summary only</description></item></channel></rss>'
        with patch.object(sources, "fetch", side_effect=[xml.encode(), sources.requests.HTTPError("403")]):
            docs = sources.fetch_documents("ftc")
        self.assertEqual(docs[0]["source_error"], "HTTPError")
        self.assertNotIn("body", docs[0])

    def test_inspection_underscore_pagination_is_supported(self):
        next_url = 'https://www.federalregister.gov/api/v1/public_inspection_documents?format=json&page=2'
        with patch.object(sources, "fetch", side_effect=[json.dumps({"results": [], "next_page_url": next_url}).encode(), b'{"results": []}']):
            self.assertEqual(list(sources.pages(sources.SOURCES["fr_inspection"])), [])

    def test_http_types_redirects_and_limits(self):
        for response in (Response("x", status=302), Response("x", status=403), Response("x", mime="application/pdf")):
            with patch.object(sources.requests, "get", return_value=response), self.assertRaises((ValueError, sources.requests.HTTPError)):
                sources.fetch(SOURCES_URL, {"text/html"})
        with patch.object(sources.requests, "get", return_value=Response("12345")), patch.object(sources, "MAX_BYTES", 4), self.assertRaises(ValueError):
            sources.fetch(SOURCES_URL, {"text/html"})

    def test_json_next_url_not_total_pages(self):
        first = {"results": [{"id": 1}], "count": 10000, "total_pages": 50,
                 "next_page_url": "https://www.federalregister.gov/api/v1/documents?format=json&page=2"}
        with patch.object(sources, "fetch", side_effect=[json.dumps(first).encode(), b'{"results":[{"id":2}]}']):
            self.assertEqual(list(sources.pages(sources.SOURCES["fr"])), [{"id": 1}, {"id": 2}])

    def test_pagination_rejects_foreign_host_and_cycle(self):
        for target in ("https://evil.example/api", sources.SOURCES["fr"]):
            with patch.object(sources, "fetch", return_value=json.dumps({"results": [], "next_page_url": target}).encode()), self.assertRaises(ValueError):
                list(sources.pages(sources.SOURCES["fr"]))

    def test_fr_detail_uses_full_text_and_separate_dates(self):
        row = dict(document_number="2026-99901", agencies=[{"slug": "industry-and-security-bureau"}],
                   html_url=FIXTURES[1]["url"], raw_text_url="https://www.federalregister.gov/public-inspection/raw_text/202/699/901.txt",
                   title="Advanced computing export controls", filed_at="2026-09-22T12:00:00Z", publication_date="2026-09-23", type="Rule")
        with patch.object(sources, "pages", return_value=iter([row])), patch.object(sources, "fetch", return_value=FIXTURES[1]["body"].encode()):
            d = sources.fetch_documents("fr_inspection")[0]
        self.assertEqual(d["published_at"], row["filed_at"])
        self.assertEqual(d["scheduled_publication"], "2026-09-23")

    def test_rss_moea_native_id_and_item_date(self):
        xml = f'<rss version="2.0"><channel><title>MOEA</title><pubDate>Wed, 23 Sep 2026 12:00:00 GMT</pubDate><item><title>Supply announcement</title><link>{FIXTURES[3]["url"].replace("&", "&amp;")}</link><pubDate>Tue, 22 Sep 2026 12:00:00 GMT</pubDate><description>{FIXTURES[3]["body"]}</description></item></channel></rss>'
        with patch.object(sources, "fetch", return_value=xml.encode()):
            d = sources.fetch_documents("moea")[0]
        self.assertEqual(d["native_id"], "99903")
        self.assertEqual(d["published_at"], "Tue, 22 Sep 2026 12:00:00 GMT")

    def test_rss_rejects_entities(self):
        with patch.object(sources, "fetch", return_value=b'<!DOCTYPE x><rss/>'), self.assertRaises(ValueError):
            sources.fetch_documents("ftc")

    def test_article_body_not_footer(self):
        html = '<main><h1>New policy</h1><div class="field--name-body">' + FIXTURES[0]["body"] + '</div></main><footer>September 22, 2026</footer>'
        d = sources.parse_article(html, "ftc", FIXTURES[2]["url"])
        self.assertIsNone(d["published_at"])
        self.assertNotIn("September", d["body"])

    def test_unavailable_body_fails_closed(self):
        with self.assertRaises(ValueError):
            sources.parse_article('<h1>Policy</h1><footer>New export rule</footer>', "bis", SOURCES_URL)

    def test_current_instrument_links_only(self):
        url = FIXTURES[1]["url"]
        from collector.macro_normalizer import Document
        for label, expected in (("this final rule", ["fr:2026-99901"]), ("this rule", ["fr:2026-99901"]), ("previous rule", [])):
            root = Document(f'<p><a href="{url}">{label}</a></p>').root
            self.assertEqual(sources._anchors(root, SOURCES_URL), expected)


SOURCES_URL = "https://www.bis.gov/news-updates"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.redis = MemoryRedis()
        self.docs = [document()]
        patches = [patch.object(geo.deduplicator, "redis_client", self.redis),
                   patch.object(sources, "SOURCES", {"bis": SOURCES_URL}),
                   patch.object(sources, "fetch_documents", side_effect=lambda _: copy.deepcopy(self.docs)),
                   patch.object(geo, "analyze_geopolitical_event", side_effect=self.ai),
                   patch.object(geo, "deliver_geopolitical_alert", return_value={"ok": True, "result": {"message_id": 123}}),
                   patch.object(geo, "datetime")]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.ai_mock, self.telegram, self.clock = self.mocks[3:]
        self.clock.now.side_effect = lambda *a: self.redis.now
        self.clock.fromisoformat = datetime.fromisoformat

    @staticmethod
    def ai(e):
        e.update(ai_summary="Official action summarized.", ai_why_it_matters="Changes covered obligations.",
                 ai_event_type="opinion", ai_sentiment="NEUTRAL", ai_confidence=90,
                 impact_score=1, policy_stage="fake", metrics={"fake": 1}, symbols=["FAKE"])
        return e

    def test_default_no_delivery_and_parser_fields_immutable(self):
        events, stats = geo.collect_geopolitical_events()
        e = events[0]
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(e["impact_score"], 90)
        self.assertEqual(e["quality_adjustment"], 0)
        self.assertEqual(e["policy_stage"], "adopted")
        self.assertEqual(e["metrics"], {})
        self.assertEqual(e["symbols"], ["NVDA"])
        self.telegram.assert_not_called()

    def test_same_policy_two_sources_one_analysis_delivery(self):
        self.docs.append(document(1))
        events, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual((len(events), stats["duplicates"], stats["delivered"]), (1, 1, 1))
        self.ai_mock.assert_called_once()
        self.telegram.assert_called_once()
        records = [json.loads(v[0]) for k, v in self.redis.data.items() if k.startswith(identity.PREFIX + "policy:")]
        self.assertEqual(len(records[0]["provenance"]), 2)

    def test_dry_run_then_delivery(self):
        geo.collect_geopolitical_events()
        _, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(stats["delivered"], 1)
        self.ai_mock.assert_called_once()

    def test_failed_delivery_retry(self):
        self.telegram.return_value = {"ok": False}
        _, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(stats["delivery_errors"], 1)
        self.assertFalse(any(":delivered:" in k for k in self.redis.data))
        self.telegram.return_value = {"ok": True, "result": {"message_id": 123}}
        _, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(stats["delivered"], 1)
        self.ai_mock.assert_called_once()

    def test_redis_failure_prevents_ai_and_telegram(self):
        with patch.object(self.redis, "eval", side_effect=geo.deduplicator.redis.RedisError("private")):
            events, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual((events, stats["state_errors"]), ([], 1))
        self.ai_mock.assert_not_called()
        self.telegram.assert_not_called()

    def test_stale_future_missing_no_state(self):
        self.docs = [document(published_at=x) for x in ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z", None)]
        _, stats = geo.collect_geopolitical_events()
        self.assertEqual([stats[k] for k in ("stale", "future", "missing_date")], [1, 1, 1])
        self.assertEqual(self.redis.data, {})

    def test_exact_freshness_boundary(self):
        e = event()
        e["published_at"] = (NOW - timedelta(hours=48)).isoformat()
        self.assertIsNone(geo.freshness(e, NOW))
        self.assertEqual(geo.freshness(e, NOW + timedelta(microseconds=1)), "stale")

    def test_unresolved_identity_no_ai_or_delivery(self):
        self.docs = [document(identity_anchors=[])]
        events, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(events[0]["alert_decision"], "DISPLAY_ONLY")
        self.assertEqual(stats["unresolved"], 1)
        self.ai_mock.assert_not_called()
        self.telegram.assert_not_called()

    def test_ai_disabled_and_proposal_bypass(self):
        geo.collect_geopolitical_events(enable_ai=False)
        self.docs = [document(1, document_type="Proposed Rule")]
        events, _ = geo.collect_geopolitical_events()
        self.assertEqual(events[0]["alert_decision"], "DISPLAY_ONLY")
        self.ai_mock.assert_not_called()

    def test_ai_failure_keeps_deterministic_alert(self):
        self.ai_mock.side_effect = RuntimeError("private")
        events, stats = geo.collect_geopolitical_events()
        self.assertEqual(stats["analysis_errors"], 1)
        self.assertEqual(events[0]["impact_score"], 90)

    def test_invalid_ai_enrichment_not_partially_copied(self):
        def invalid(e):
            e = self.ai(e)
            e["ai_confidence"] = True
            return e
        self.ai_mock.side_effect = invalid
        events, stats = geo.collect_geopolitical_events()
        self.assertEqual(stats["analysis_errors"], 1)
        self.assertNotIn("ai_summary", events[0])

    def test_conflicting_roots_withheld(self):
        first, _ = geo.collect_geopolitical_events()
        self.docs = [document(identity_anchors=["fr:2026-99902"], native_id="other")]
        second, _ = geo.collect_geopolitical_events()
        self.assertNotEqual(first[0]["event_id"], second[0]["event_id"])
        self.docs = [document(identity_anchors=["fr:2026-99901", "fr:2026-99902"], native_id="bridge")]
        _, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(stats["state_errors"], 1)
        self.telegram.assert_not_called()

    def test_partial_article_failure_does_not_drop_source(self):
        self.docs.insert(0, {"source_error": "HTTPError"})
        events, stats = geo.collect_geopolitical_events()
        self.assertEqual((len(events), stats["fetch_errors"]), (1, 1))

    def test_delivery_read_failure_no_send(self):
        events, _ = geo.collect_geopolitical_events()
        original = self.redis.get
        def fail(key):
            if ':delivered:' in key:
                raise geo.deduplicator.redis.RedisError('outage')
            return original(key)
        with patch.object(self.redis, 'get', side_effect=fail):
            _, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(stats['state_errors'], 1)
        self.telegram.assert_not_called()

    def test_delivered_retained_past_short_processing_ttl(self):
        with patch.object(geo, 'DEDUP_TTL_SECONDS', 1):
            geo.collect_geopolitical_events(send_alerts=True)
            self.redis.now += timedelta(seconds=2)
            _, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(stats['duplicates'], 1)
        self.telegram.assert_called_once()

    def test_reused_url_new_fr_instrument_distinct(self):
        first, _ = geo.collect_geopolitical_events()
        # Explicit new instrument on a reused native URL has a separate action identity.
        self.docs = [document(identity_anchors=['fr:2026-99999'])]
        second, stats = geo.collect_geopolitical_events()
        self.assertEqual(stats['duplicates'], 0)
        self.assertNotEqual(second[0]['event_id'], first[0]['event_id'])

    def test_processing_cache_failure_retryable(self):
        original = self.redis.eval
        def fail(script, *args):
            if script == geo.CACHE:
                raise geo.deduplicator.redis.RedisError("write failed")
            return original(script, *args)
        with patch.object(self.redis, "eval", side_effect=fail):
            _, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(stats["state_errors"], 1)
        self.telegram.assert_not_called()
        _, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(stats["delivered"], 1)

    def test_expired_owner_cannot_cache_or_release(self):
        self.redis.set("lease", "new", ex=60)
        self.assertEqual(self.redis.eval(geo.CACHE, 2, "lease", "cache", "old", "value", 60), 0)
        self.assertEqual(self.redis.eval(geo.RELEASE, 1, "lease", "old"), 0)
        self.assertEqual(self.redis.get("lease"), "new")

    def test_freshness_after_analysis(self):
        def slow(e):
            self.redis.now += timedelta(days=3)
            return self.ai(e)
        self.ai_mock.side_effect = slow
        geo.collect_geopolitical_events(send_alerts=True)
        self.telegram.assert_not_called()

    def test_old_policy_new_companion_cannot_refresh(self):
        geo.collect_geopolitical_events()
        self.redis.now += timedelta(days=3)
        self.docs = [document(1, published_at=self.redis.now.isoformat())]
        _, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(stats["stale"], 1)
        self.telegram.assert_not_called()

    def test_stage_change_distinct_and_cosmetic_same(self):
        events, _ = geo.collect_geopolitical_events()
        first_id = events[0]["event_id"]
        self.docs = [document(headline="Cosmetic headline")]
        _, stats = geo.collect_geopolitical_events()
        self.assertEqual(stats["duplicates"], 1)
        self.docs = [document(body=FIXTURES[0]["body"].replace("imposes", "amends"))]
        events, _ = geo.collect_geopolitical_events()
        self.assertNotEqual(events[0]["event_id"], first_id)

    def test_isolated_namespaces(self):
        geo.collect_geopolitical_events(send_alerts=True)
        self.assertTrue(all(k.startswith("mias:geopolitical:") for k in self.redis.data))
        self.assertTrue(any(":processed:" in k for k in self.redis.data))
        self.assertTrue(any(":delivered:" in k for k in self.redis.data))

    def test_corrupt_cache_blocks_send(self):
        events, _ = geo.collect_geopolitical_events()
        self.redis.set(geo.state_key(events[0], "processed"), '{}', ex=60)
        _, stats = geo.collect_geopolitical_events(send_alerts=True)
        self.assertEqual(stats["state_errors"], 1)
        self.telegram.assert_not_called()

    def test_delivery_lease_blocks_concurrency(self):
        events, _ = geo.collect_geopolitical_events()
        self.redis.set(geo.state_key(events[0], "delivery"), "other", ex=60)
        geo.collect_geopolitical_events(send_alerts=True)
        self.telegram.assert_not_called()

    def test_source_failure_isolated(self):
        with patch.object(sources, "SOURCES", {"bis": "x", "fr": "y"}), patch.object(sources, "fetch_documents", side_effect=[ValueError("bad"), [document(1)]]):
            events, stats = geo.collect_geopolitical_events()
        self.assertEqual((len(events), stats["fetch_errors"]), (1, 1))

    def test_opt_in_defaults_preserved(self):
        with patch.object(multi, "read_feed", return_value=([], dict(fetched=0,relevant=0,duplicates=0,processed=0))), patch.object(geo, "collect_geopolitical_events", return_value=([],dict(fetched=0,relevant=0,duplicates=0,processed=0))) as collect:
            multi.collect_all_sources()
            collect.assert_not_called()
            multi.collect_all_sources(include_geopolitical=True)
            collect.assert_called_once_with(enable_ai=True, send_alerts=False)


if __name__ == "__main__":
    unittest.main()
