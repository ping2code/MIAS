"""Treasury tests: HTTP, Redis, AI, Telegram and dotenv are always isolated."""

import copy
import io
import json
import os
import runpy
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "ALERT_THRESHOLD": "70", "DISPLAY_THRESHOLD": "40", "TREASURY_MAX_AGE_HOURS": "48",
    "TREASURY_YIELD_MOVE_BPS": "15", "REDIS_HOST": "localhost", "REDIS_PORT": "6379",
}, clear=True):
    from collector import treasury_collector as treasury
    from collector import treasury_sources as sources
    from collector import multi_source_collector as multi
    from collector.treasury_normalizer import normalize_release, normalize_auction, normalize_debt_letter, official_url, treasury_identity
    from collector.treasury_yields import parse_yield_observations, normalize_yield_events
    from analyzer.treasury_scoring import score_treasury_event

FIXTURES = Path(__file__).parent / "fixtures" / "treasury"
RELEASE_URL = "https://home.treasury.gov/news/press-releases/test-refunding"


def release():
    return normalize_release((FIXTURES / "refunding.html").read_text(), RELEASE_URL)


def auction():
    return json.loads((FIXTURES / "auctions.json").read_text())[0]


def yield_rows():
    return [{"observation_date": "2026-09-18", "yields_percent": {"BC_2YEAR": 4.0, "BC_10YEAR": 4.5, "BC_30YEAR": 5.0}},
            {"observation_date": "2026-09-21", "yields_percent": {"BC_2YEAR": 4.15, "BC_10YEAR": 4.4, "BC_30YEAR": 5.0}}]


class Response:
    def __init__(self, data, mime="text/html", status=200):
        self.data = data.encode() if isinstance(data, str) else data
        self.headers, self.status_code = {"Content-Type": mime}, status
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def raise_for_status(self):
        if self.status_code >= 400:
            raise sources.requests.HTTPError("private detail")
    def iter_content(self, chunk_size):
        yield self.data


class MemoryRedis:
    def __init__(self, clock):
        self.clock, self.store, self.calls = clock, {}, []
    def get(self, key):
        self.calls.append(("get", key))
        value, expiry = self.store.get(key, (None, self.clock()))
        if expiry <= self.clock():
            self.store.pop(key, None)
            return None
        return value
    def set(self, key, value, *, ex, nx=False):
        self.calls.append(("set", key))
        if nx and self.get(key) is not None:
            return None
        self.store[key] = value, self.clock() + timedelta(seconds=ex)
        return True
    def eval(self, script, count, *args):
        self.calls.append(("eval", args[0]))
        if script == treasury.RELEASE_LEASE:
            key, token = args
            if self.get(key) == token:
                self.store.pop(key, None)
                return 1
            return 0
        assert script == treasury.CACHE_IF_OWNER and count == 2
        key, cache, token, value, ttl = args
        if self.get(key) != token:
            return 0
        self.set(cache, value, ex=ttl)
        return 1


