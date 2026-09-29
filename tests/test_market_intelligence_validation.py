"""Phase 8A validation: fail closed on invalid or resealed-inconsistent syntheses; incomplete evidence is valid."""
from copy import deepcopy
import unittest

from evidence_synthesis.canonical import content_id
from market_intelligence.builder import build
from market_intelligence.validation import MarketIntelligenceInputError, validated_synthesis
from tests import market_intelligence_cases as cases


def load(name="meta_real_shaped"):
    return cases.synthesis(name)


def resealed(data):
    data["synthesis_id"] = content_id({k: v for k, v in data.items() if k != "synthesis_id"})
    return data


def _frame(d, i):
    return d["timeframes"][i]


def _ctx(d):
    return d["market_context"]


def _swap_references(d):
    refs = _ctx(d)["references"]
    refs[0], refs[1] = refs[1], refs[0]


# (label, case, mutation, reseal, exact message). reseal=False keeps the stale synthesis_id (tampering).
INVALID = [
    ("wrong synthesis_id", "meta_real_shaped", lambda d: d.update(synthesis_id="sha256:" + "0" * 64), False,
     "synthesis_id does not match the synthesis body (tampered or corrupt)"),
    ("modified body, unchanged id", "meta_real_shaped",
     lambda d: _frame(d, 0).update(state="bearish_setup", state_direction="bearish"), False,
     "synthesis_id does not match the synthesis body (tampered or corrupt)"),
    ("malformed synthesis_id", "meta_real_shaped", lambda d: d.update(synthesis_id="md5:x"), False,
     "synthesis_id is malformed"),
    ("unsupported format", "meta_real_shaped", lambda d: d.update(synthesis_format_version="phase7g-v2"), True,
     "unsupported synthesis format version"),
    ("unsupported rules", "meta_real_shaped", lambda d: d.update(rules_version="phase7g-rules-v2"), True,
     "unsupported synthesis rules version"),
    ("missing key", "meta_real_shaped", lambda d: d.pop("news"), True,
     "synthesis must have exactly the 12 phase7g-v1 top-level keys"),
    ("extra key", "meta_real_shaped", lambda d: d.update(signal="x"), True,
     "synthesis must have exactly the 12 phase7g-v1 top-level keys"),
    ("NaN anywhere", "meta_real_shaped", lambda d: _frame(d, 0).update(age_seconds=float("nan")), False,
     "synthesis body is not canonical JSON"),
    ("naive as_of", "meta_real_shaped", lambda d: d["packet_ref"].update(as_of="2026-09-23T20:05:00"), True,
     "packet_ref.as_of must be timezone-aware"),
    ("packet_ref extra key", "meta_real_shaped", lambda d: d["packet_ref"].update(x=1), True,
     "packet_ref has unexpected keys"),
    ("timeframe order", "meta_real_shaped", lambda d: d["timeframes"].reverse(), True,
     "timeframes must be exactly 1d, 1h, 5m in order"),
    ("resealed wrong state_direction", "meta_real_shaped", lambda d: _frame(d, 0).update(state_direction="bearish"),
     True, "timeframes[1d].state_direction is inconsistent with its state"),
    ("resealed unknown state", "meta_real_shaped", lambda d: _frame(d, 1).update(state="strong_buy"), True,
     "timeframes[1h].state is not supported"),
    ("resealed wrong age", "meta_real_shaped", lambda d: _frame(d, 2).update(age_seconds=1), True,
     "timeframes[5m].age_seconds is inconsistent"),
    ("unavailable frame with facts", "unavailable_timeframe", lambda d: _frame(d, 2).update(state="range"), True,
     "timeframes[5m] is unavailable but carries row facts"),
    ("resealed wrong relation", "meta_real_shaped", lambda d: d["timeframe_relations"][0].update(relation="agree"),
     True, "timeframe_relations[1d:1h].relation is inconsistent with the directions"),
    ("relation order", "meta_real_shaped", lambda d: d["timeframe_relations"].reverse(), True,
     "timeframe_relations must follow the pair order (1d,1h), (1h,5m), (1d,5m)"),
    ("resealed wrong pattern", "meta_real_shaped", lambda d: d["timeframe_alignment"].update(pattern="all_bullish"),
     True, "timeframe_alignment.pattern is inconsistent with the directions"),
    ("resealed wrong alignment count", "meta_real_shaped", lambda d: d["timeframe_alignment"].update(bullish=3),
     True, "timeframe_alignment.bullish is inconsistent"),
    ("malformed context relation", "meta_real_shaped", lambda d: _ctx(d)["relations"][0].update(relation="maybe"),
     True, "market_context relation is not supported"),
    ("resealed wrong context relation", "meta_real_shaped",
     lambda d: _ctx(d)["relations"][0].update(relation="agree"), True,
     "market_context relation is inconsistent with direction and sign"),
    ("missing context relation", "meta_real_shaped", lambda d: _ctx(d)["relations"].pop(), True,
     "market_context relations must cover every timeframe x reference in order"),
    ("reference order", "meta_real_shaped", _swap_references, True,
     "market_context references are not in canonical order"),
    ("unknown value_sign", "meta_real_shaped", lambda d: _ctx(d)["references"][0].update(value_sign="up"), True,
     "market_context reference value_sign is not supported"),
    ("unavailable context with facts", "market_context_unavailable",
     lambda d: _ctx(d).update(freshness_status="current"), True, "unavailable market_context must not carry facts"),
    ("contradiction removed", "higher_aligned_5m_opposed", lambda d: d["contradictions"].pop(), True,
     "contradictions are inconsistent with the synthesis facts"),
    ("contradiction fabricated", "all_bearish", lambda d: d["contradictions"].append(
        {"code": "timeframe_opposition", "subjects": ["1d", "1h"], "pointers": ["x"]}), True,
     "contradictions are inconsistent with the synthesis facts"),
    ("mixed made a contradiction", "mixed_all", lambda d: d["contradictions"].append(
        {"code": "timeframe_unavailable", "subjects": ["1h"], "pointers": ["x"]}), True,
     "contradictions are inconsistent with the synthesis facts"),
    ("contradiction unknown code", "meta_real_shaped", lambda d: d["contradictions"][0].update(code="technical_state_mixed"),
     True, "contradiction code is not supported"),
    ("contradiction unsorted", "meta_real_shaped", lambda d: d["contradictions"].reverse(), True,
     "contradictions must be sorted by (code, subjects) and unique"),
    ("contradiction without pointers", "meta_real_shaped", lambda d: d["contradictions"][0].update(pointers=[]), True,
     "contradiction pointers must not be empty"),
    ("completeness inconsistent", "meta_real_shaped", lambda d: d["completeness"].update(technical="partial"), True,
     "completeness.technical is inconsistent with the timeframes"),
    ("news count inconsistent", "meta_real_shaped", lambda d: d["news"].update(item_count=3), True,
     "news.counts_by_family does not add up to item_count"),
    ("news sources unsorted", "meta_real_shaped", lambda d: d["news"]["sources"].reverse(), True,
     "news.sources must be sorted, unique and one per item"),
    ("boolean count", "meta_real_shaped", lambda d: d["news"]["counts_by_family"][0].update(count=True), True,
     "news.counts_by_family.count must be an integer"),
    ("provenance mismatch", "meta_real_shaped", lambda d: d["provenance"].update(rules_version="other"), True,
     "provenance is inconsistent with the synthesis"),
]


