"""Phase 4 swing (pivot) detection, confirmation delay, HH/HL/LH/LL labels and trend state."""
from datetime import datetime, timedelta
from decimal import Decimal
import unittest

from market_data.models import EXCHANGE_TZ, MarketBar
from technical.structure import Pivot, confirmed, find_pivots, label_pivots, structure_state

T0 = datetime(2026, 9, 21, 9, 30, tzinfo=EXCHANGE_TZ)


def hl_bars(highs, lows=None):
    """Bars with the given highs (and lows, default high - 2); open/close sit mid-range."""
    lows = lows or [h - 2 for h in highs]
    out = []
    for i, (h, l) in enumerate(zip(highs, lows)):
        mid = Decimal(str((h + l) / 2))
        out.append(MarketBar("NVDA", T0 + timedelta(minutes=5 * i), "5m", mid, Decimal(str(h)), Decimal(str(l)), mid, 100))
    return out


def pivot(kind, index, price):
    return Pivot(kind, index, index + 2, price, T0 + timedelta(minutes=5 * index))


class PivotDetectionTests(unittest.TestCase):
    def test_swing_high_and_low(self):
        bars = hl_bars([10, 11, 15, 12, 11, 9, 8, 10, 12], [8, 9, 13, 10, 7, 5, 4, 6, 9])
        pivots = find_pivots(bars, 2)
        self.assertEqual([(p.kind, p.index, p.price, p.confirmed_index) for p in pivots],
                         [("high", 2, 15.0, 4), ("low", 6, 4.0, 8)])

    def test_edges_are_never_pivots(self):
        bars = hl_bars([20, 11, 12, 13, 30])
        self.assertEqual(find_pivots(bars, 2), [])

    def test_equal_high_tie_rule_leftmost_wins(self):
        bars = hl_bars([10, 11, 15, 15, 12, 11, 10])
        highs = [p for p in find_pivots(bars, 2) if p.kind == "high"]
        self.assertEqual([p.index for p in highs], [2])  # Bar 3 fails the strict left comparison.

    def test_equal_low_tie_rule_leftmost_wins(self):
        bars = hl_bars([20] * 7, [10, 9, 5, 5, 7, 8, 9])
        lows = [p for p in find_pivots(bars, 2) if p.kind == "low"]
        self.assertEqual([p.index for p in lows], [2])

    def test_window_size_changes_sensitivity(self):
        highs = [10, 12, 11, 13, 12, 11, 10, 9]
        self.assertEqual([p.index for p in find_pivots(hl_bars(highs), 1) if p.kind == "high"], [1, 3])
        self.assertEqual([p.index for p in find_pivots(hl_bars(highs), 2) if p.kind == "high"], [3])
        with self.assertRaises(ValueError):
            find_pivots(hl_bars(highs), 0)

    def test_swing_high_not_confirmed_before_window_bars(self):
        bars = hl_bars([10, 11, 15, 12, 11, 9, 8])
        (high,) = [p for p in find_pivots(bars, 2) if p.kind == "high"]
        self.assertEqual(confirmed([high], 3), [])
        self.assertEqual(confirmed([high], 4), [high])
        # The pivot cannot even be detected until its right-side bars exist.
        self.assertEqual([p for p in find_pivots(bars[:4], 2) if p.kind == "high"], [])
        self.assertEqual([p.index for p in find_pivots(bars[:5], 2) if p.kind == "high"], [2])

    def test_later_bars_can_invalidate_an_unconfirmed_candidate(self):
        early = hl_bars([10, 11, 15, 12])
        later = hl_bars([10, 11, 15, 12, 16])
        self.assertEqual([p for p in find_pivots(later, 2) if p.kind == "high" and p.index == 2], [])
        self.assertEqual(find_pivots(early, 2), [])  # So it was correctly not reported early either.


class LabelTests(unittest.TestCase):
    def test_highs_compared_only_with_highs_and_lows_with_lows(self):
        pivots = [pivot("high", 2, 100), pivot("low", 5, 90), pivot("high", 8, 105), pivot("low", 11, 95),
                  pivot("high", 14, 103), pivot("low", 17, 85), pivot("high", 20, 103)]
        labels = [(p.kind, p.label) for p in label_pivots(pivots)]
        self.assertEqual(labels, [("high", None), ("low", None), ("high", "HH"), ("low", "HL"), ("high", "LH"),
                                  ("low", "LL"), ("high", "EH")])

    def test_equal_tolerance(self):
        pivots = [pivot("high", 2, 100), pivot("high", 6, 100.2)]
        self.assertEqual(label_pivots(pivots)[1].label, "HH")
        self.assertEqual(label_pivots(pivots, equal_tolerance_pct=0.25)[1].label, "EH")


class TrendTests(unittest.TestCase):
    def trend(self, *prices):
        kinds = ["high", "low"] * (len(prices) // 2)
        return structure_state(label_pivots([pivot(k, 3 * i, p) for i, (k, p) in enumerate(zip(kinds, prices))]))

    def test_trend_table(self):
        self.assertEqual(self.trend(100, 90, 105, 95)["trend"], "bullish")
        self.assertEqual(self.trend(100, 90, 95, 85)["trend"], "bearish")
        self.assertEqual(self.trend(100, 90, 98, 92)["trend"], "range")   # LH + HL (contracting).
        self.assertEqual(self.trend(100, 90, 100, 85)["trend"], "range")  # Equal highs.
        self.assertEqual(self.trend(100, 90, 105, 85)["trend"], "mixed")  # HH + LL (expanding).
        self.assertEqual(self.trend(100, 90)["trend"], "insufficient_data")
        self.assertEqual(structure_state([])["trend"], "insufficient_data")

    def test_state_evidence(self):
        state = self.trend(100, 90, 105, 95)
        self.assertEqual((state["last_high_type"], state["last_low_type"]), ("HH", "HL"))
        self.assertEqual((state["confirmed_highs"], state["confirmed_lows"]), (2, 2))
        self.assertEqual(state["last_significant_high"]["price"], 105)
        self.assertEqual(state["last_significant_low"]["label"], "HL")


if __name__ == "__main__":
    unittest.main()
