"""Phase 8A builder: goldens, explicit semantics, attention, provenance, the 729 combinations, determinism, identity.

Every count here is a validation count, not a market statistic.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
from itertools import product
import json
import re
import unittest
from unittest.mock import patch

from evidence_synthesis import rules as sr
from evidence_synthesis.builder import synthesize
from evidence_synthesis.canonical import canonical_json, content_id
from market_intelligence import rules
from market_intelligence.builder import build
from tests import evidence_synthesis_corpus as corpus
from tests import market_intelligence_cases as cases
from tests.test_evidence_synthesis_replay import resolve as resolve_packet, with_states

TOP_LEVEL = ["attention", "comparison", "conflicts", "event_presence", "evidence_coverage",
             "intelligence_format_version", "intelligence_id", "market_context_alignment", "provenance",
             "rules_version", "synthesis_ref", "timeframe_structure", "transitions"]


def text(intelligence):
    return canonical_json(intelligence.to_dict()) + "\n"


def expected_text(name):
    return cases.intelligence_path(name).read_text(encoding="utf-8")


def codes(intelligence):
    return {a.code for a in intelligence.attention}


def resolve_synthesis(data, pointer):
    """True if a Phase 8 synthesis pointer names an existing location in the synthesis dict."""
    if pointer == "completeness":
        return True
    frames = {f["interval"]: f for f in data["timeframes"]}
    market, news = data["market_context"], data["news"]
    patterns = [
        (r"timeframes\[(1d|1h|5m)\]\.(\w+)", lambda g: g[2] in frames[g[1]]),
        (r"timeframe_relations\[(\w+):(\w+)\]\.relation",
         lambda g: any((x["first"], x["second"]) == (g[1], g[2]) for x in data["timeframe_relations"])),
        (r"timeframe_alignment\.pattern", lambda g: True),
        (r"market_context\.references\[([^:\]]+):(\w+)\]\.value_sign",
         lambda g: any((x["reference"], x["basis"]) == (g[1], g[2]) for x in market["references"])),
        (r"market_context\.relations\[(\w+):([^:\]]+):(\w+)\]\.relation",
         lambda g: any((x["interval"], x["reference"], x["basis"]) == (g[1], g[2], g[3]) for x in market["relations"])),
        (r"market_context\.(\w+)", lambda g: g[1] in market),
        (r"news\.counts_by_family\[(\w+)\]", lambda g: any(x["key"] == g[1] for x in news["counts_by_family"])),
        (r"news\.(\w+)", lambda g: g[1] in news),
        (r"contradictions\[(\w+):([^\]]*)\]",
         lambda g: any(c["code"] == g[1] and c["subjects"] == g[2].split(",") for c in data["contradictions"])),
    ]
    for pattern, check in patterns:
        match = re.fullmatch(pattern, pointer)
        if match:
            return check(match)
    return False


def pointer_sets(value):
    """Every (synthesis_pointers, packet_pointers) list found anywhere in an intelligence dict."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("synthesis_pointers", "packet_pointers"):
                yield key, child
            else:
                yield from pointer_sets(child)
    elif isinstance(value, list):
        for child in value:
            yield from pointer_sets(child)


class ContractAndGoldenTests(unittest.TestCase):
    def test_goldens_byte_exact(self):
        self.assertEqual(len(cases.names()), 25)
        for name in cases.names():
            with self.subTest(case=name):
                self.assertEqual(text(build(cases.synthesis(name))), expected_text(name))

    def test_top_level_contract_and_reserved_phase8b_fields(self):
        for name in cases.names():
            data = build(cases.synthesis(name)).to_dict()
            self.assertEqual(sorted(data), TOP_LEVEL)
            self.assertIsNone(data["comparison"])
            self.assertEqual(data["transitions"], [])
            self.assertEqual((data["intelligence_format_version"], data["rules_version"]),
                             ("phase8-v1", "phase8-rules-v1"))

    def test_synthesis_ref_copied(self):
        syn = cases.synthesis("meta_real_shaped")
        ref = build(syn).synthesis_ref
        self.assertEqual((ref.synthesis_id, ref.synthesis_format_version, ref.synthesis_rules_version, ref.packet_id,
                          ref.symbol, ref.as_of),
                         (syn["synthesis_id"], "phase7g-v1", "phase7g-rules-v1", syn["packet_ref"]["packet_id"],
                          "META", "2026-09-23T20:05:00+00:00"))