class InvalidTests(unittest.TestCase):
    def test_tamper_matrix_fails_closed_with_stable_messages(self):
        for label, name, fn, reseal, message in INVALID:
            with self.subTest(case=label):
                data = load(name)
                fn(data)
                if reseal:
                    data = resealed(data)
                before = deepcopy(data)
                with self.assertRaises(MarketIntelligenceInputError) as caught:
                    build(data)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(data, before)  # Never repaired.

    def test_non_synthesis_inputs(self):
        from tests.test_evidence_packet import packet
        for value in (None, [], "synthesis", 1, packet()):
            with self.assertRaises(MarketIntelligenceInputError) as caught:
                build(value)
            self.assertEqual(str(caught.exception), "input must be an EvidenceSynthesis or its canonical dict")

    def test_packet_dict_is_not_a_synthesis(self):
        with self.assertRaises(MarketIntelligenceInputError):
            build(cases.packet("all_bullish"))


class IncompleteButValidTests(unittest.TestCase):
    def test_incomplete_syntheses_build(self):
        for name in ("unavailable_timeframe", "missing_1h", "missing_1d", "missing_1d_5m", "technical_unavailable",
                     "market_context_unavailable", "empty_news", "news_unavailable", "benchmark_unavailable",
                     "insufficient_data_state", "market_context_and_news_unavailable"):
            with self.subTest(case=name):
                data = load(name)
                self.assertIs(validated_synthesis(data), data)
                self.assertEqual(build(data).to_dict(), build(deepcopy(data)).to_dict())

    def test_validation_never_mutates(self):
        data = load()
        before = deepcopy(data)
        validated_synthesis(data)
        build(data)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
