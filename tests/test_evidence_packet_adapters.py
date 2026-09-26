"""Phase 7C v1 adapters: summary normalization, durable technical rows (validation, hash, bar_end, deep immutability)
and RSS/SEC news events (identity, publication precision, structural separation, allow-list). Offline; sockets off.
"""
from datetime import date, datetime, timedelta, timezone
import copy
import json
import socket
import unittest
from unittest.mock import patch

from evidence_packet import models as m
from evidence_packet.adapters import AdapterError, adapt_news_event, adapt_technical_row, normalize_summary
from market_data.aggregation import aggregate
from market_data.calendar import default_calendar
from market_data.models import EXCHANGE_TZ
from persistence.technical_snapshot_repository import content_hash, snapshot_row
from technical.engine import TechnicalEngine
from tests.market_data_fakes import calendar_bars

CAL = default_calendar()
CANARY = "sk-CANARY-never-serialize-0000"
_ROWS = {}


def technical_rows(symbol="META"):
    """Durable snapshot_row dicts (1d, 1h, 5m) produced by the existing engine on fixture bars through 2026-09-23."""
    if symbol not in _ROWS:
        days = CAL.trading_days(date(2026, 6, 1), date(2026, 9, 23))
        b30 = calendar_bars(symbol, days, 30, base=740.0)
        rows = {}
        for label, bars in (("1d", aggregate(b30, "1d", CAL)), ("1h", aggregate(b30, "1h", CAL)),
                            ("5m", calendar_bars(symbol, days[-12:], 5, base=740.0))):
            snapshot = TechnicalEngine(calendar=CAL).replay(bars)[-1]
            rows[label] = snapshot_row(snapshot, provider="polygon", engine_version="phase4c-v2",
                                       provider_delay_seconds=900, warmup_start=bars[0].timestamp,
                                       warmup_bars=len(bars), session_type=None if label == "1d" else "regular")
        _ROWS[symbol] = rows
    return copy.deepcopy(_ROWS[symbol])


def rss_event(**changes):
    event = dict(source="Yahoo Finance META", publisher="Reuters", headline="Meta announces AI partnership",
                 url="https://example.com/meta-ai", published_at="2026-09-23T13:05:00+00:00",
                 summary="<p>Meta &amp; partner <b>expand</b>\n  AI   work.</p>", symbols=["META"],
                 direct_symbols=["META"], related_symbols=[], relevant=True, impact_score=75, impact_level="HIGH",
                 score_reasons=["Direct META mention", "Keyword: ai", "Source quality: Reuters (+15)"],
                 original_impact_score=75, quality_adjustment=0, alert_decision="ALERT", news_fingerprint="a" * 64,
                 ai_summary="AI summary", ai_sentiment="BULLISH", ai_confidence=80, ai_why_it_matters="because",
                 ai_event_type="partnership", api_key=CANARY, authorization=f"Bearer {CANARY}",
                 raw_payload="<rss>raw</rss>", database_url="postgresql://user:pw@db/mias")
    event.update(changes)
    return event


def sec_event(**changes):
    event = dict(source="SEC EDGAR", publisher="SEC", headline="META filed SEC Form 8-K",
                 url="https://www.sec.gov/Archives/edgar/data/1326801/000132680126000123/form8k.htm",
                 accession_number="0001326801-26-000123", published_at="2026-09-22",
                 summary="SEC filing Form 8-K for META", symbols=["META"], direct_symbols=["META"], related_symbols=[],
                 relevant=True, event_type="sec_filing", sec_form="8-K", sec_fingerprint="b" * 64, impact_score=55,
                 impact_level="MEDIUM", score_reasons=["Direct META mention"], alert_decision="DISPLAY_ONLY")
    event.update(changes)
    return event


OBSERVED = datetime(2026, 9, 23, 13, 7, 12, tzinfo=timezone.utc)


def news(event, family="news", observed=OBSERVED, outcome="processed"):
    return m.NewsInput(family=family, event=event, observed_at=observed, collector_outcome=outcome)


class NoNetwork(unittest.TestCase):
    def run(self, result=None):
        with patch.object(socket, "socket", side_effect=OSError("network disabled in tests")), \
                patch.object(socket, "create_connection", side_effect=OSError("network disabled in tests")):
            return super().run(result)