class StructureTests(unittest.TestCase):
    def test_required_shapes(self):
        expect = {
            "all_bullish": ("all_bullish", "none", None, []),
            "all_bearish": ("all_bearish", "none", None, []),
            "non_directional": ("all_non_directional", "none", None, []),
            "meta_real_shaped": ("partially_directional", "none", None, []),
            "higher_aligned_5m_opposed": ("opposed", "isolated_interval", "5m", [("1h", "5m"), ("1d", "5m")]),
            "lower_aligned_1d_opposed": ("opposed", "isolated_interval", "1d", [("1d", "1h"), ("1d", "5m")]),
            "single_pair_opposition": ("opposed", "single_pair", None, [("1d", "5m")]),
            "unavailable_timeframe": ("incomplete", "not_applicable", None, []),
            "insufficient_data_state": ("incomplete", "not_applicable", None, []),
        }
        for name, (pattern, shape, isolated, pairs) in expect.items():
            with self.subTest(case=name):
                s = build(cases.synthesis(name)).timeframe_structure
                self.assertEqual((s.pattern, s.opposition_shape, s.isolated_interval, list(s.opposing_pairs)),
                                 (pattern, shape, isolated, pairs))

    def test_coverage_distinguishes_unavailable_insufficient_available_empty(self):
        c = build(cases.synthesis("insufficient_data_state")).evidence_coverage
        self.assertEqual((c.available_intervals, c.insufficient_data_intervals, c.unavailable_intervals),
                         (("1d", "5m"), ("1h",), ()))
        c = build(cases.synthesis("missing_1d_5m")).evidence_coverage
        self.assertEqual((c.technical_status, c.available_intervals, [(u.interval, u.reason) for u in c.unavailable_intervals]),
                         ("partial", ("1h",), [("1d", "not_supplied"), ("5m", "not_supplied")]))
        self.assertEqual(build(cases.synthesis("empty_news")).evidence_coverage.news_state, "available_empty")
        self.assertEqual(build(cases.synthesis("news_unavailable")).evidence_coverage.news_state, "unavailable")
        c = build(cases.synthesis("benchmark_unavailable")).evidence_coverage
        self.assertEqual([(u.reference, u.basis, u.reasons) for u in c.unavailable_references],
                         [("SPY", "prev_close", ("session_mismatch",)), ("SPY", "open", ("session_mismatch",))])
        c = build(cases.synthesis("market_context_unavailable")).evidence_coverage
        self.assertEqual((c.market_context_status, c.available_references, c.unavailable_references),
                         ("unavailable", (), ()))


