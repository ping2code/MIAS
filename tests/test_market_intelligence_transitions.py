"""Phase 8B: optional previous synthesis, comparison and deterministic transitions.

Transitions describe differences between two cutoff-safe EvidenceSynthesis objects. They never predict.
"""
from copy import deepcopy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evidence_synthesis.builder import synthesize
from evidence_synthesis.canonical import canonical_json, content_id
from market_intelligence import builder as builder_module
from market_intelligence import rules
from market_intelligence.builder import build
from market_intelligence.validation import MarketIntelligenceInputError
from tests import evidence_synthesis_corpus as corpus
from tests import market_intelligence_cases as current_cases
from tests import market_intelligence_transition_cases as cases
from tests.test_evidence_packet import AS_OF, packet
from tests.test_market_intelligence_builder import resolve_synthesis
from tests.test_market_intelligence_isolation import FORBIDDEN, FORBIDDEN_PHRASES, sealed, words

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURRENT_STATE = ("synthesis_ref", "evidence_coverage", "timeframe_structure", "market_context_alignment",
                 "event_presence", "conflicts", "attention", "provenance")
MIRROR = {"opposition_appeared": "opposition_resolved", "gap_appeared": "gap_resolved",
          "entered_alignment": "exited_alignment", "domain_became_available": "domain_became_unavailable",
          "news_identities_added": "news_identities_removed"}
MIRROR.update({v: k for k, v in list(MIRROR.items())})


def text(intelligence):
    return canonical_json(intelligence.to_dict()) + "\n"


def expected_text(name):
    return cases.intelligence_path(name).read_text(encoding="utf-8")


def resealed_synthesis(data):
    data["synthesis_id"] = content_id({k: v for k, v in data.items() if k != "synthesis_id"})
    return data


def later_packet(name):
    """A corpus packet re-cut at LATER (every input is already at or before it), resealed."""
    data = json.loads(corpus.packet_path(name).read_text(encoding="utf-8"))
    data["as_of"] = cases.LATER.isoformat()
    data["packet_id"] = content_id({k: v for k, v in data.items() if k != "packet_id"})
    return data


def later(name):
    return synthesize(later_packet(name)).to_dict()


def earlier(name):
    return json.loads(corpus.synthesis_path(name).read_text(encoding="utf-8"))


def keyed(transitions):
    return [(t.code, t.subjects) for t in transitions]


def mirrored(transition):
    """The transition expected when the two inputs are swapped."""
    code, subjects = transition.code, transition.subjects
    if code in MIRROR:
        return MIRROR[code], subjects if code.startswith(("opposition", "gap", "news")) else \
            (subjects[0], subjects[2], subjects[1]) if code.startswith("domain") else (subjects[1], subjects[0])
    if code in ("pattern_changed",):
        return code, (subjects[1], subjects[0])
    if code in ("timeframe_state_changed", "timeframe_direction_changed"):
        return code, (subjects[0], subjects[2], subjects[1])
    if code == "reference_sign_changed":
        return code, (subjects[0], subjects[1], subjects[3], subjects[2])
    raise AssertionError(code)


def resolve(previous, current, pointer):
    prefix, _, path = pointer.partition(":")
    data = {"previous": previous, "current": current}[prefix]
    match = re.fullmatch(r"completeness\.(\w+)", path)
    if match:
        return match[1] in data["completeness"]
    match = re.fullmatch(r"news\.sources\[([^\]]+)\]", path)
    if match:
        return f"news.items[{match[1]}]" in data["news"]["sources"]
    return resolve_synthesis(data, path)


class CurrentOnlyLockTests(unittest.TestCase):
    def test_phase8a_goldens_byte_identical(self):
        for name in current_cases.names():
            with self.subTest(case=name):
                intel = build(current_cases.synthesis(name))
                self.assertEqual(text(intel), current_cases.intelligence_path(name).read_text(encoding="utf-8"))
                self.assertEqual(build(current_cases.synthesis(name), None).to_dict(), intel.to_dict())
                self.assertIsNone(intel.comparison)
                self.assertEqual(intel.transitions, ())

    def test_versions_unchanged(self):
        self.assertEqual((rules.INTELLIGENCE_FORMAT_VERSION, rules.RULES_VERSION), ("phase8-v1", "phase8-rules-v1"))