class TreasuryNormalizerTests(unittest.TestCase):
    def test_release_schema_score_and_cosmetic_identity(self):
        event = release()
        self.assertEqual(event["published_at"], "2026-09-21T12:30:00+00:00")
        self.assertEqual(event["treasury_category"], "quarterly_refunding")
        self.assertEqual(event["symbols"], [])
        self.assertEqual(score_treasury_event(event)["impact_score"], 85)
        self.assertEqual(treasury_identity(dict(event, headline="Cosmetic", url="new")), event["event_id"])

    def test_material_categories_and_unrelated_release_filter(self):
        from collector.treasury_normalizer import classify_release
        for title, body, category in (
            ("Treasury Announces Marketable Borrowing Estimates", "Official estimate", "borrowing_estimates"),
            ("Treasury Announces Increased Sizes of Buybacks", "Treasury is increasing buyback sizes", "issuance_policy"),
            ("Treasury letter on debt limit", "Treasury will begin extraordinary measures", "extraordinary_measures"),
            ("Treasury debt limit letter", "Treasury will be unable to pay obligations", "debt_limit"),
            ("Treasury launches emergency liquidity facility", "Treasury launches emergency liquidity support", "material_press_release"),
            ("Remarks on historical perspectives on debt limit", "Unable to pay obligations", "routine_release"),
            ("TBAC recommendations", "Treasury will increase issuance", "routine_release"),
            ("New commemorative coin", "Mention of debt limit", "routine_release")):
            self.assertEqual(classify_release(title, body), category)

    def test_explicit_correction_versions_but_footer_does_not(self):
        html = (FIXTURES / "refunding.html").read_text()
        updated = html.replace("</p></div>", "</p><p>Correction: September 22, 2026 at 9:00 a.m.</p></div>")
        corrected = normalize_release(updated, RELEASE_URL)
        self.assertNotEqual(corrected["event_id"], release()["event_id"])
        self.assertEqual(corrected["original_published_at"], release()["published_at"])
        self.assertEqual(normalize_release(html + "<footer>Updated September 22, 2026</footer>", RELEASE_URL)["event_id"], release()["event_id"])

    def test_missing_publication_and_naive_time(self):
        html = (FIXTURES / "refunding.html").read_text()
        self.assertIsNone(normalize_release(html.replace('datetime="2026-09-21T08:30:00-04:00"', ""), RELEASE_URL)["published_at"])
        with self.assertRaises(ValueError):
            normalize_release(html.replace("08:30:00-04:00", "08:30:00"), RELEASE_URL)

    def test_official_url_validation(self):
        for url in ("http://home.treasury.gov/a", "https://home.treasury.gov.evil/a", "https://user:pw@home.treasury.gov/a", "https://home.treasury.gov:444/a"):
            with self.assertRaises(ValueError):
                official_url(url)

    def test_debt_letter_requires_extracted_body_and_date(self):
        text = (FIXTURES / "debt_letter.txt").read_text()
        event = normalize_debt_letter(text, "https://home.treasury.gov/system/files/136/letter.pdf")
        self.assertEqual(event["treasury_category"], "debt_limit")
        self.assertEqual(event["published_at"], "2026-09-21T04:00:00+00:00")
        for invalid in ("", "debt limit", "x" * 400 + text[50:]):
            with self.assertRaises(ValueError):
                normalize_debt_letter(invalid, "https://home.treasury.gov/system/files/136/letter.pdf")

    def test_live_auction_fixture_stages_dates_and_numeric_facts(self):
        row = auction()
        announcement = normalize_auction(row, "announcement")
        result = normalize_auction(row, "result")
        self.assertNotEqual(announcement["event_id"], result["event_id"])
        self.assertEqual(result["published_at"], "2026-09-21T04:00:00+00:00")
        self.assertEqual(result["timestamp_precision"], "date")
        self.assertEqual(result["metrics"]["bid_to_cover"], 2.77)
        self.assertEqual(result["metrics"]["highDiscountRate_percent"], 4.015)
        self.assertNotIn("tail", result["metrics"])

    def test_reopening_and_same_cusip_new_auction_differ(self):
        row = auction()
        old = normalize_auction(row, "result")
        row.update(auctionDate="2026-10-01T00:00:00", pdfFilenameCompetitiveResults="R_20261001_1.pdf")
        self.assertNotEqual(normalize_auction(row, "result")["event_id"], old["event_id"])

    def test_updated_timestamp_does_not_refresh_or_reidentify(self):
        row = auction()
        first = normalize_auction(row, "result")
        row.update(updatedTimestamp="2026-09-22T12:30:00", bidToCoverRatio="3.0")
        second = normalize_auction(row, "result")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["published_at"], second["published_at"])

    def test_incomplete_auction_results_and_document_date_mismatch(self):
        for field, value in (("totalAccepted", ""), ("pdfFilenameCompetitiveResults", ""),
                             ("bidToCoverRatio", "NaN"), ("pdfFilenameCompetitiveResults", "R_20260922_1.pdf")):
            row = auction()
            row[field] = value
            with self.assertRaises(ValueError):
                normalize_auction(row, "result")

    def test_all_auction_scores_and_quality_adjustment(self):
        event = normalize_auction(auction(), "result")
        for kind, score in (("Note", 70), ("Bond", 70), ("TIPS", 60), ("FRN", 60), ("Bill", 40)):
            event["security_type"] = kind
            scored = score_treasury_event(event)
            self.assertEqual(scored["impact_score"], score)
            self.assertEqual(scored["quality_adjustment"], 0)


