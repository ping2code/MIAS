"""Phase 4 breakout/breakdown logic, state classification and confidence (never BUY/SELL)."""
from dataclasses import replace
from datetime import datetime
import unittest

from market_data.models import EXCHANGE_TZ
from technical.levels import Level, breakout_state
from technical.models import TechnicalConfig, TechnicalSnapshot
from technical.signals import classify, evidence_items

T0 = datetime(2026, 9, 21, 9, 30, tzinfo=EXCHANGE_TZ)
CONFIG = TechnicalConfig()
STATES = {"bullish_setup", "bearish_setup", "bullish_momentum", "bearish_momentum", "breakout_watch", "breakdown_watch",
          "range", "mixed", "insufficient_data"}


def level(price):
    return Level(price, 2, T0, T0, 2, 0)


class BreakoutTests(unittest.TestCase):
    # Buffer with atr=None: 0.1% of the close, so about 0.1 at price 100.

    def test_confirmed_breakout_needs_close_beyond_buffer(self):
        self.assertEqual(breakout_state(100.15, [99.5, 99.9], [level(100)], None, [])[0], "breakout")
        state, level_price, pad = breakout_state(100.05, [99.5, 99.9], [level(100)], None, [])
        self.assertEqual((state, level_price), ("none", None))
        self.assertAlmostEqual(pad, 100.05 * 0.001)

    def test_wick_only_break_is_not_a_breakout(self):
        # Only closes count; a bar whose high pierced the level but closed below it is not a breakout.
        self.assertEqual(breakout_state(99.95, [99.5, 99.9], [level(100)], None, [])[0], "none")

    def test_already_above_is_not_a_new_breakout(self):
        self.assertEqual(breakout_state(101.0, [99.0, 100.5], [level(100)], None, [])[0], "none")

    def test_creeping_through_the_buffer_counts_once(self):
        # Previous close inside the buffer zone above the level; an earlier close was below it.
        self.assertEqual(breakout_state(100.4, [99.8, 100.05], [level(100)], None, [])[0], "breakout")

    def test_bounce_off_support_is_not_a_breakout(self):
        # Never closed at or below the level in the lookback: the level was support, not resistance.
        self.assertEqual(breakout_state(100.4, [100.3, 100.05], [level(100)], None, [])[0], "none")

    def test_breakdown_and_failures(self):
        self.assertEqual(breakout_state(99.8, [100.2, 100.05], [level(100)], None, [])[0], "breakdown")
        self.assertEqual(breakout_state(99.9, [100.5, 100.4], [], None, [("breakout", 100.0)])[:2],
                         ("failed_breakout", 100.0))
        self.assertEqual(breakout_state(100.1, [99.5, 99.6], [], None, [("breakdown", 100.0)])[:2],
                         ("failed_breakdown", 100.0))
        self.assertEqual(breakout_state(100.5, [100.5, 100.4], [], None, [("breakout", 100.0)])[0], "none")

    def test_reversal_through_fresh_breakout_is_a_failure_not_a_breakdown(self):
        state = breakout_state(99.5, [99.8, 100.6], [level(100)], None, [("breakout", 100.0)])
        self.assertEqual(state[:2], ("failed_breakout", 100.0))

    def test_atr_buffer(self):
        state, _, pad = breakout_state(100.3, [99.8, 99.9], [level(100)], 2.0, [])
        self.assertEqual((state, pad), ("none", 0.5))  # 0.25 x ATR 2.0 beats 0.1% of price.
        self.assertEqual(breakout_state(100.6, [99.8, 99.9], [level(100)], 2.0, [])[0], "breakout")

    def test_no_history(self):
        self.assertEqual(breakout_state(200.0, [], [level(100)], None, [])[0], "none")


def snapshot(**overrides):
    values = dict(symbol="META", timestamp=T0, interval="5m", index=40, price=100.0,
                  ema=dict(ema9=100.0, ema20=99.0, ema50=98.0, ema200=None), vwap=99.5, rsi=60.0, atr=0.5, volume=1000,
                  average_volume=1000.0, relative_volume=1.0, trend="bullish", last_high_type="HH", last_low_type="HL",
                  significant_high=None, significant_low=None, support_levels=(), resistance_levels=(), gap_type=None,
                  gap_percent=None, gap_absolute=None, breakout_state="none", breakout_level=None,
                  ema_state=dict(alignment="bullish_alignment", price_vs_ema20="above"),
                  vwap_state=dict(position="above_vwap", distance_dollars=0.5, distance_percent=0.5), momentum="strong")
    values.update(overrides)
    return TechnicalSnapshot(**values)


BEARISH = dict(trend="bearish", last_high_type="LH", last_low_type="LL", rsi=40.0, momentum="weak",
               ema_state=dict(alignment="bearish_alignment", price_vs_ema20="below"),
               vwap_state=dict(position="below_vwap", distance_dollars=-0.5, distance_percent=-0.5))