class MarketContextTests(unittest.TestCase):
    def test_own_and_relative_profiles_kept_apart(self):
        a = build(cases.synthesis("context_session_mismatch")).market_context_alignment
        self.assertEqual([(s.basis, s.value_sign) for s in a.own_return_signs],
                         [("prev_close", "positive"), ("open", "zero")])
        self.assertEqual((a.own_return_profile, a.relative_return_signs, a.relative_return_profile),
                         ("mixed", (), "unavailable"))
        for name, profile in (("positive_market_return", "all_positive"), ("zero_market_return", "all_zero"),
                              ("all_bullish", "all_negative")):
            a = build(cases.synthesis(name)).market_context_alignment
            self.assertEqual((a.own_return_profile, a.relative_return_profile), (profile, profile))
        a = build(cases.synthesis("benchmark_misaligned")).market_context_alignment
        self.assertEqual((a.own_return_profile, a.relative_return_profile), ("all_positive", "unavailable"))

    def test_per_interval_tallies(self):
        a = build(cases.synthesis("context_opposes_one_timeframe")).market_context_alignment
        rows = {row.interval: row for row in a.by_interval}
        self.assertEqual((rows["1d"].oppose_count, rows["1d"].agree_count), (4, 0))
        self.assertEqual([(k.reference, k.basis) for k in rows["1d"].opposing_references],
                         [("self", "prev_close"), ("self", "open"), ("QQQ", "prev_close"), ("QQQ", "open")])
        self.assertEqual((rows["1h"].non_directional_count, rows["1h"].oppose_count), (4, 0))
        a = build(cases.synthesis("benchmark_unavailable")).market_context_alignment
        self.assertEqual({row.interval: row.unavailable_count for row in a.by_interval}, {"1d": 2, "1h": 2, "5m": 2})
        a = build(cases.synthesis("market_context_unavailable")).market_context_alignment
        self.assertEqual((a.available, a.by_interval, a.freshness_status), (False, (), None))

    def test_context_never_changes_technical_structure(self):
        for name in ("all_bullish", "positive_market_return", "zero_market_return"):
            s = build(cases.synthesis(name)).timeframe_structure
            self.assertEqual((s.pattern, s.opposition_shape, s.directional_intervals),
                             ("all_bullish", "none", ("1d", "1h", "5m")))


class EventPresenceTests(unittest.TestCase):
    def test_counts_and_recency(self):
        e = build(cases.synthesis("meta_real_shaped")).event_presence
        self.assertEqual((e.news_state, e.item_count), ("available", 2))
        self.assertEqual([(c.key, c.count) for c in e.counts_by_family], [("news", 1), ("sec", 1)])
        self.assertEqual([(c.key, c.count) for c in e.counts_by_relevance], [("direct", 2), ("related_only", 0)])
        self.assertEqual((e.newest_publication.timestamp, e.newest_age_seconds, e.oldest_age_seconds),
                         ("2026-09-23T13:05:00+00:00", 25200, 25200))
        self.assertEqual((e.newest_publication.date, e.newest_date_age_days), ("2026-09-22", 1))
        empty = build(cases.synthesis("empty_news")).event_presence
        self.assertEqual((empty.item_count, empty.newest_publication.timestamp, empty.oldest_age_seconds),
                         (0, None, None))

    def test_no_news_direction(self):
        text_ = json.dumps(build(cases.synthesis("meta_real_shaped")).event_presence.to_dict()).lower()
        for word in ("sentiment", "bullish", "bearish", "catalyst", "important", "positive", "negative"):
            self.assertNotIn(word, text_)


class ConflictTests(unittest.TestCase):
    def test_conflicts_are_the_opposition_contradictions_only(self):
        for name in cases.names():
            syn = cases.synthesis(name)
            intel = build(syn)
            expected = [(c["code"], tuple(c["subjects"])) for c in syn["contradictions"]
                        if c["code"] in ("timeframe_opposition", "market_context_opposes_timeframe")]
            self.assertEqual([(c.code, c.subjects) for c in intel.conflicts], sorted(expected), name)
            for conflict, contradiction in zip(intel.conflicts, [c for c in syn["contradictions"]
                                                                 if c["code"] in rules.CONFLICT_CODES]):
                self.assertEqual(list(conflict.packet_pointers), contradiction["pointers"])

    def test_mixed_state_and_mixed_signs_are_not_conflicts(self):
        self.assertEqual(build(cases.synthesis("mixed_all")).conflicts, ())
        self.assertEqual(build(cases.synthesis("range_all")).conflicts, ())
        mixed_signs = build(cases.synthesis("context_session_mismatch"))
        self.assertEqual(mixed_signs.market_context_alignment.own_return_profile, "mixed")
        self.assertEqual(mixed_signs.conflicts, ())


