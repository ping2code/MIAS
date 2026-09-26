"""Phase 7C v1 evidence packet: assembly, identity, availability, as-of leakage, ordering, dedupe, security, isolation.

Everything is offline: sockets are disabled, and no database, Redis, AI or technical-engine execution happens
during assembly.
"""
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
import json
import os
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from evidence_packet import models as m
from evidence_packet.assembler import assemble
from evidence_packet.serialization import canonical_json, content_id
from market_data.calendar import default_calendar
from market_data.models import EXCHANGE_TZ
from tests.test_evidence_packet_adapters import CANARY, news, rss_event, sec_event, technical_rows
from tests.test_market_context import context as market_context_for, et, standard

CAL = default_calendar()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAY = date(2026, 9, 23)
AS_OF = datetime(2026, 9, 23, 20, 5, tzinfo=timezone.utc)  # 16:05 ET, after the close.
OBS = datetime(2026, 9, 23, 13, 7, tzinfo=timezone.utc)


def mctx(symbol="META", now=None):
    return market_context_for(standard(symbol, "100", "100", "97.9"), ("QQQ", standard("QQQ", "100", "100", "99.4")),
                              now=now or et(DAY, 16, 5), symbol=symbol)


def collection(*inputs, succeeded=True):
    return m.NewsCollection(succeeded=succeeded, inputs=tuple(inputs))


def packet(symbol="META", as_of=AS_OF, **kw):
    kw.setdefault("market_context", mctx(symbol))
    kw.setdefault("technical_rows", technical_rows(symbol) if symbol == "META" else None)
    kw.setdefault("news", collection(news(rss_event()), news(sec_event(), family="sec")))
    return assemble(symbol, as_of, calendar=CAL, **kw)


def excluded(p):
    return {e.reason: e.count for e in p.news.excluded}


class NoNetwork(unittest.TestCase):
    def run(self, result=None):
        with patch.object(socket, "socket", side_effect=OSError("network disabled in tests")), \
                patch.object(socket, "create_connection", side_effect=OSError("network disabled in tests")):
            return super().run(result)