class ClassificationTests(unittest.TestCase):
    def test_bullish_setup_high_confidence(self):
        signal = classify(snapshot(), CONFIG)
        self.assertEqual((signal.state, signal.confidence, signal.agreeing, signal.conflicting),
                         ("bullish_setup", "HIGH", 5, 0))
        self.assertIn("confirmed HH/HL structure", signal.reasons)

    def test_bearish_setup(self):
        self.assertEqual(classify(snapshot(**BEARISH), CONFIG).state, "bearish_setup")

    def test_setup_without_vwap_uses_ema20(self):
        s = snapshot(vwap=None, vwap_state=dict(position=None, distance_dollars=None, distance_percent=None))
        self.assertEqual(classify(s, CONFIG).state, "bullish_setup")

    def test_momentum_states(self):
        s = snapshot(trend="insufficient_data", last_high_type=None, last_low_type=None)
        self.assertEqual(classify(s, CONFIG).state, "bullish_momentum")
        b = snapshot(**dict(BEARISH, trend="mixed", last_high_type="HH"))
        self.assertEqual(classify(b, CONFIG).state, "bearish_momentum")
        neutral = replace(s, momentum="neutral", rsi=50.0)
        self.assertEqual(classify(neutral, CONFIG).state, "mixed")

    def test_range_outranks_momentum_but_not_breakout(self):
        s = snapshot(trend="range", last_high_type="LH", last_low_type="HL")
        signal = classify(s, CONFIG)
        self.assertEqual((signal.state, signal.confidence), ("range", "LOW"))
        self.assertEqual(signal.reasons[0], "range structure (LH/HL)")
        broke = replace(s, breakout_state="breakout", breakout_level=99.8)
        self.assertEqual(classify(broke, CONFIG).state, "breakout_watch")
        self.assertEqual(classify(replace(s, breakout_state="breakdown", breakout_level=99.8), CONFIG).state,
                         "breakdown_watch")

    def test_failed_breakout_is_named(self):
        s = snapshot(trend="mixed", breakout_state="failed_breakout", breakout_level=100.2, momentum="neutral")
        signal = classify(s, CONFIG)
        self.assertEqual(signal.state, "mixed")
        self.assertEqual(signal.reasons[0], "failed breakout of level 100.20")

    def test_insufficient_data(self):
        s = snapshot(ema=dict(ema9=None, ema20=None, ema50=None, ema200=None), rsi=None)
        signal = classify(s, CONFIG)
        self.assertEqual((signal.state, signal.confidence), ("insufficient_data", "LOW"))
        self.assertIn("EMA20, RSI", signal.reasons[0])

    def test_confidence_levels(self):
        weak = snapshot(momentum="neutral", rsi=50.0, gap_type="gap_down", gap_percent=-1.0)
        signal = classify(weak, CONFIG)
        self.assertEqual((signal.state, signal.agreeing, signal.conflicting, signal.confidence),
                         ("bullish_setup", 4, 1, "MEDIUM"))
        thin = snapshot(momentum="weak", rsi=40.0, gap_type="gap_down", gap_percent=-1.0,
                        ema_state=dict(alignment="bullish_alignment", price_vs_ema20="below"))
        signal = classify(thin, CONFIG)
        self.assertEqual((signal.state, signal.agreeing, signal.conflicting, signal.confidence),
                         ("bullish_setup", 3, 3, "LOW"))

    def test_elevated_relative_volume_adds_agreement(self):
        base = snapshot(momentum="neutral", rsi=50.0)
        self.assertEqual(classify(base, CONFIG).confidence, "MEDIUM")
        loud = classify(replace(base, relative_volume=2.0), CONFIG)
        self.assertEqual((loud.confidence, loud.agreeing), ("HIGH", 5))
        self.assertIn("relative volume 2.0x (elevated)", loud.reasons)

    def test_never_buy_or_sell(self):
        variants = [snapshot(), snapshot(**BEARISH), snapshot(trend="range"), snapshot(breakout_state="breakout", breakout_level=99.0)]
        for s in variants:
            signal = classify(s, CONFIG)
            self.assertIn(signal.state, STATES)
            text = " ".join((signal.state,) + signal.reasons).lower()
            for word in ("buy", "sell", "entry", "target", "stop loss"):
                self.assertNotIn(word, text)

    def test_evidence_items(self):
        items = evidence_items(snapshot(gap_type="gap_up", gap_percent=1.2), CONFIG)
        self.assertEqual([name for name, _, _ in items],
                         ["structure", "ema_alignment", "price_vs_ema20", "vwap", "momentum", "gap"])
        self.assertTrue(all(sign == 1 for _, sign, _ in items))


if __name__ == "__main__":
    unittest.main()