class TreasuryYieldTests(unittest.TestCase):
    def test_captured_xml_schema_and_updated_is_not_publication(self):
        xml = (FIXTURES / "yields.xml").read_bytes()
        rows = parse_yield_observations(xml)
        self.assertEqual(len(rows), 2)
        events = normalize_yield_events(rows, date(2026, 9, 22))
        self.assertTrue(all(e["published_at"] is None for e in events))
        self.assertTrue(all(e["event_type"] == "treasury_yield_observation" for e in events))

    def test_valid_prior_threshold_and_exact_basis_points(self):
        events = normalize_yield_events(yield_rows(), date(2026, 9, 22))
        self.assertEqual(score_treasury_event(events[0])["impact_score"], 30)
        second = events[1]
        self.assertEqual(second["metrics"]["movement_bps"]["BC_2YEAR"], 15)
        self.assertEqual(second["metrics"]["spread_2s10s_bps"], 25)
        self.assertEqual(score_treasury_event(second)["impact_score"], 70)
        self.assertEqual(score_treasury_event(second, yield_threshold_bps=16)["impact_score"], 30)

    def test_negative_movement_and_routine_yields(self):
        rows = yield_rows()
        rows[1]["yields_percent"]["BC_2YEAR"] = 3.8
        self.assertEqual(score_treasury_event(normalize_yield_events(rows, date(2026,9,22))[1])["impact_score"], 70)
        rows[1]["yields_percent"]["BC_2YEAR"] = 4.01
        self.assertEqual(score_treasury_event(normalize_yield_events(rows, date(2026,9,22))[1])["impact_score"], 30)

    def test_long_gap_missing_tenors_and_future_dates(self):
        rows = yield_rows()
        rows[0]["observation_date"] = "2026-09-01"
        self.assertEqual(score_treasury_event(normalize_yield_events(rows,date(2026,9,22))[1])["impact_score"],30)
        rows[1]["observation_date"] = "2026-09-23"
        with self.assertRaises(ValueError):
            normalize_yield_events(rows,date(2026,9,22))
        rows = yield_rows()
        rows[0]["yields_percent"] = {"BC_1MONTH": 3.0}
        self.assertEqual(normalize_yield_events(rows,date(2026,9,22))[1]["metrics"]["movement_bps"], {})

    def test_revisions_order_and_conflicting_duplicate_dates(self):
        rows=yield_rows()
        first=normalize_yield_events(rows,date(2026,9,22))
        self.assertEqual(first,normalize_yield_events(list(reversed(rows)),date(2026,9,22)))
        revised=copy.deepcopy(rows)
        revised[1]["yields_percent"]["BC_2YEAR"]=4.5
        self.assertEqual(first[1]["event_id"],normalize_yield_events(revised,date(2026,9,22))[1]["event_id"])
        with self.assertRaises(ValueError):
            normalize_yield_events(rows+[revised[1]],date(2026,9,22))

    def test_xml_null_values_and_malformed_xml(self):
        xml=(FIXTURES / "yields.xml").read_bytes()
        for bad in (b"<html>denied</html>",b"<feed>", b'<!DOCTYPE feed>'+xml):
            with self.assertRaises(ValueError):
                parse_yield_observations(bad)


class TreasuryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,9,21,14,tzinfo=timezone.utc)
        self.enterContext(patch("requests.sessions.Session.request",side_effect=AssertionError("Live HTTP forbidden")))
        clock=self.enterContext(patch.object(treasury,"datetime",wraps=datetime))
        clock.now.side_effect=lambda tz:self.now.astimezone(tz)
        self.redis=MemoryRedis(lambda:self.now)
        self.enterContext(patch.object(treasury.deduplicator,"redis_client",self.redis))
        self.index=self.enterContext(patch.object(sources,"fetch_index_documents",return_value=[{"url":RELEASE_URL,"kind":"release"}]))
        self.fetch=self.enterContext(patch.object(sources,"fetch",return_value=(FIXTURES / "refunding.html").read_bytes()))
        self.auctions=self.enterContext(patch.object(sources,"fetch_auctions",return_value=[]))
        self.yields=self.enterContext(patch.object(sources,"fetch_yields",return_value=[]))
        self.ai=self.enterContext(patch.object(treasury,"analyze_treasury_event",side_effect=self.enrich))
        self.send=self.enterContext(patch.object(treasury,"deliver_treasury_alert",return_value={"ok":True}))
        self.enterContext(patch.object(treasury.deduplicator,"is_near_duplicate_headline",side_effect=AssertionError("News dedup forbidden")))

    @staticmethod
    def enrich(event):
        return dict(event,ai_summary="Official financing policy update",ai_sentiment="NEUTRAL",ai_confidence=85,ai_why_it_matters="Relevant to rates",ai_event_type="official policy")

    def test_default_and_exact_cross_index_dedup(self):
        events,stats=treasury.collect_treasury_events()
        self.assertEqual(stats["processed"],1)
        self.assertEqual(events[0]["impact_score"],85)
        self.fetch.assert_called_once()
        self.ai.assert_called_once()
        self.send.assert_not_called()
        self.assertTrue(all(k.startswith("mias:treasury:") for _,k in self.redis.calls))

    def test_dry_run_failed_delivery_retry_and_confirmed_marker(self):
        treasury.collect_treasury_events()
        self.send.return_value={"ok":False}
        with self.assertLogs("treasury_collector",level="ERROR"):
            self.assertEqual(treasury.collect_treasury_events(send_alerts=True)[1]["delivery_errors"],1)
        self.send.return_value={"ok":True}
        self.assertEqual(treasury.collect_treasury_events(send_alerts=True)[1]["delivered"],1)
        self.assertEqual(treasury.collect_treasury_events(send_alerts=True)[1]["delivered"],0)
        self.ai.assert_called_once()
        self.assertEqual(self.send.call_count,2)

    def test_redis_failure_blocks_ai_and_delivery(self):
        with patch.object(self.redis,"get",side_effect=treasury.deduplicator.redis.RedisError("private")),self.assertLogs("treasury_collector") as logs:
            self.assertEqual(treasury.collect_treasury_events(send_alerts=True)[1]["state_errors"],1)
        self.assertNotIn("private",str(logs.output))
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_cache_write_failure_and_retry(self):
        original=self.redis.set
        def fail(key,*args,**kwargs):
            if ":processed:" in key:raise treasury.deduplicator.redis.RedisError("failure")
            return original(key,*args,**kwargs)
        with patch.object(self.redis,"set",side_effect=fail),self.assertLogs("treasury_collector"):
            treasury.collect_treasury_events(send_alerts=True)
        self.send.assert_not_called()
        self.assertEqual(treasury.collect_treasury_events(send_alerts=True)[1]["delivered"],1)

    def test_stale_future_missing_before_state(self):
        for now,reason in ((self.now+timedelta(days=3),"stale"),(self.now-timedelta(days=1),"future")):
            self.now=now
            self.assertEqual(treasury.collect_treasury_events()[1][reason],1)
        self.now=datetime(2026,9,21,14,tzinfo=timezone.utc)
        self.fetch.return_value=self.fetch.return_value.replace(b'datetime="2026-09-21T08:30:00-04:00"',b'')
        self.assertEqual(treasury.collect_treasury_events()[1]["missing_date"],1)
        self.assertEqual(self.redis.calls,[])

    def test_exact_freshness_boundary_and_short_ttl(self):
        self.now=datetime(2026,9,23,12,30,tzinfo=timezone.utc)
        with patch.object(treasury,"DEDUP_TTL_SECONDS",1):
            self.assertEqual(treasury.collect_treasury_events(enable_ai=False)[1]["processed"],1)
        self.now+=timedelta(seconds=1)
        self.assertEqual(treasury.collect_treasury_events()[1]["stale"],1)

    def test_ai_cannot_mutate_parser_fields(self):
        self.index.return_value=[]
        row=auction()
        row.update(securityType="Note",securityTerm="2-Year",highYield="4.1")
        self.auctions.return_value=[row]
        def malicious(event):
            event["metrics"]["total_accepted_usd"]=1
            event.update(event_id="wrong",impact_score=0,published_at="2099-01-01")
            return self.enrich(event)
        self.ai.side_effect=malicious
        events,_=treasury.collect_treasury_events()
        event=next(e for e in events if e["release_stage"]=="result")
        self.assertNotEqual(event["metrics"]["total_accepted_usd"],1)
        self.assertEqual(event["impact_score"],70)
        self.assertEqual(event["quality_adjustment"],0)
        self.assertNotEqual(event["event_id"],"wrong")

    def test_ai_failures_and_disabled(self):
        self.ai.side_effect=RuntimeError("private API detail")
        with self.assertLogs("treasury_collector") as logs:
            events,stats=treasury.collect_treasury_events()
        self.assertEqual(stats["analysis_errors"],1)
        self.assertEqual(events[0]["impact_score"],85)
        self.assertNotIn("private API detail",str(logs.output))
        self.redis.store.clear()
        self.ai.reset_mock()
        treasury.collect_treasury_events(enable_ai=False)
        self.ai.assert_not_called()

    def test_invalid_ai_enrichment_retains_deterministic_result(self):
        self.ai.side_effect=lambda e:dict(self.enrich(e),ai_confidence=999)
        with self.assertLogs("treasury_collector"):
            events,stats=treasury.collect_treasury_events()
        self.assertEqual(stats["analysis_errors"],1)
        self.assertNotIn("ai_summary",events[0])
        self.assertEqual(events[0]["quality_adjustment"],0)

    def test_partial_result_is_retryable_without_consuming_result_identity(self):
        self.index.return_value=[]
        row=auction()
        row.update(securityType="Note",securityTerm="2-Year",highYield="4.1",totalAccepted="")
        self.auctions.return_value=[row]
        with self.assertLogs("treasury_collector"):
            events,stats=treasury.collect_treasury_events()
        self.assertEqual(stats["invalid"],1)
        self.assertEqual(events,[])
        self.ai.assert_not_called()
        row["totalAccepted"]="1000000000"
        events,stats=treasury.collect_treasury_events()
        self.assertEqual(stats["processed"],1)
        self.assertEqual(events[0]["release_stage"],"result")

    def test_corrupt_cache_blocks_delivery(self):
        self.redis.set(treasury.state_key(release(),"processed"),'{"event_id":"wrong"}',ex=100)
        with self.assertLogs("treasury_collector"):
            self.assertEqual(treasury.collect_treasury_events(send_alerts=True)[1]["state_errors"],1)
        self.send.assert_not_called()

    def test_yield_observations_retained_but_no_unverified_alert(self):
        self.index.return_value=[]
        self.yields.return_value=normalize_yield_events(yield_rows(),date(2026,9,22))
        events,stats=treasury.collect_treasury_events(send_alerts=True)
        self.assertEqual(stats["stale"],1)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]["impact_score"],70)
        self.assertEqual(events[0]["initial_decision"],"ALERT")
        self.assertEqual(events[0]["alert_decision"],"DISPLAY_ONLY")
        self.assertFalse(events[0]["alert_eligible"])
        self.ai.assert_not_called()
        self.send.assert_not_called()

    def test_expired_owner_cannot_write_cache(self):
        def slow(event):
            self.now+=timedelta(seconds=treasury.LEASE_SECONDS+1)
            return self.enrich(event)
        self.ai.side_effect=slow
        with self.assertLogs("treasury_collector"):
            self.assertEqual(treasury.collect_treasury_events(send_alerts=True)[1]["state_errors"],1)
        self.send.assert_not_called()

    def test_delivery_rechecks_freshness_and_lease(self):
        treasury.collect_treasury_events()
        key=treasury.state_key(release(),"event")+":delivery"
        self.redis.set(key,"other",ex=100)
        treasury.collect_treasury_events(send_alerts=True)
        treasury._release(key,"expired")
        self.assertEqual(self.redis.get(key),"other")
        self.send.assert_not_called()
        self.now+=timedelta(hours=49)
        treasury.collect_treasury_events(send_alerts=True)
        self.send.assert_not_called()

    def test_pdf_failure_does_not_infer_from_link(self):
        self.index.return_value=[{"url":"https://home.treasury.gov/system/files/136/debt-limit.pdf","kind":"letter"}]
        with patch.object(sources,"extract_pdf_text",side_effect=ValueError("unreadable")),self.assertLogs("treasury_collector"):
            events,stats=treasury.collect_treasury_events()
        self.assertEqual(events,[])
        self.assertEqual(stats["invalid"],1)
        self.ai.assert_not_called()

    def test_source_failure_isolated(self):
        self.auctions.side_effect=ValueError("bad schema")
        with self.assertLogs("treasury_collector"):
            events,stats=treasury.collect_treasury_events(enable_ai=False)
        self.assertEqual(stats["fetch_errors"],1)
        self.assertEqual(len(events),1)

    def test_aggregate_default_unchanged_and_opt_in(self):
        counts=dict(fetched=0,relevant=0,processed=0,duplicates=0)
        with patch.object(multi,"read_feed",return_value=([],counts)),patch.object(treasury,"collect_treasury_events",return_value=([],counts)) as collect:
            multi.collect_all_sources()
            collect.assert_not_called()
            multi.collect_all_sources(include_treasury=True,treasury_enable_ai=False)
            collect.assert_called_once_with(enable_ai=False,send_alerts=False)


class TreasurySourceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("requests.sessions.Session.request",side_effect=AssertionError("Live HTTP forbidden")))

    def test_http_headers_types_size_and_redirects(self):
        for response in (Response("x","text/html",403),Response("x","application/json",302),Response("x","text/html"),Response("x"*101,"application/json")):
            with patch.object(sources.requests,"get",return_value=response) as get,patch.object(sources,"MAX_BYTES",100),self.assertRaises((ValueError,sources.requests.HTTPError)):
                sources.fetch(sources.AUCTION_URL,{"application/json"})
            self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_auction_date_queries_and_filter_validation(self):
        with patch.object(sources,"fetch",return_value=b'[]') as fetch:
            self.assertEqual(sources.fetch_auctions(date(2026,9,22),lookback_days=3),[])
            self.assertEqual(fetch.call_count,6)
            self.assertTrue(any("announcementDate=" in c.args[0] for c in fetch.call_args_list))
        with patch.object(sources,"fetch",return_value=json.dumps([auction()]).encode()),self.assertRaises(ValueError):
            sources.fetch_auctions(date(2026,9,22),lookback_days=1)

    def test_month_boundary_yields_fetch_prior_month(self):
        with patch.object(sources,"fetch",return_value=b'xml') as fetch,patch.object(sources,"parse_yield_observations",return_value=[]):
            self.assertEqual(sources.fetch_yields(date(2026,1,2)),[])
        self.assertIn("202512",fetch.call_args_list[0].args[0])
        self.assertIn("202601",fetch.call_args_list[1].args[0])

    def test_discovery_official_links_only(self):
        html='<main><a href="/news/press-releases/sb123">Release</a><a href="https://evil.example/news/press-releases/x">Bad</a><a href="/system/files/136/debt.pdf">Debt Limit Letter</a></main>'
        docs=sources.discover_documents(html,sources.DEBT_URL)
        self.assertEqual(len(docs),2)
        self.assertEqual({d["kind"] for d in docs},{"release","letter"})

    def test_pagination_contract_and_caps(self):
        html='<main><a href="/news/press-releases/sb123">Release</a><a rel="next" href="?page=1">Next</a></main>'
        with patch.object(sources,"fetch",return_value=html.encode()),self.assertLogs("treasury_collector"):
            self.assertEqual(len(sources.fetch_index_documents(sources.PRESS_URL)),1)
        with patch.object(sources,"fetch",return_value=html.replace('?page=1','https://evil.example/').encode()),self.assertRaises(ValueError):
            sources.fetch_index_documents(sources.PRESS_URL)

    def test_official_news_manifest_replaces_broken_html_pager(self):
        html=b'<main><div data-news-manifest="/news-data/press-releases/manifest.json"></div><a rel="next" href="?page=2">Next</a></main>'
        manifest={"category":"press-releases","searchShards":[{"endYear":2026,"path":"/news-data/press-releases/search/2026.json","count":1}]}
        shard={"category":"press-releases","count":1,"items":[{"url":"/news/press-releases/sb123/","datetime":"2026-09-21T12:30:00Z"}]}
        with patch.object(sources,"fetch",side_effect=[html,json.dumps(manifest).encode(),json.dumps(shard).encode()]) as fetch:
            docs=sources.fetch_index_documents(sources.PRESS_URL)
        self.assertEqual(docs,[{"url":"https://home.treasury.gov/news/press-releases/sb123","kind":"release"}])
        self.assertEqual(fetch.call_count,3)

    def test_news_manifest_count_and_external_path_rejected(self):
        manifest={"category":"press-releases","searchShards":[{"endYear":2026,"path":"/news-data/press-releases/search/2026.json","count":2}]}
        shard={"category":"press-releases","count":1,"items":[{"url":"/news/press-releases/sb123","datetime":"2026-09-21"}]}
        with patch.object(sources,"fetch",side_effect=[json.dumps(manifest).encode(),json.dumps(shard).encode()]),self.assertRaises(ValueError):
            sources.fetch_press_manifest("/news-data/press-releases/manifest.json")
        with self.assertRaises(ValueError):
            sources.fetch_press_manifest("https://evil.example/manifest.json")

    def test_pdf_parser_rejects_empty_encrypted_and_bad_data(self):
        from pypdf import PdfWriter
        writer=PdfWriter()
        writer.add_blank_page(width=100,height=100)
        buffer=io.BytesIO()
        writer.write(buffer)
        with self.assertRaises(ValueError):
            sources.extract_pdf_text(buffer.getvalue())
        with self.assertRaises(ValueError):
            sources.extract_pdf_text(b'not a pdf')
        writer.encrypt("test-only")
        encrypted=io.BytesIO()
        writer.write(encrypted)
        with self.assertRaises(ValueError):
            sources.extract_pdf_text(encrypted.getvalue())

    def test_pdf_real_text_extraction_to_letter_normalization(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer=PdfWriter()
        page=writer.add_blank_page(width=612,height=792)
        font=DictionaryObject({NameObject("/Type"):NameObject("/Font"),NameObject("/Subtype"):NameObject("/Type1"),NameObject("/BaseFont"):NameObject("/Helvetica")})
        page[NameObject("/Resources")]=DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):font})})
        text=(FIXTURES / "debt_letter.txt").read_text().replace("\n"," ")
        content=DecodedStreamObject()
        content.set_data(("BT /F1 10 Tf 20 700 Td ("+text+") Tj ET").encode("ascii"))
        page[NameObject("/Contents")]=content
        buffer=io.BytesIO()
        writer.write(buffer)
        extracted=sources.extract_pdf_text(buffer.getvalue())
        event=normalize_debt_letter(extracted,"https://home.treasury.gov/system/files/136/letter.pdf")
        self.assertEqual(event["treasury_category"],"debt_limit")
        self.assertIn("October 15, 2026",event["summary"])

    def test_config_defaults_override_and_invalid(self):
        with patch("dotenv.load_dotenv"),patch.dict(os.environ,{},clear=True):
            values=runpy.run_path("shared/config.py")
            self.assertEqual(values["TREASURY_MAX_AGE_HOURS"],48)
            self.assertEqual(values["TREASURY_YIELD_MOVE_BPS"],15)
            for value in ("0","-1","nan","inf"):
                with patch.dict(os.environ,{"TREASURY_YIELD_MOVE_BPS":value}),self.assertRaises(ValueError):
                    runpy.run_path("shared/config.py")


if __name__ == "__main__":
    unittest.main()
