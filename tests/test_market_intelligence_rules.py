"""Phase 8A rules: pinned contract, sign profiles, opposition shape (exhaustive), attention codes."""
from dataclasses import fields
from itertools import product
import unittest

from evidence_synthesis import rules as sr
from market_intelligence import model as m
from market_intelligence import rules


class ContractTests(unittest.TestCase):
    def test_versions_and_supported_inputs(self):
        self.assertEqual((rules.INTELLIGENCE_FORMAT_VERSION, rules.RULES_VERSION), ("phase8-v1", "phase8-rules-v1"))
        self.assertEqual(rules.SUPPORTED_SYNTHESIS_FORMATS, {"phase7g-v1"})
        self.assertEqual(rules.SUPPORTED_SYNTHESIS_RULES, {"phase7g-rules-v1"})

    def test_exactly_13_top_level_fields(self):
        self.assertEqual([f.name for f in fields(m.MarketIntelligence)], [
            "intelligence_format_version", "intelligence_id", "rules_version", "synthesis_ref", "comparison",
            "evidence_coverage", "timeframe_structure", "market_context_alignment", "event_presence", "conflicts",
            "transitions", "attention", "provenance"])
        self.assertEqual([f.name for f in fields(m.SynthesisRef)], [
            "synthesis_id", "synthesis_format_version", "synthesis_rules_version", "packet_id", "symbol", "as_of"])
        self.assertEqual([f.name for f in fields(m.Provenance)], [
            "synthesis_id", "synthesis_format_version", "synthesis_rules_version", "packet_id", "domains_used"])

    def test_closed_enumerations(self):
        self.assertEqual(rules.OPPOSITION_SHAPES, ("none", "isolated_interval", "single_pair", "not_applicable"))
        self.assertEqual(rules.SIGN_PROFILES, ("all_positive", "all_negative", "all_zero", "mixed", "unavailable"))
        self.assertEqual(rules.ATTENTION_CATEGORIES, ("conflict", "gap", "presence"))
        self.assertEqual(rules.ATTENTION_CODES, {
            "timeframe_opposition_present": "conflict", "market_context_opposition_present": "conflict",
            "evidence_incomplete": "gap", "context_session_mismatch": "gap", "comparison_misaligned": "gap",
            "market_context_not_current": "gap", "news_unavailable": "gap", "sec_filing_present": "presence"})
        self.assertEqual(rules.CONFLICT_CODES, ("timeframe_opposition", "market_context_opposes_timeframe"))
        self.assertLessEqual(set(rules.CONFLICT_CODES), set(sr.CONTRADICTION_CODES))

    def test_freshness_vocabulary_is_upstream(self):
        """Only upstream Phase 7B statuses; market_closed and current are never 'not current'. No threshold."""
        upstream = {"current", "market_closed", "lagging", "no_data", "unknown"}
        self.assertEqual(rules.NOT_CURRENT_FRESHNESS, {"lagging", "no_data", "unknown"})
        self.assertLessEqual(rules.NOT_CURRENT_FRESHNESS, upstream)

    def test_sec_identity_version_matches_phase7c(self):
        from tests.test_evidence_packet import collection, packet
        from tests.test_evidence_packet_adapters import news, sec_event
        [item] = packet(news=collection(news(sec_event(), family="sec"))).to_dict()["news"]["items"]
        self.assertEqual(item["identity_version"], rules.SEC_IDENTITY_VERSION)

    def test_synthesis_vocabulary_reused(self):
        self.assertEqual((rules.INTERVALS, rules.PAIRS, rules.BASES), (sr.INTERVALS, sr.PAIRS, sr.BASES))


class SignProfileTests(unittest.TestCase):
    def test_table(self):
        cases = {
            (): "unavailable", ("unavailable",): "unavailable", ("positive",): "all_positive",
            ("negative", "negative"): "all_negative", ("zero", "unavailable"): "all_zero",
            ("positive", "negative"): "mixed", ("positive", "zero"): "mixed", ("zero", "negative"): "mixed",
        }
        for signs, expected in cases.items():
            self.assertEqual(rules.sign_profile(signs), expected, signs)


class OppositionShapeTests(unittest.TestCase):
    def test_exhaustive_over_directions(self):
        """All 4^3 direction combinations, against expectations written without calling the rule."""
        values = ("bullish", "bearish", "non_directional", "unavailable")
        seen = set()
        for combo in product(values, repeat=3):
            directions = dict(zip(rules.INTERVALS, combo))
            relations = {(a, b): sr.pair_relation(directions[a], directions[b]) for a, b in rules.PAIRS}
            pattern = sr.timeframe_pattern(combo)
            shape, isolated = rules.opposition_shape(pattern, directions, relations)
            seen.add(shape)
            if "unavailable" in combo:
                self.assertEqual((shape, isolated), ("not_applicable", None))
            elif not ("bullish" in combo and "bearish" in combo):
                self.assertEqual((shape, isolated), ("none", None))
            elif "non_directional" in combo:
                self.assertEqual((shape, isolated), ("single_pair", None))
            else:
                minority = min(("bullish", "bearish"), key=combo.count)
                self.assertEqual((shape, isolated), ("isolated_interval", rules.INTERVALS[combo.index(minority)]))
        self.assertEqual(seen, set(rules.OPPOSITION_SHAPES))


if __name__ == "__main__":
    unittest.main()