class IdentityAndSerializationTests(NoNetwork):
    def test_frozen_deterministic_content_addressed(self):
        a, b = packet(), packet()
        self.assertEqual(canonical_json(a.to_dict()), canonical_json(b.to_dict()))
        self.assertEqual(a.packet_id, b.packet_id)
        self.assertEqual(a.packet_id, content_id(a.body()))
        self.assertRegex(a.packet_id, r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("packet_id", a.body())
        self.assertNotIn("generated_at", json.dumps(a.to_dict()))
        with self.assertRaises(FrozenInstanceError):
            a.symbol = "NVDA"
        with self.assertRaises(FrozenInstanceError):
            a.news.items[0].facts.headline = "x"

    def test_input_order_does_not_matter_for_set_like_inputs(self):
        inputs = [news(rss_event()), news(sec_event(), family="sec"),
                  news(rss_event(url="https://example.com/b", headline="Meta B", published_at="2026-09-23T14:00:00+00:00"))]
        forward = packet(news=collection(*inputs))
        backward = packet(news=collection(*reversed(inputs)))
        self.assertEqual(canonical_json(forward.to_dict()), canonical_json(backward.to_dict()))
        reordered = packet(technical_rows=dict(reversed(list(technical_rows().items()))))
        self.assertEqual(reordered.packet_id, packet().packet_id)  # Technical rows supplied in reverse order.
        self.assertEqual([t.interval for t in reordered.technical.timeframes], ["1d", "1h", "5m"])
        self.assertEqual(reordered.to_dict()["technical"], packet().to_dict()["technical"])

    def test_versions_and_content_participate_in_identity(self):
        base = packet().packet_id
        with patch.object(m, "FORMAT_VERSION", "phase7c-v1-test"):
            self.assertNotEqual(packet().packet_id, base)
        self.assertNotEqual(packet(as_of=AS_OF + timedelta(seconds=1)).packet_id, base)
        self.assertNotEqual(packet(news=collection(news(rss_event(impact_score=76)))).packet_id, base)

    def test_value_types(self):
        d = packet().to_dict()
        self.assertEqual(d["as_of"], "2026-09-23T20:05:00+00:00")
        self.assertEqual(d["market_context"]["context"]["symbol_context"]["return_since_prev_close"], "-0.021")
        self.assertIsInstance(d["technical"]["timeframes"][0]["row"]["price"], float)
        sec = [i for i in d["news"]["items"] if i["facts"]["family"] == "sec"][0]
        self.assertEqual((sec["facts"]["publication_date"], sec["facts"]["published_at"]), ("2026-09-22", None))
        self.assertEqual(d["news"]["items"][0]["observed_at"], "2026-09-23T13:07:12+00:00")
        with self.assertRaises(ValueError):
            canonical_json({"x": float("nan")})
        with self.assertRaises(ValueError):
            assemble("META", datetime(2026, 9, 23, 16), calendar=CAL)


class MarketContextSectionTests(NoNetwork):
    def test_available_preserved_exactly_and_not_mutated(self):
        ctx = mctx()
        before = canonical_json(ctx.to_dict())
        p = packet(market_context=ctx)
        self.assertIs(p.market_context.context, ctx)
        self.assertEqual(p.market_context.availability, m.Availability(m.AVAILABLE))
        self.assertEqual(p.to_dict()["market_context"]["context"], ctx.to_dict())
        self.assertEqual(canonical_json(ctx.to_dict()), before)
        comparisons = p.to_dict()["market_context"]["context"]["comparisons"]
        self.assertEqual([(c["benchmark"], c["basis"]) for c in comparisons], [("QQQ", "prev_close"), ("QQQ", "open")])

    def test_not_supplied_after_as_of_mismatch_and_lagging(self):
        self.assertEqual(packet(market_context=None).market_context.availability,
                         m.Availability(m.UNAVAILABLE, (m.NOT_SUPPLIED,)))
        late = packet(market_context=mctx(now=et(DAY, 16, 30)))  # context.as_of 16:30 ET > packet 16:05 ET.
        self.assertEqual((late.market_context.availability.reasons, late.market_context.context), ((m.AFTER_AS_OF,), None))
        self.assertEqual(packet(market_context=mctx("NVDA")).market_context.availability.reasons, (m.SYMBOL_MISMATCH,))
        lagging = market_context_for(standard("META", "100", "100", "101", n=6), now=et(DAY, 11))
        section = assemble("META", AS_OF, calendar=CAL, market_context=lagging).market_context
        self.assertEqual(section.context.symbol_context.freshness.status, "lagging")
        with self.assertRaises(TypeError):
            assemble("META", AS_OF, calendar=CAL, market_context=lagging.to_dict())


class TechnicalSectionTests(NoNetwork):
    def test_all_timeframes_in_order_and_preserved(self):
        rows = technical_rows()
        section = packet().technical
        self.assertEqual(section.availability, m.Availability(m.AVAILABLE))
        self.assertEqual([t.interval for t in section.timeframes], ["1d", "1h", "5m"])
        for evidence in section.timeframes:
            thawed = evidence.to_dict()["row"]
            self.assertEqual(thawed, rows[evidence.interval])
            self.assertEqual((thawed["technical_state"], thawed["confidence"]),
                             (rows[evidence.interval]["technical_state"], rows[evidence.interval]["confidence"]))

    def test_missing_partial_unavailable_and_cutoff(self):
        rows = technical_rows()
        del rows["1h"]
        partial = packet(technical_rows=rows).technical
        self.assertEqual(partial.availability, m.Availability(m.PARTIAL, ("1h:not_supplied",)))
        self.assertEqual(partial.timeframes[1], m.TechnicalTimeframeEvidence(interval="1h", missing_reason=m.NOT_SUPPLIED))
        none = packet(technical_rows={}).technical
        self.assertEqual(none.availability.status, m.UNAVAILABLE)
        self.assertEqual(packet(technical_rows=None).technical.availability.reasons, (m.NOT_SUPPLIED,))
        early = packet(as_of=datetime(2026, 9, 23, 19, 59, tzinfo=timezone.utc)).technical  # 15:59 ET < 16:00 bar ends.
        self.assertEqual(early.availability, m.Availability(m.UNAVAILABLE, ("1d:after_as_of", "1h:after_as_of",
                                                                            "5m:after_as_of")))
        tampered = technical_rows()
        tampered["5m"]["rsi14"] = 99.9
        self.assertEqual(packet(technical_rows=tampered).technical.availability.reasons, ("5m:content_hash_mismatch",))

    def test_no_technical_engine_execution(self):
        with patch("technical.engine.TechnicalEngine.replay", side_effect=AssertionError("engine ran")), \
                patch("technical.incremental.IncrementalTechnicalEngine.update", side_effect=AssertionError("engine ran")):
            self.assertEqual(packet(technical_rows=technical_rows()).technical.availability.status, m.AVAILABLE)

    def test_caller_mutation_cannot_change_packet(self):
        rows = technical_rows()
        p = packet(technical_rows=rows)
        before = canonical_json(p.to_dict())
        rows["5m"]["technical_state"] = "bearish_setup"
        rows["1d"]["support_levels"].append({"price": 1.0})
        rows["1h"]["ema_state"]["alignment"] = "tampered"
        self.assertEqual(canonical_json(p.to_dict()), before)
        self.assertEqual(p.packet_id, content_id(p.body()))


class NewsSectionTests(NoNetwork):
    def test_availability_semantics(self):
        self.assertEqual(packet(news=None).news.availability, m.Availability(m.UNAVAILABLE, (m.NOT_SUPPLIED,)))
        self.assertEqual(packet(news=collection()).news.availability,
                         m.Availability(m.AVAILABLE_EMPTY, (m.NO_MATCHING_ITEMS,)))
        self.assertEqual(packet(news=collection(succeeded=False)).news.availability,
                         m.Availability(m.UNAVAILABLE, (m.SOURCE_ERROR,)))
        with self.assertRaises(TypeError):
            packet(news=[])  # An empty list never implies a successful collection.
        only_excluded = packet(news=collection(news(rss_event(symbols=["NVDA"]))))
        self.assertEqual((only_excluded.news.availability.status, excluded(only_excluded)),
                         (m.AVAILABLE_EMPTY, {m.SYMBOL_MISMATCH: 1}))

    def test_rss_and_sec_items_structure_and_order(self):
        p = packet(news=collection(
            news(sec_event(), family="sec"),
            news(rss_event()),
            news(rss_event(url="https://example.com/later", headline="Meta later", published_at="2026-09-23T15:00:00+00:00")),
            news(rss_event(url="https://example.com/older", headline="Meta older", published_at="2026-09-22T12:00:00+00:00",
                           symbols=["META", "NVDA"]), observed=datetime(2026, 9, 22, 12, 5, tzinfo=timezone.utc))))
        order = [(i.facts.headline, i.facts.family) for i in p.news.items]
        self.assertEqual(order, [("Meta later", "news"), ("Meta announces AI partnership", "news"), ("Meta older", "news"),
                                 ("META filed SEC Form 8-K", "sec")])  # Same 2026-09-22 date: timestamped before date-only.
        rss = p.news.items[1]
        self.assertEqual((rss.upstream_score.impact_score, rss.upstream_score.impact_level), (75, "HIGH"))
        self.assertEqual(rss.delivery, m.NewsDelivery("ALERT", "processed"))
        self.assertEqual(p.news.availability, m.Availability(m.AVAILABLE))

    def test_multi_symbol_event_same_identity_in_each_packet(self):
        both = rss_event(symbols=["NVDA", "META"], direct_symbols=["META", "NVDA"])
        meta = packet(news=collection(news(both)))
        nvda = packet("NVDA", market_context=None, news=collection(news(both)))
        self.assertEqual((meta.news.items[0].event_key, meta.news.items[0].identity_version),
                         (nvda.news.items[0].event_key, nvda.news.items[0].identity_version))
        self.assertEqual(meta.news.items[0].relevance.symbols, ("META", "NVDA"))

    def test_exclusions_counted_by_reason(self):
        p = packet(news=collection(
            news(rss_event(), family="macro"), news(rss_event(), family="geopolitical"),
            news(rss_event(), outcome="near_duplicate_suppressed"),
            news(rss_event(headline="")),
            news(rss_event(url="https://example.com/nvda", symbols=["NVDA"])),
            news(rss_event(url="https://example.com/wide", symbols=[])),
            news(rss_event(url="https://example.com/raw", published_at="Tue, garbled")),
            news(rss_event(url="https://example.com/future", published_at="2026-09-23T20:42:00+00:00")),
            news(rss_event(url="https://example.com/seen-later"), observed=AS_OF + timedelta(minutes=1)),
            news(sec_event(published_at="2026-09-23"), family="sec"),
            news(rss_event())))
        self.assertEqual(excluded(p), {m.UNSUPPORTED_FAMILY: 2, m.NEAR_DUPLICATE_SUPPRESSED: 1, m.INVALID_EVENT: 1,
                                       m.SYMBOL_MISMATCH: 2, m.UNKNOWN_PUBLICATION_TIME: 1, m.AFTER_AS_OF: 2,
                                       m.OBSERVED_AFTER_AS_OF: 1})
        self.assertEqual(len(p.news.items), 1)
        self.assertEqual([e.reason for e in p.news.excluded], sorted(e.reason for e in p.news.excluded))

    def test_date_only_publication_rule(self):
        for filed, included in (("2026-09-22", True), ("2026-09-23", False), ("2026-09-24", False)):
            p = packet(news=collection(news(sec_event(published_at=filed), family="sec")))
            self.assertEqual(len(p.news.items), int(included), filed)
        # A date-only item is judged by the exchange date of as_of, never an assumed time of day.
        late_utc = datetime(2026, 9, 24, 2, 0, tzinfo=timezone.utc)  # Still 2026-09-23 in New York.
        self.assertEqual(late_utc.astimezone(EXCHANGE_TZ).date(), DAY)
        p = packet(as_of=late_utc, news=collection(news(sec_event(published_at="2026-09-23"), family="sec")))
        self.assertEqual(excluded(p), {m.AFTER_AS_OF: 1})

    def test_duplicates_and_conflicts(self):
        first = news(rss_event(), observed=datetime(2026, 9, 23, 13, 10, tzinfo=timezone.utc))
        earlier = news(rss_event(), observed=datetime(2026, 9, 23, 13, 6, tzinfo=timezone.utc))
        p = packet(news=collection(first, earlier))
        self.assertEqual((len(p.news.items), excluded(p)), (1, {m.DUPLICATE_IDENTICAL: 1}))
        self.assertEqual(p.news.items[0].observed_at, datetime(2026, 9, 23, 13, 6, tzinfo=timezone.utc))
        conflict = packet(news=collection(news(rss_event()), news(rss_event(headline="Meta announces something else")),
                                          news(sec_event(), family="sec")))
        self.assertEqual(excluded(conflict), {m.IDENTITY_CONFLICT: 2})
        self.assertEqual([i.facts.family for i in conflict.news.items], ["sec"])

    def test_ai_extra_keys_and_secrets_never_serialized(self):
        text = canonical_json(packet().to_dict())
        for leaked in (CANARY, "Bearer", "ai_summary", "ai_sentiment", "BULLISH", "AI summary", "raw_payload",
                       "<rss>", "<p>", "postgresql://", "database_url", "api_key", "authorization", "news_fingerprint",
                       "sec_fingerprint"):
            self.assertNotIn(leaked, text)


class PartialFailureTests(NoNetwork):
    def test_one_two_all_unavailable(self):
        one = packet(market_context=None)
        two = packet(market_context=None, technical_rows=None)
        none = assemble("META", AS_OF, calendar=CAL)
        self.assertEqual([one.market_context.availability.status, one.technical.availability.status,
                          one.news.availability.status], [m.UNAVAILABLE, m.AVAILABLE, m.AVAILABLE])
        self.assertEqual([two.market_context.availability.status, two.technical.availability.status],
                         [m.UNAVAILABLE, m.UNAVAILABLE])
        self.assertEqual({s.availability.status for s in (none.market_context, none.technical, none.news)}, {m.UNAVAILABLE})
        self.assertEqual(none.packet_id, assemble("META", AS_OF, calendar=CAL).packet_id)


class BoundaryAndIsolationTests(NoNetwork):
    FORBIDDEN = {"score", "confidence", "direction", "sentiment", "recommendation", "buy", "sell", "call", "put",
                 "overall", "verdict", "signal"}

    def keys(self, value, path=()):
        if isinstance(value, dict):
            for key, child in value.items():
                yield path + (key,)
                yield from self.keys(child, path + (key,))
        elif isinstance(value, list):
            for child in value:
                yield from self.keys(child, path + ("[]",))

    def test_no_packet_level_interpretation(self):
        data = packet().to_dict()
        for path in self.keys(data):
            if path[-1].lower() in self.FORBIDDEN:
                # Allowed only as original domain evidence: technical row fields and the MarketContext payload.
                self.assertTrue(path[:4] == ("technical", "timeframes", "[]", "row") or path[0] == "market_context", path)
        self.assertEqual(set(data), {"format_version", "packet_id", "symbol", "as_of", "market_context", "technical",
                                     "news", "provenance"})
        self.assertNotIn("options", data)
        self.assertEqual(data["format_version"], "phase7c-v1")

    def test_zero_io_during_assembly(self):
        with patch("persistence.database.make_engine", side_effect=AssertionError("database")), \
                patch("builtins.open", side_effect=AssertionError("file I/O")):
            packet()

    def test_forbidden_imports(self):
        code = ("import sys, evidence_packet, evidence_packet.assembler, evidence_packet.adapters\n"
                "bad = sorted(n for n in sys.modules if n.split('.')[0] in ('collector', 'analyzer', 'alert_engine', "
                "'evidence', 'evaluation', 'openai', 'redis', 'telegram', 'dotenv', 'feedparser', 'requests') "
                "or n == 'shared.config')\nprint(bad)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])


if __name__ == "__main__":
    unittest.main()