class SummaryTests(NoNetwork):
    def test_html_entities_whitespace_trim(self):
        self.assertEqual(normalize_summary("<p>Meta &amp; partner <b>expand</b>\n  AI   work.</p>"),
                         "Meta & partner expand AI work.")
        self.assertEqual(normalize_summary("  a&nbsp;b &lt;c&gt; &#8212; d  "), "a b <c> — d")
        self.assertEqual(normalize_summary("<div>x</div><div>y</div><br>z"), "x y z")
        self.assertEqual(normalize_summary("<script>alert(1)</script><style>p{}</style>text"), "text")
        self.assertEqual(normalize_summary("\t\n  spaced \n\n out \t"), "spaced out")

    def test_cap_missing_and_determinism(self):
        long = "é" * 1500
        self.assertEqual(normalize_summary(long), "é" * 1000)
        self.assertEqual(len(normalize_summary("<i>" + "x" * 2000 + "</i>")), 1000)
        for missing in (None, "", "   ", "<p> </p>", 42):
            self.assertIsNone(normalize_summary(missing))
        text = "<p>Same &amp; same</p>"
        self.assertEqual({normalize_summary(text) for _ in range(5)}, {"Same & same"})


class TechnicalAdapterTests(NoNetwork):
    def test_valid_rows_bar_end_and_exact_values(self):
        rows = technical_rows()
        ends = {}
        for interval in ("1d", "1h", "5m"):
            evidence = adapt_technical_row(rows[interval], symbol="META", interval=interval, calendar=CAL)
            ends[interval] = evidence.bar_end
            thawed = evidence.to_dict()["row"]
            self.assertEqual(json.dumps(thawed, sort_keys=True), json.dumps(rows[interval], sort_keys=True))
            self.assertEqual(content_hash(thawed), rows[interval]["content_hash"])
            for key in ("technical_state", "confidence", "rsi14", "vwap", "trend", "breakout_state", "momentum",
                        "support_levels", "significant_high", "engine_version", "source_provider",
                        "provider_delay_seconds", "warmup_start", "warmup_bars", "snapshot_timestamp"):
                self.assertEqual(thawed[key], rows[interval][key], (interval, key))
        close = datetime(2026, 9, 23, 16, tzinfo=EXCHANGE_TZ)
        self.assertEqual(ends, {"1d": close, "1h": close, "5m": close})  # 1d close; 1h 15:30 truncated; 5m 15:55+5m.

    def test_deep_immutability_against_caller_mutation(self):
        row = technical_rows()["5m"]
        evidence = adapt_technical_row(row, symbol="META", interval="5m", calendar=CAL)
        before = json.dumps(evidence.to_dict(), sort_keys=True)
        row["technical_state"] = "bearish_setup"
        row["support_levels"].append({"price": 1})
        row["ema_state"]["alignment"] = "tampered"
        row["reasons"].clear()
        self.assertEqual(json.dumps(evidence.to_dict(), sort_keys=True), before)
        with self.assertRaises(TypeError):
            evidence.row["technical_state"] = "x"
        with self.assertRaises(TypeError):
            evidence.row["ema_state"]["alignment"] = "x"
        self.assertIsInstance(evidence.row["support_levels"], tuple)
        with self.assertRaises(Exception):
            evidence.interval = "1d"

    def test_rejections(self):
        rows = technical_rows()
        tampered = dict(rows["5m"], rsi14=1.0)
        cases = [(tampered, "5m", "META", m.CONTENT_HASH_MISMATCH), (rows["5m"], "5m", "NVDA", m.SYMBOL_MISMATCH),
                 (rows["5m"], "1h", "META", m.INVALID_ROW), ({"symbol": "META"}, "5m", "META", m.INVALID_ROW),
                 (dict(rows["5m"], technical_state="BUY"), "5m", "META", m.CONTENT_HASH_MISMATCH),
                 ("row", "5m", "META", m.INVALID_ROW)]
        bad_state = dict(rows["5m"], technical_state="BUY")
        bad_state["content_hash"] = content_hash(bad_state)  # Hash consistent, vocabulary invalid.
        cases.append((bad_state, "5m", "META", m.INVALID_ROW))
        for row, interval, symbol, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(AdapterError) as caught:
                adapt_technical_row(row, symbol=symbol, interval=interval, calendar=CAL)
            self.assertEqual(caught.exception.reason, reason)

    def test_extra_keys_never_copied(self):
        row = dict(technical_rows()["1d"], api_key=CANARY, raw_payload="x", created_at="2026-09-23", id="uuid")
        text = json.dumps(adapt_technical_row(row, symbol="META", interval="1d", calendar=CAL).to_dict())
        for leaked in (CANARY, "raw_payload", "created_at", '"id"'):
            self.assertNotIn(leaked, text)