class AttentionTests(unittest.TestCase):
    def test_each_code_independently(self):
        expect = {
            "timeframe_opposition_present": ("higher_aligned_5m_opposed", ("1d", "5m")),
            "market_context_opposition_present": ("context_opposes_one_timeframe", ("1d",)),
            "evidence_incomplete": ("unavailable_timeframe", ("technical", "5m")),
            "context_session_mismatch": ("context_session_mismatch", ("market_context", "1d")),
            "comparison_misaligned": ("benchmark_misaligned", ("SPY", "open")),
            "market_context_not_current": ("meta_real_shaped", ("market_context", "lagging")),
            "news_unavailable": ("news_unavailable", ("news",)),
            "sec_filing_present": ("meta_real_shaped", ("sec",)),
        }
        self.assertEqual(set(expect), set(rules.ATTENTION_CODES))
        for code, (name, subjects) in expect.items():
            with self.subTest(code=code):
                flags = [a for a in build(cases.synthesis(name)).attention if a.code == code]
                self.assertIn(subjects, [a.subjects for a in flags])
                self.assertTrue(all(a.category == rules.ATTENTION_CODES[code] for a in flags))
                self.assertTrue(all(a.synthesis_pointers for a in flags))

    def test_negative_cases(self):
        self.assertNotIn("market_context_not_current", codes(build(cases.synthesis("context_session_mismatch"))))
        self.assertEqual(build(cases.synthesis("context_session_mismatch")).market_context_alignment.freshness_status,
                         "market_closed")
        self.assertNotIn("sec_filing_present", codes(build(cases.synthesis("empty_news"))))
        self.assertNotIn("news_unavailable", codes(build(cases.synthesis("empty_news"))))
        quiet = build(cases.synthesis("mixed_all"))
        self.assertEqual({a.category for a in quiet.attention} & {"conflict"}, set())
        misaligned = build(cases.synthesis("benchmark_misaligned"))
        self.assertNotIn("evidence_incomplete", codes(misaligned))  # Misaligned is its own code, not duplicated.
        many = build(cases.synthesis("all_bullish"))
        self.assertEqual([a.subjects for a in many.attention if a.code == "market_context_opposition_present"],
                         [("1d",), ("1h",), ("5m",)])  # One flag per timeframe, not per reference.

    def test_canonical_order_and_no_ranking_fields(self):
        for name in cases.names():
            attention = build(cases.synthesis(name)).attention
            keys = [(a.category, a.code, a.subjects) for a in attention]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(len(keys), len(set(keys)))
            for a in attention:
                self.assertEqual(set(a.to_dict()), {"category", "code", "subjects", "synthesis_pointers",
                                                    "packet_pointers"})


class ProvenanceTests(unittest.TestCase):
    def test_every_pointer_resolves(self):
        for name in cases.names():
            with self.subTest(case=name):
                syn, packet = cases.synthesis(name), cases.packet(name)
                data = build(syn).to_dict()
                seen = 0
                for kind, pointers in pointer_sets(data):
                    for pointer in pointers:
                        seen += 1
                        resolved = resolve_synthesis(syn, pointer) if kind == "synthesis_pointers" \
                            else resolve_packet(packet, pointer)
                        self.assertTrue(resolved, (kind, pointer))
                self.assertGreater(seen, 0)
                for conflict in data["conflicts"]:
                    self.assertTrue(conflict["synthesis_pointers"] and conflict["packet_pointers"])

    def test_provenance_block(self):
        syn = cases.synthesis("meta_real_shaped")
        p = build(syn).provenance
        self.assertEqual((p.synthesis_id, p.packet_id, p.synthesis_rules_version, p.domains_used),
                         (syn["synthesis_id"], syn["packet_ref"]["packet_id"], "phase7g-rules-v1",
                          ("market_context", "technical", "news")))