class ComparisonValidationTests(unittest.TestCase):
    def assertInvalid(self, current, previous, message):
        with self.assertRaises(MarketIntelligenceInputError) as caught:
            build(current, previous)
        self.assertEqual(str(caught.exception), message)

    def test_valid_pair(self):
        intel = build(cases.current("no_change"), cases.previous("no_change"))
        c = intel.comparison
        self.assertEqual((c.status, c.reasons, c.elapsed_seconds), ("comparable", (), 300))
        self.assertEqual((c.previous_ref.synthesis_id, c.previous_ref.as_of, c.previous_ref.symbol),
                         (cases.previous("no_change")["synthesis_id"], "2026-09-23T20:05:00+00:00", "META"))
        self.assertEqual(len(intel.to_dict()), 13)

    def test_invalid_pairs_fail_closed(self):
        meta = earlier("meta_real_shaped")
        nvda = synthesize(packet(symbol="NVDA")).to_dict()
        self.assertInvalid(later("meta_real_shaped"), nvda,
                           "previous synthesis symbol does not match the current synthesis")
        self.assertInvalid(earlier("all_bullish"), meta,
                           "previous synthesis as_of must be earlier than the current synthesis as_of")
        self.assertInvalid(meta, later("meta_real_shaped"),
                           "previous synthesis as_of must be earlier than the current synthesis as_of")
        self.assertInvalid(meta, deepcopy(meta), "previous and current synthesis are the same synthesis")

    def test_previous_is_fully_validated(self):
        tampered = earlier("meta_real_shaped")
        tampered["timeframes"][0]["state_direction"] = "bearish"
        self.assertInvalid(later("meta_real_shaped"), tampered,
                           "synthesis_id does not match the synthesis body (tampered or corrupt)")
        self.assertInvalid(later("meta_real_shaped"), resealed_synthesis(tampered),
                           "timeframes[1d].state_direction is inconsistent with its state")
        self.assertInvalid(later("meta_real_shaped"), "previous",
                           "input must be an EvidenceSynthesis or its canonical dict")


class ComparabilityTests(unittest.TestCase):
    def test_version_mismatch_is_not_comparable(self):
        """Only reachable once more than one synthesis version is supported; exercised with a patched set."""
        current = cases.current("entered_alignment")
        for field, provenance_field, supported, reason in (
                ("rules_version", "rules_version", "SUPPORTED_SYNTHESIS_RULES", "synthesis_rules_version_mismatch"),
                ("synthesis_format_version", "synthesis_format_version", "SUPPORTED_SYNTHESIS_FORMATS",
                 "synthesis_format_version_mismatch")):
            with self.subTest(field=field):
                previous = cases.previous("entered_alignment")
                previous[field] = previous["provenance"][provenance_field] = previous[field] + "-other"
                previous = resealed_synthesis(previous)
                with self.assertRaises(MarketIntelligenceInputError):
                    build(current, previous)  # Unsupported today: fail closed.
                with patch.object(rules, supported, getattr(rules, supported) | {previous[field]}):
                    intel = build(current, previous)
                self.assertEqual((intel.comparison.status, intel.comparison.reasons, intel.transitions),
                                 ("not_comparable", (reason,), ()))