class NewsAdapterTests(NoNetwork):
    def test_rss_structure_identity_and_precision(self):
        item = adapt_news_event(news(rss_event()))
        self.assertEqual(item.identity_version, "news-url-v1")
        self.assertEqual(len(item.event_key), 64)
        f = item.facts
        self.assertEqual((f.family, f.headline, f.summary, f.source, f.publisher, f.canonical_url),
                         ("news", "Meta announces AI partnership", "Meta & partner expand AI work.", "Yahoo Finance META",
                          "Reuters", "https://example.com/meta-ai"))
        self.assertEqual((f.published_at, f.publication_date, f.timestamp_precision, f.publication_basis),
                         (datetime(2026, 9, 23, 13, 5, tzinfo=timezone.utc), None, "second", "rss_published"))
        self.assertEqual(item.relevance, m.NewsRelevance(("META",), ("META",), (), True))
        self.assertEqual(item.upstream_score, m.NewsUpstreamScore(75, "HIGH", ("Direct META mention", "Keyword: ai",
                                                                               "Source quality: Reuters (+15)"), 75, 0))
        self.assertEqual(item.delivery, m.NewsDelivery("ALERT", "processed"))
        self.assertEqual(item.observed_at, OBSERVED)

    def test_rss_fallback_identity_and_unknown_times(self):
        item = adapt_news_event(news(rss_event(url="N/A")))
        self.assertEqual((item.identity_version, item.event_key, item.facts.canonical_url),
                         ("news-fingerprint-v1", "a" * 64, None))
        for raw, basis in ((None, "no_feed_date"), ("Tue, 99 Foo", "unparsed_feed_date"),
                           ("2026-09-23T13:05:00", "unparsed_feed_date")):
            f = adapt_news_event(news(rss_event(published_at=raw))).facts
            self.assertEqual((f.timestamp_precision, f.publication_basis, f.published_at), ("unknown", basis, None))

    def test_sec_identity_and_date_only(self):
        item = adapt_news_event(news(sec_event(), family="sec"))
        self.assertEqual((item.identity_version, item.event_key), ("sec-v1", "b" * 64))
        f = item.facts
        self.assertEqual((f.published_at, f.publication_date, f.timestamp_precision, f.publication_basis),
                         (None, date(2026, 9, 22), "date", "sec_submissions_filing_date"))
        self.assertEqual((f.sec_form, f.accession_number, f.canonical_url.startswith("https://www.sec.gov/")),
                         ("8-K", "0001326801-26-000123", True))
        bad_date = adapt_news_event(news(sec_event(published_at="22 Sep"), family="sec")).facts
        self.assertEqual((bad_date.timestamp_precision, bad_date.publication_date), ("unknown", None))

    def test_rejections(self):
        cases = [(news(rss_event(), family="macro"), m.UNSUPPORTED_FAMILY),
                 (news(rss_event(), family="fed"), m.UNSUPPORTED_FAMILY),
                 (news(rss_event(), outcome="near_duplicate_suppressed"), m.NEAR_DUPLICATE_SUPPRESSED),
                 (news(rss_event(headline="")), m.INVALID_EVENT),
                 (news(rss_event(url="N/A", news_fingerprint=None)), m.INVALID_EVENT),
                 (news(rss_event(event_type="macro_release")), m.INVALID_EVENT),
                 (news(rss_event(impact_score="75")), m.INVALID_EVENT),
                 (news(rss_event(), observed=datetime(2026, 9, 23, 13)), m.INVALID_EVENT),
                 (news(sec_event(accession_number="0001326801-26-999999"), family="sec"), m.INVALID_EVENT),
                 (news(sec_event(sec_fingerprint="short"), family="sec"), m.INVALID_EVENT),
                 (news(sec_event(event_type=None), family="sec"), m.INVALID_EVENT)]
        for news_input, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(AdapterError) as caught:
                adapt_news_event(news_input)
            self.assertEqual(caught.exception.reason, reason)

    def test_ai_and_arbitrary_keys_never_serialized(self):
        text = json.dumps(adapt_news_event(news(rss_event())).to_dict())
        for leaked in (CANARY, "Bearer", "ai_", "BULLISH", "AI summary", "raw_payload", "<rss>", "<p>", "postgresql://",
                       "api_key", "authorization", "news_fingerprint"):
            self.assertNotIn(leaked, text)

    def test_immutable_after_adaptation(self):
        event = rss_event()
        item = adapt_news_event(news(event))
        before = json.dumps(item.to_dict(), sort_keys=True)
        event["symbols"].append("NVDA")
        event["score_reasons"].append("tampered")
        event["headline"] = "changed"
        self.assertEqual(json.dumps(item.to_dict(), sort_keys=True), before)
        with self.assertRaises(Exception):
            item.facts.headline = "x"
        self.assertIsInstance(item.upstream_score.score_reasons, tuple)


if __name__ == "__main__":
    unittest.main()