class ExhaustiveTests(unittest.TestCase):
    # sha256 over the 729 intelligence_ids (one per line, product order). A determinism anchor, not a statistic.
    DIGEST = "afb8579c46bfe8e726335716d80ac4f7dfb3a4821c7c97639a4c2c448c979a9e"

    @classmethod
    def setUpClass(cls):
        base = cases.packet("all_bullish")
        cls.results = [(combo, build(synthesize(with_states(base, combo)).to_dict()))
                       for combo in product(sr.TECHNICAL_STATES, repeat=3)]

    def test_shapes_and_coverage(self):
        for combo, intel in self.results:
            dirs = [sr.state_direction(s) for s in combo]
            s, c = intel.timeframe_structure, intel.evidence_coverage
            if "unavailable" in dirs:
                expected = "not_applicable"
            elif not ("bullish" in dirs and "bearish" in dirs):
                expected = "none"
            else:
                expected = "single_pair" if "non_directional" in dirs else "isolated_interval"
            self.assertEqual(s.opposition_shape, expected, combo)
            self.assertEqual(c.insufficient_data_intervals,
                             tuple(i for i, x in zip(rules.INTERVALS, combo) if x == "insufficient_data"))
            self.assertEqual(c.available_intervals,
                             tuple(i for i, x in zip(rules.INTERVALS, combo) if x != "insufficient_data"))
            self.assertIsNone(intel.comparison)
            self.assertEqual(intel.transitions, ())

    def test_summary_counts(self):
        """Validation counts only (3 bullish, 3 bearish, 2 non-directional, 1 insufficient_data state)."""
        shapes = Counter(i.timeframe_structure.opposition_shape for _, i in self.results)
        self.assertEqual(shapes, {"not_applicable": 217, "none": 242, "isolated_interval": 162, "single_pair": 108})

    def test_ids_unique_and_digest(self):
        ids = [i.intelligence_id for _, i in self.results]
        self.assertEqual(len(set(ids)), 729)
        self.assertEqual(hashlib.sha256("\n".join(ids).encode()).hexdigest(), self.DIGEST)


class DeterminismAndIdentityTests(unittest.TestCase):
    def test_100_repetitions(self):
        for name in cases.names():
            with self.subTest(case=name):
                raw = canonical_json(cases.synthesis(name))
                self.assertEqual({text(build(json.loads(raw))) for _ in range(100)}, {expected_text(name)})

    def test_typed_and_dict_agree(self):
        for name in corpus.names():
            with self.subTest(case=name):
                typed = synthesize(cases.packet(name))
                forms = (typed, typed.to_dict(), json.loads(canonical_json(typed.to_dict())), cases.synthesis(name))
                self.assertEqual({text(build(f)) for f in forms}, {expected_text(name)})

    def test_identity(self):
        syn = cases.synthesis("meta_real_shaped")
        intel = build(syn)
        self.assertEqual(intel.intelligence_id, content_id(intel.body()))
        self.assertRegex(intel.intelligence_id, r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("generated_at", text(intel))
        with patch.object(rules, "RULES_VERSION", "phase8-rules-v2"):
            self.assertNotEqual(build(syn).intelligence_id, intel.intelligence_id)
        other = cases.synthesis("meta_real_shaped")
        other["provenance"]["source_domains"] = ["market_context", "technical", "news"]
        self.assertEqual(build(other).intelligence_id, intel.intelligence_id)
        changed = deepcopy(syn)
        changed["packet_ref"]["packet_id"] = changed["provenance"]["packet_id"] = "sha256:" + "1" * 64
        changed["synthesis_id"] = content_id({k: v for k, v in changed.items() if k != "synthesis_id"})
        self.assertNotEqual(build(changed).intelligence_id, intel.intelligence_id)

    def test_dict_key_order_irrelevant(self):
        def reversed_keys(value):
            if isinstance(value, dict):
                return {k: reversed_keys(value[k]) for k in reversed(list(value))}
            if isinstance(value, list):
                return [reversed_keys(v) for v in value]
            return value
        for name in cases.names():
            self.assertEqual(text(build(reversed_keys(cases.synthesis(name)))), expected_text(name))

    def test_immutable_and_no_floats(self):
        intel = build(cases.synthesis("meta_real_shaped"))
        with self.assertRaises(FrozenInstanceError):
            intel.intelligence_id = "x"
        with self.assertRaises(FrozenInstanceError):
            intel.timeframe_structure.pattern = "x"
        stack = [intel.to_dict()]
        while stack:
            value = stack.pop()
            self.assertNotIsInstance(value, float)
            if isinstance(value, dict):
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)


if __name__ == "__main__":
    unittest.main()