class TransitionTests(unittest.TestCase):
    def test_goldens_byte_exact(self):
        self.assertEqual(len(cases.names()), 18)
        for name in cases.names():
            with self.subTest(pair=name):
                self.assertEqual(text(build(cases.current(name), cases.previous(name))), expected_text(name))

    def test_every_code_independently(self):
        expect = {
            "timeframe_state_changed": ("state_changed_same_direction", ("1d", "bullish_setup", "bullish_momentum")),
            "timeframe_direction_changed": ("opposition_appeared", ("5m", "bullish", "bearish")),
            "pattern_changed": ("opposition_appeared", ("all_bullish", "opposed")),
            "entered_alignment": ("entered_alignment", ("partially_directional", "all_bullish")),
            "exited_alignment": ("exited_alignment", ("all_bullish", "partially_directional")),
            "opposition_appeared": ("opposition_appeared", ("timeframe_opposition", "1d", "5m")),
            "opposition_resolved": ("opposition_resolved", ("timeframe_opposition", "1h", "5m")),
            "gap_appeared": ("timeframe_gap_appeared", ("timeframe_unavailable", "5m")),
            "gap_resolved": ("context_became_available", ("market_context_unavailable", "market_context")),
            "domain_became_available": ("news_became_available", ("news", "unavailable", "available")),
            "domain_became_unavailable": ("context_became_unavailable", ("market_context", "available", "unavailable")),
            "reference_sign_changed": ("reference_signs_changed", ("QQQ", "open", "negative", "positive")),
            "news_identities_added": ("news_identities_added", None),
            "news_identities_removed": ("news_identities_removed", None),
        }
        self.assertEqual(set(expect), set(rules.TRANSITION_CODES))
        for code, (name, subjects) in expect.items():
            with self.subTest(code=code):
                found = [t for t in build(cases.current(name), cases.previous(name)).transitions if t.code == code]
                self.assertTrue(found)
                if subjects:
                    self.assertIn(subjects, [t.subjects for t in found])
                for t in found:
                    self.assertTrue(t.previous_pointers or t.current_pointers)

    def test_exact_small_cases(self):
        self.assertEqual(build(cases.current("no_change"), cases.previous("no_change")).transitions, ())
        self.assertEqual(keyed(build(cases.current("state_changed_same_direction"),
                                     cases.previous("state_changed_same_direction")).transitions),
                         [("timeframe_state_changed", ("1d", "bullish_setup", "bullish_momentum")),
                          ("timeframe_state_changed", ("1h", "bullish_momentum", "bullish_setup"))])
        insufficient = build(cases.current("insufficient_data_appeared"), cases.previous("insufficient_data_appeared"))
        self.assertIn(("timeframe_state_changed", ("1h", "mixed", "insufficient_data")), keyed(insufficient.transitions))
        self.assertIn(("gap_appeared", ("timeframe_unavailable", "1h")), keyed(insufficient.transitions))
        self.assertNotIn("domain_became_unavailable", {t.code for t in insufficient.transitions})
        both_aligned = build(synthesize(packet(as_of=cases.LATER, technical_rows=corpus.states(
            "bearish_setup", "bearish_momentum", "breakdown_watch"))).to_dict(), earlier("all_bullish"))
        codes = {t.code for t in both_aligned.transitions}
        self.assertIn("pattern_changed", codes)
        self.assertFalse(codes & {"entered_alignment", "exited_alignment"})  # all_bullish -> all_bearish.

    def test_news_identity_wording_and_subjects(self):
        added = build(cases.current("news_identities_added"), cases.previous("news_identities_added"))
        [t] = [t for t in added.transitions if t.code == "news_identities_added"]
        self.assertEqual(t.subjects[0], "2")
        self.assertEqual(list(t.subjects[1:]), sorted(t.subjects[1:]))
        self.assertEqual((t.previous_pointers, len(t.current_pointers)), ((), 2))
        text_ = json.dumps(added.to_dict()["transitions"]).lower()
        for word in ("expired", "invalid", "relevant", "catalyst", "positive_news", "negative_news", "stronger"):
            self.assertNotIn(word, text_)

    def test_ordering_and_uniqueness(self):
        for name in cases.names():
            transitions = build(cases.current(name), cases.previous(name)).transitions
            keys = keyed(transitions)
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(len(keys), len(set(keys)))
            self.assertLessEqual({t.code for t in transitions}, set(rules.TRANSITION_CODES))


class MirrorTests(unittest.TestCase):
    PAIRS = (("meta_real_shaped", "all_bullish"), ("all_bullish", "higher_aligned_5m_opposed"),
             ("meta_real_shaped", "market_context_unavailable"), ("meta_real_shaped", "news_unavailable"),
             ("empty_news", "meta_real_shaped"), ("technical_unavailable", "meta_real_shaped"),
             ("meta_real_shaped", "unavailable_timeframe"), ("positive_market_return", "all_bullish"),
             ("meta_real_shaped", "benchmark_misaligned"), ("meta_real_shaped", "insufficient_data_state"))

    def test_swapping_inputs_mirrors_every_transition(self):
        for a, b in self.PAIRS:
            with self.subTest(pair=(a, b)):
                forward = build(later(b), earlier(a)).transitions
                backward = build(later(a), earlier(b)).transitions
                self.assertTrue(forward)
                self.assertEqual(sorted(mirrored(t) for t in forward), keyed(backward))

    def test_same_evidence_no_transitions(self):
        for name in corpus.names():
            self.assertEqual(build(later(name), earlier(name)).transitions, (), name)


class InvarianceTests(unittest.TestCase):
    def test_previous_never_changes_current_state(self):
        for name in cases.names():
            with self.subTest(pair=name):
                alone = build(cases.current(name)).to_dict()
                compared = build(cases.current(name), cases.previous(name)).to_dict()
                for key in CURRENT_STATE:
                    self.assertEqual(compared[key], alone[key], key)
                self.assertNotEqual(compared["intelligence_id"], alone["intelligence_id"])

    def test_identity_depends_on_previous(self):
        current = cases.current("no_change")
        a = build(current, earlier("meta_real_shaped")).intelligence_id
        b = build(current, earlier("all_bullish")).intelligence_id
        self.assertEqual(len({a, b, build(current).intelligence_id}), 3)
        self.assertEqual(build(current, earlier("meta_real_shaped")).intelligence_id, a)


