"""Phase 7G builder: goldens, explicit semantics, determinism, input forms, ordering, identity, immutability, freshness."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date
import json
import unittest
from unittest.mock import patch

from evidence_packet.serialization import canonical_json, content_id
from evidence_synthesis import rules
from evidence_synthesis.builder import synthesize
from tests import evidence_synthesis_cases as cases
from tests.test_evidence_packet import AS_OF, CAL, collection, packet
from tests.test_evidence_packet_adapters import news, rss_event
from tests.test_market_context import context as market_context_for, et, flat, standard

FORBIDDEN_TOP_LEVEL = {"direction", "confidence", "score", "strength", "recommendation", "readiness", "setup",
                       "signal", "prediction", "options", "trade", "target", "stop", "probability", "expected_return"}


def load(name):
    return json.loads(cases.packet_path(name).read_text(encoding="utf-8"))


def codes(synthesis):
    return sorted({c.code for c in synthesis.contradictions})


class GoldenTests(unittest.TestCase):
    def test_goldens_byte_exact(self):
        for name in cases.CASES:
            with self.subTest(case=name):
                expected = cases.synthesis_path(name).read_text(encoding="utf-8")
                self.assertEqual(canonical_json(synthesize(load(name)).to_dict()) + "\n", expected)

    def test_golden_packets_still_match_builders(self):
        """The stored packets are exactly what the Phase 7C assembler builds today (no silent fixture drift)."""
        for name, build in cases.CASES.items():
            with self.subTest(case=name):
                self.assertEqual(build().to_dict(), load(name))

    def test_explicit_case_semantics(self):
        expect = {
            "all_bullish": ("all_bullish", ["agree", "agree", "agree"]),
            "all_bearish": ("all_bearish", ["agree", "agree", "agree"]),
            "non_directional": ("all_non_directional", ["non_directional"] * 3),
            "higher_aligned_5m_opposed": ("opposed", ["agree", "oppose", "oppose"]),
            "lower_aligned_1d_opposed": ("opposed", ["oppose", "agree", "oppose"]),
            "unavailable_timeframe": ("incomplete", ["agree", "unavailable", "unavailable"]),
        }
        for name, (pattern, relations) in expect.items():
            s = synthesize(load(name))
            self.assertEqual(s.timeframe_alignment.pattern, pattern, name)
            self.assertEqual([r.relation for r in s.timeframe_relations], relations, name)
            self.assertEqual([(r.first, r.second) for r in s.timeframe_relations], list(rules.PAIRS))
        opposed = synthesize(load("higher_aligned_5m_opposed"))
        self.assertEqual([c.subjects for c in opposed.contradictions if c.code == "timeframe_opposition"],
                         [("1d", "5m"), ("1h", "5m")])
        missing = synthesize(load("unavailable_timeframe"))
        self.assertEqual(missing.completeness.technical, "partial")
        self.assertEqual([(t.interval, t.reason) for t in missing.completeness.technical_missing],
                         [("5m", "not_supplied")])
        self.assertIn(("5m",), [c.subjects for c in missing.contradictions if c.code == "timeframe_unavailable"])

    def test_mixed_alone_is_not_a_contradiction(self):
        s = synthesize(load("non_directional"))
        self.assertEqual([t.state for t in s.timeframes], ["range", "mixed", "range"])
        self.assertEqual(s.contradictions, ())
        self.assertNotIn("technical_state_mixed", json.dumps(s.to_dict()))

    def test_source_facts_copied_verbatim(self):
        data = load("meta_real_shaped")
        s = synthesize(data)
        for frame, facts in zip(data["technical"]["timeframes"], s.timeframes):
            row = frame["row"]
            self.assertEqual((facts.state, facts.trend, facts.breakout_state, facts.momentum, facts.ema_alignment,
                              facts.vwap_position, facts.technical_confidence, facts.bar_end),
                             (row["technical_state"], row["trend"], row["breakout_state"], row["momentum"],
                              row["ema_alignment"], row["vwap_position"], row["confidence"], frame["bar_end"]))
            self.assertEqual(facts.age_seconds, 300)  # AS_OF 20:05Z − bar_end 20:00Z.


class MarketContextTests(unittest.TestCase):
    def test_both_bases_every_benchmark_no_winner(self):
        s = synthesize(load("meta_real_shaped"))
        refs = [(ref.reference, ref.basis, ref.value_sign) for ref in s.market_context.references]
        self.assertEqual(refs, [("self", "prev_close", "negative"), ("self", "open", "negative"),
                                ("QQQ", "prev_close", "negative"), ("QQQ", "open", "negative")])
        self.assertEqual(len(s.market_context.relations), 3 * len(refs))
        self.assertEqual(s.market_context.context_age_seconds, 0)
        self.assertEqual(s.market_context.freshness_status, "lagging")  # Preserved, never re-judged.
        # Technical facts are never changed by market context.
        self.assertEqual([t.state_direction for t in s.timeframes], ["bullish", "non_directional", "bullish"])
        opposing = [c.subjects for c in s.contradictions if c.code == "market_context_opposes_timeframe"]
        self.assertEqual(opposing, sorted(opposing))
        self.assertIn(("1d", "QQQ", "prev_close"), opposing)
        self.assertNotIn(("1h", "QQQ", "prev_close"), opposing)  # Non-directional timeframe: no opposition.

    def test_unavailable_market_context(self):
        s = synthesize(load("market_context_unavailable"))
        self.assertEqual((s.market_context.available, s.market_context.references, s.market_context.relations),
                         (False, (), ()))
        self.assertIn("market_context_unavailable", codes(s))

    def test_misaligned_comparison_and_session_mismatch(self):
        meta = standard("META", "100", "100", "101", n=6)
        spy = standard("SPY", "100", "100", "101", n=8)
        del spy[78 + 5]  # SPY lacks the bar ending 10:00: the comparison is misaligned.
        ctx = market_context_for(meta, ("SPY", spy))
        s = synthesize(packet(market_context=ctx, technical_rows=None))
        misaligned = [c.subjects for c in s.contradictions if c.code == "comparison_misaligned"]
        self.assertEqual(misaligned, [("SPY", "open"), ("SPY", "prev_close")])
        earlier = market_context_for(flat("META", date(2026, 9, 21), 78, "100") +
                                     flat("META", date(2026, 9, 22), 78, "101"), now=et(date(2026, 9, 22), 16, 5))
        s = synthesize(packet(market_context=earlier))  # Context session 09-22; technical bars end 09-23.
        self.assertIn(("market_context", "1d"), [c.subjects for c in s.contradictions
                                                 if c.code == "context_session_mismatch"])


class NewsTests(unittest.TestCase):
    def test_news_facts_are_non_directional_counts(self):
        s = synthesize(load("meta_real_shaped"))
        n = s.news
        self.assertEqual((n.availability, n.item_count), ("available", 2))
        self.assertEqual([(c.key, c.count) for c in n.counts_by_family], [("news", 1), ("sec", 1)])
        self.assertEqual([(c.key, c.count) for c in n.upstream_alert_decision_counts],
                         [("ALERT", 1), ("DISPLAY_ONLY", 1)])
        self.assertEqual((n.newest_publication_timestamp, n.newest_publication_age_seconds),
                         ("2026-09-23T13:05:00+00:00", 25200))
        self.assertEqual((n.newest_publication_date, n.newest_publication_date_age_days), ("2026-09-22", 1))
        text = json.dumps(s.to_dict()).lower()
        for word in ("sentiment", "bullish_news", "bearish_news", "ai_"):
            self.assertNotIn(word, text)

    def test_empty_available_vs_unavailable(self):
        empty = synthesize(load("empty_news"))
        self.assertEqual((empty.news.availability, empty.news.item_count), ("available_empty", 0))
        self.assertNotIn("news_unavailable", codes(empty))
        unavailable = synthesize(packet(news=None))
        self.assertEqual(unavailable.news.availability, "unavailable")
        self.assertIn("news_unavailable", codes(unavailable))


class IdentityAndDeterminismTests(unittest.TestCase):
    def test_same_packet_byte_identical_and_content_addressed(self):
        data = load("meta_real_shaped")
        a, b = synthesize(data), synthesize(deepcopy(data))
        self.assertEqual(canonical_json(a.to_dict()), canonical_json(b.to_dict()))
        self.assertEqual(a.synthesis_id, content_id(a.body()))
        self.assertRegex(a.synthesis_id, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(a.packet_ref.packet_id, data["packet_id"])
        self.assertNotIn("generated_at", canonical_json(a.to_dict()))

    def test_typed_and_json_round_trip_identical(self):
        typed = cases.CASES["meta_real_shaped"]()
        from_json = json.loads(canonical_json(typed.to_dict()))
        self.assertEqual(synthesize(typed).to_dict(), synthesize(from_json).to_dict())

    def test_dict_ordering_is_irrelevant(self):
        data = load("meta_real_shaped")

        def reversed_keys(value):
            if isinstance(value, dict):
                return {k: reversed_keys(value[k]) for k in reversed(list(value))}
            if isinstance(value, list):
                return [reversed_keys(v) for v in value]
            return value
        self.assertEqual(synthesize(reversed_keys(data)).synthesis_id, synthesize(data).synthesis_id)

    def test_news_order_is_canonicalized(self):
        """Reordered (and resealed) news items change only the packet identity, never the synthesized content."""
        data = load("meta_real_shaped")
        swapped = deepcopy(data)
        swapped["news"]["items"].reverse()
        swapped["packet_id"] = content_id({k: v for k, v in swapped.items() if k != "packet_id"})
        a, b = synthesize(data).to_dict(), synthesize(swapped).to_dict()
        for d in (a, b):
            for key in ("synthesis_id", "packet_ref", "provenance"):
                d.pop(key)
        self.assertEqual(a, b)

    def test_rules_version_changes_identity(self):
        data = load("meta_real_shaped")
        base = synthesize(data).synthesis_id
        with patch.object(rules, "RULES_VERSION", "phase7g-rules-test"):
            self.assertNotEqual(synthesize(data).synthesis_id, base)

    def test_immutable_and_input_not_mutated(self):
        data = load("all_bullish")
        before = deepcopy(data)
        s = synthesize(data)
        self.assertEqual(data, before)
        with self.assertRaises(FrozenInstanceError):
            s.synthesis_id = "x"
        with self.assertRaises(FrozenInstanceError):
            s.timeframes[0].state = "bearish_setup"
        typed = cases.CASES["all_bullish"]()
        typed_before = canonical_json(typed.to_dict())
        synthesize(typed)
        self.assertEqual(canonical_json(typed.to_dict()), typed_before)

    def test_no_forbidden_top_level_concepts(self):
        for name in cases.CASES:
            keys = set(synthesize(load(name)).to_dict())
            self.assertEqual(keys, {"synthesis_format_version", "synthesis_id", "rules_version", "packet_ref",
                                    "completeness", "timeframes", "timeframe_relations", "timeframe_alignment",
                                    "market_context", "news", "contradictions", "provenance"})
            self.assertFalse(keys & FORBIDDEN_TOP_LEVEL)


class ProvenanceTests(unittest.TestCase):
    def test_pointers_are_stable_identifiers(self):
        s = synthesize(load("meta_real_shaped"))
        self.assertIn("technical.1h.row.technical_state", s.timeframes[1].sources)
        self.assertTrue(all(p.startswith("news.items[") and ":" in p for p in s.news.sources))
        self.assertIn("market_context.context.comparisons[QQQ:prev_close].relative_return",
                      [p for ref in s.market_context.references for p in ref.sources])
        for c in s.contradictions:
            self.assertTrue(c.pointers)
        self.assertEqual((s.provenance.packet_id, s.provenance.rules_version),
                         (s.packet_ref.packet_id, "phase7g-rules-v1"))


if __name__ == "__main__":
    unittest.main()
