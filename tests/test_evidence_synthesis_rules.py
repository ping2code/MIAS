"""Phase 7G synthesis rules: exhaustive 9×9×9 state combinations, frozen contracts, strict signs, relations.

Expectations are written independently from the approved specification, not by calling the code under test.
"""
from itertools import product
import unittest

from evidence_synthesis import rules as r

SPEC_DIRECTION = dict(bullish_setup="bullish", bullish_momentum="bullish", breakout_watch="bullish",
                      bearish_setup="bearish", bearish_momentum="bearish", breakdown_watch="bearish",
                      range="non_directional", mixed="non_directional", insufficient_data="unavailable")


def spec_pair(a, b):
    if "unavailable" in (a, b):
        return "unavailable"
    if "non_directional" in (a, b):
        return "non_directional"
    return "agree" if a == b else "oppose"


def spec_pattern(directions):
    if "unavailable" in directions:
        return "incomplete"
    if "bullish" in directions and "bearish" in directions:
        return "opposed"
    if set(directions) == {"bullish"}:
        return "all_bullish"
    if set(directions) == {"bearish"}:
        return "all_bearish"
    if set(directions) == {"non_directional"}:
        return "all_non_directional"
    return "partially_directional"


class ExhaustiveTests(unittest.TestCase):
    def test_all_729_state_combinations(self):
        combos = list(product(r.TECHNICAL_STATES, repeat=3))
        self.assertEqual(len(combos), 729)
        patterns = set()
        for states in combos:
            directions = [r.state_direction(s) for s in states]
            self.assertEqual(directions, [SPEC_DIRECTION[s] for s in states], states)
            by = dict(zip(r.INTERVALS, directions))
            for first, second in r.PAIRS:
                self.assertEqual(r.pair_relation(by[first], by[second]), spec_pair(by[first], by[second]), states)
                self.assertEqual(r.pair_relation(by[first], by[second]), r.pair_relation(by[second], by[first]))
            pattern = r.timeframe_pattern(directions)
            self.assertEqual(pattern, spec_pattern(directions), states)
            patterns.add(pattern)
        self.assertEqual(patterns, set(r.PATTERNS))  # Every pattern is reachable.

    def test_missing_row_is_unavailable(self):
        self.assertEqual(r.state_direction(None), "unavailable")
        self.assertEqual(r.timeframe_pattern(["bullish", "unavailable", "bearish"]), "incomplete")  # Precedence.

    def test_named_cases(self):
        d = r.state_direction
        cases = {("bullish_setup", "bullish_momentum", "breakout_watch"): "all_bullish",
                 ("bearish_setup", "bearish_momentum", "breakdown_watch"): "all_bearish",
                 ("range", "mixed", "range"): "all_non_directional",
                 ("bullish_setup", "bullish_setup", "bearish_setup"): "opposed",   # 1d+1h aligned, 5m countertrend
                 ("bearish_setup", "bullish_setup", "bullish_momentum"): "opposed",  # 1h+5m aligned, 1d opposed
                 ("bullish_setup", "mixed", "range"): "partially_directional"}
        for states, pattern in cases.items():
            self.assertEqual(r.timeframe_pattern([d(s) for s in states]), pattern, states)


class FrozenContractTests(unittest.TestCase):
    def test_state_direction_equals_technical_engine_contract(self):
        from technical.signals import DIRECTION  # Test-only import; runtime synthesis never imports technical.
        self.assertEqual(r.STATE_DIRECTION, DIRECTION)
        for state in r.TECHNICAL_STATES:
            expected = {1: "bullish", -1: "bearish"}.get(DIRECTION.get(state))
            if expected:
                self.assertEqual(r.state_direction(state), expected)
            else:
                self.assertIn(r.state_direction(state), ("non_directional", "unavailable"))

    def test_vocabularies_equal_persisted_schema(self):
        from persistence.models import BREAKOUT_STATES, TECHNICAL_STATES, TECHNICAL_TRENDS
        self.assertEqual((r.TECHNICAL_STATES, r.TECHNICAL_TRENDS, r.BREAKOUT_STATES),
                         (TECHNICAL_STATES, TECHNICAL_TRENDS, BREAKOUT_STATES))

    def test_versions(self):
        self.assertEqual((r.SYNTHESIS_FORMAT_VERSION, r.RULES_VERSION, r.PACKET_FORMAT_VERSION),
                         ("phase7g-v1", "phase7g-rules-v1", "phase7c-v1"))
        self.assertNotIn("technical_state_mixed", r.CONTRADICTION_CODES)


class SignAndContextTests(unittest.TestCase):
    def test_strict_sign_no_deadband(self):
        for value, expected in (("0.0000000001", "positive"), ("-0.0000000001", "negative"), ("0", "zero"),
                                ("-0", "zero"), ("0E-10", "zero"), ("12.5", "positive"), (None, "unavailable")):
            self.assertEqual(r.sign(value), expected, value)
        for bad in ("abc", "NaN", "Infinity", 1.5):
            with self.assertRaises(ValueError):
                r.sign(bad)

    def test_context_relation(self):
        table = {("bullish", "positive"): "agree", ("bullish", "negative"): "oppose",
                 ("bearish", "negative"): "agree", ("bearish", "positive"): "oppose",
                 ("bullish", "zero"): "non_directional", ("non_directional", "positive"): "non_directional",
                 ("unavailable", "positive"): "unavailable", ("bearish", "unavailable"): "unavailable"}
        for (direction, value_sign), expected in table.items():
            self.assertEqual(r.context_relation(direction, value_sign), expected)


if __name__ == "__main__":
    unittest.main()
