"""Phase 4 replay: every snapshot uses only data available at its bar (no look-ahead).

The central proof is ``replay(bars)[i] == analyze(bars[:i + 1])`` for every bar of
every synthetic scenario: the analysis of a truncated series cannot see the
future, so equality shows the full replay did not either. The targeted tests
below pin the individual mechanisms.
"""
from dataclasses import replace
from decimal import Decimal
import unittest

from market_data.models import MarketBar
from technical import indicators
from technical.engine import TechnicalEngine
from technical.structure import find_pivots
from tests.technical_fixtures import SCENARIOS

ENGINE = TechnicalEngine()


def mutate_future(bars, start):
    """Replace every bar from ``start`` on with a wildly different path (same timestamps)."""
    out = list(bars[:start])
    for i, bar in enumerate(bars[start:]):
        price = Decimal("50") + Decimal(i % 7) * 3
        out.append(MarketBar(bar.symbol, bar.timestamp, bar.interval, price, price + 2, price - 2, price + 1, 10 * bar.volume))
    return out


class ReplayEquivalenceTests(unittest.TestCase):
    def test_replay_equals_truncated_analysis_for_every_bar(self):
        for name, build in SCENARIOS.items():
            for symbol in ("META", "NVDA"):
                bars = build(symbol)
                full = ENGINE.replay(bars)
                step = 1 if symbol == "META" else 5  # Every bar for META; every fifth for NVDA (runtime).
                for i in range(0, len(bars), step):
                    self.assertEqual(full[i], ENGINE.analyze(bars[:i + 1]), f"{name} {symbol} bar {i}")

    def test_changing_the_future_never_changes_the_past(self):
        for name in ("breakout_high_volume", "gap_up_failure", "vwap_reclaim"):
            bars = SCENARIOS[name]("META")
            for cut in (30, 57, len(bars) - 10):
                original, mutated = ENGINE.replay(bars), ENGINE.replay(mutate_future(bars, cut))
                self.assertEqual(original[:cut], mutated[:cut], f"{name} cut {cut}")
                self.assertNotEqual(original[cut:], mutated[cut:])

    def test_replay_is_deterministic(self):
        bars = SCENARIOS["ema_crossover"]("NVDA")
        self.assertEqual(ENGINE.replay(bars), ENGINE.replay(list(bars)))


class NoLookAheadMechanismTests(unittest.TestCase):
    def test_swing_high_not_confirmed_before_required_future_bars(self):
        bars = SCENARIOS["bullish_trend"]("META")
        window = ENGINE.config.pivot_window
        snaps = ENGINE.replay(bars)
        for pivot in find_pivots(bars, window):
            self.assertEqual(pivot.confirmed_index, pivot.index + window)
            # Detection itself needs the right-side bars: with one bar fewer, the pivot does not exist.
            truncated = [p.index for p in find_pivots(bars[:pivot.confirmed_index], window) if p.kind == pivot.kind]
            self.assertNotIn(pivot.index, truncated)
        counts = [s.evidence["confirmed_pivots"] for s in snaps]
        confirmations = sorted(p.confirmed_index for p in find_pivots(bars, window))
        for i, count in enumerate(counts):
            self.assertEqual(count, sum(1 for c in confirmations if c <= i), f"bar {i}")

    def test_structure_labels_only_use_confirmed_pivots(self):
        snaps = ENGINE.replay(SCENARIOS["bearish_trend"]("NVDA"))
        for s in snaps:
            for key in ("significant_high", "significant_low"):
                pivot = getattr(s, key)
                if pivot:
                    self.assertLessEqual(pivot["confirmed_index"], s.index)
                    self.assertLessEqual(pivot["index"], s.index - ENGINE.config.pivot_window)

    def test_levels_never_use_future_pivots(self):
        snaps = ENGINE.replay(SCENARIOS["range"]("META"))
        window = ENGINE.config.pivot_window
        for s in snaps:
            for level in s.support_levels + s.resistance_levels:
                self.assertGreaterEqual(s.index, window)
                self.assertLessEqual(level["last_seen"], snaps[s.index - window].timestamp.isoformat())

    def test_breakout_not_confirmed_before_its_bar(self):
        bars = SCENARIOS["breakout_high_volume"]("NVDA")
        for cut in range(40, 57):
            states = {s.breakout_state for s in ENGINE.replay(bars[:cut + 1])}
            self.assertEqual(states, {"none"}, f"cut {cut}")
        self.assertEqual(ENGINE.replay(bars[:58])[-1].breakout_state, "breakout")

    def test_breakout_bar_does_not_form_the_level_it_breaks(self):
        snaps = ENGINE.replay(SCENARIOS["breakout_high_volume"]("META"))
        level = snaps[57].breakout_level
        self.assertIn(round(level, 4), [l["price"] for l in snaps[56].resistance_levels])

    def test_ema_and_rsi_use_only_history(self):
        bars = SCENARIOS["gap_up_continuation"]("NVDA")
        closes = [float(b.close) for b in bars]
        snaps = ENGINE.replay(bars)
        for i in (19, 50, 100, len(bars) - 1):
            self.assertEqual(snaps[i].ema["ema20"], indicators.ema(closes[:i + 1], 20)[-1])
            self.assertEqual(snaps[i].rsi, indicators.rsi(closes[:i + 1], 14)[-1])
            self.assertEqual(snaps[i].atr, indicators.atr(bars[:i + 1], 14)[-1])
            self.assertEqual(snaps[i].vwap, indicators.vwap(bars[:i + 1])[-1])

    def test_gap_unknown_until_session_open(self):
        bars = SCENARIOS["gap_up_continuation"]("META")
        snaps = ENGINE.replay(bars)
        self.assertIsNone(snaps[77].gap_type)       # Last bar of day 1 knows nothing about day 2's open.
        self.assertEqual(snaps[78].gap_type, "gap_up")

    def test_equality_detects_leaks(self):
        # Sanity check of the method itself: a snapshot carrying future information would compare unequal.
        bars = SCENARIOS["range"]("META")
        snap = ENGINE.analyze(bars[:40])
        leaked = replace(snap, price=float(bars[41].close))
        self.assertNotEqual(ENGINE.replay(bars)[39], leaked)


if __name__ == "__main__":
    unittest.main()