class ProvenanceTests(unittest.TestCase):
    def test_every_transition_pointer_resolves(self):
        for name in cases.names():
            previous, current = cases.previous(name), cases.current(name)
            for t in build(current, previous).transitions:
                for pointer in t.previous_pointers:
                    self.assertTrue(pointer.startswith("previous:") and resolve(previous, current, pointer), pointer)
                for pointer in t.current_pointers:
                    self.assertTrue(pointer.startswith("current:") and resolve(previous, current, pointer), pointer)


class DeterminismTests(unittest.TestCase):
    def test_100_repetitions(self):
        for name in cases.names():
            with self.subTest(pair=name):
                current, previous = canonical_json(cases.current(name)), canonical_json(cases.previous(name))
                outputs = {text(build(json.loads(current), json.loads(previous))) for _ in range(100)}
                self.assertEqual(outputs, {expected_text(name)})

    def test_typed_and_dict_combinations(self):
        for name in ("entered_alignment", "news_identities_added", "context_became_unavailable"):
            typed_current = synthesize(cases.current_packet(name))
            typed_previous = synthesize(cases.previous_packet(name))
            forms_current = (typed_current, typed_current.to_dict())
            forms_previous = (typed_previous, typed_previous.to_dict())
            outputs = {text(build(c, p)) for c in forms_current for p in forms_previous}
            self.assertEqual(outputs, {expected_text(name)})

    def test_dict_key_order(self):
        def reversed_keys(value):
            if isinstance(value, dict):
                return {k: reversed_keys(value[k]) for k in reversed(list(value))}
            if isinstance(value, list):
                return [reversed_keys(v) for v in value]
            return value
        for name in cases.names():
            self.assertEqual(text(build(reversed_keys(cases.current(name)), reversed_keys(cases.previous(name)))),
                             expected_text(name))

    def test_identity_comparison_ignores_list_positions(self):
        """Transitions compare stable identities (sets), never positions: permuting the contradiction and news lists
        of already-validated inputs leaves them unchanged. (A synthesis itself must keep canonical order.)"""
        for name in ("reference_signs_changed", "news_became_unavailable", "opposition_appeared"):
            previous, current = cases.previous(name), cases.current(name)
            base = builder_module._transitions(previous, current)
            for data in (previous, current):
                data["contradictions"].reverse()
                data["news"]["sources"].reverse()
                data["market_context"]["references"].reverse()
            self.assertEqual(builder_module._transitions(previous, current), base)

    def test_fresh_processes_and_environments(self):
        code = ("import hashlib\nfrom evidence_synthesis.canonical import canonical_json\n"
                "from market_intelligence.builder import build\n"
                "from tests import market_intelligence_transition_cases as c\n"
                "print(hashlib.sha256(''.join(canonical_json(build(c.current(n), c.previous(n)).to_dict()) "
                "for n in c.names()).encode()).hexdigest())")
        expected = hashlib.sha256("".join(expected_text(n)[:-1] for n in cases.names()).encode()).hexdigest()
        variants = ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "2", "TZ": "Asia/Tokyo", "HOSTNAME": "elsewhere"},
                    {"PYTHONHASHSEED": "4099", "LANG": "C", "TZ": "UTC", "MIAS_UNRELATED": "x"})
        for extra in variants:
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd,
                                        env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


class IsolationAndSemanticsTests(unittest.TestCase):
    def test_sealed_transitions(self):
        inputs = {name: (cases.current(name), cases.previous(name)) for name in cases.names()}
        expected = {name: expected_text(name) for name in cases.names()}
        with sealed():
            produced = {name: text(build(*pair)) for name, pair in inputs.items()}
        self.assertEqual(produced, expected)

    def test_no_forbidden_concepts_in_transitions_or_comparison(self):
        for value in (*rules.TRANSITION_CODES, *rules.COMPARISON_STATUSES, *rules.NOT_COMPARABLE_REASONS):
            forbidden = words(value) & FORBIDDEN
            if value == "timeframe_direction_changed":
                forbidden -= {"direction"}  # The upstream state_direction fact; see the Phase 8A scan exception.
            self.assertFalse(forbidden, value)
        for banned in ("improving", "worsening", "strengthening", "weakening", "confirmation", "acceleration",
                       "deterioration", "reversal", "momentum_improved"):
            self.assertFalse(any(banned in code for code in rules.TRANSITION_CODES), banned)
        for name in cases.names():
            data = build(cases.current(name), cases.previous(name)).to_dict()
            stack = [data["comparison"], data["transitions"]]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, child in value.items():
                        self.assertFalse(words(key) & FORBIDDEN, key)
                        self.assertFalse(any(p in key for p in FORBIDDEN_PHRASES), key)
                        stack.append(child)
                elif isinstance(value, list):
                    stack.extend(value)


if __name__ == "__main__":
    unittest.main()
