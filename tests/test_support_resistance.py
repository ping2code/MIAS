"""Phase 4 support/resistance clustering and touch counting."""
from datetime import datetime, timedelta
import unittest

from market_data.models import EXCHANGE_TZ
from technical.levels import buffer, cluster_levels, split_levels
from technical.structure import Pivot

T0 = datetime(2026, 9, 21, 9, 30, tzinfo=EXCHANGE_TZ)


def pivot(kind, index, price):
    return Pivot(kind, index, index + 2, price, T0 + timedelta(minutes=5 * index))


class ClusterTests(unittest.TestCase):
    def test_nearby_pivots_cluster_into_one_level(self):
        pivots = [pivot("high", 5, 100.0), pivot("high", 15, 100.2), pivot("low", 25, 99.9), pivot("low", 10, 95.0)]
        levels = cluster_levels(pivots, atr=None, cluster_pct=0.35, min_touches=2)
        self.assertEqual(len(levels), 1)
        level = levels[0]
        self.assertAlmostEqual(level.price, (100.0 + 100.2 + 99.9) / 3)
        self.assertEqual((level.touches, level.highs, level.lows), (3, 2, 1))  # A level can change roles.
        self.assertEqual((level.first_seen, level.last_seen), (T0 + timedelta(minutes=25), T0 + timedelta(minutes=125)))
        as_dict = level.as_dict(101.0)
        self.assertEqual((as_dict["type"], as_dict["touches"], as_dict["strength"]["span_seconds"]), ("support", 3, 6000))
        self.assertEqual(level.as_dict(99.0)["type"], "resistance")

    def test_min_touches_and_separation(self):
        pivots = [pivot("high", 1, 100.0), pivot("high", 2, 101.0), pivot("high", 3, 102.0)]
        self.assertEqual(cluster_levels(pivots, cluster_pct=0.35), [])  # 1% apart: three single-touch clusters.
        self.assertEqual(len(cluster_levels(pivots, cluster_pct=0.35, min_touches=1)), 3)

    def test_atr_widens_tolerance(self):
        pivots = [pivot("high", 1, 100.0), pivot("high", 2, 101.0)]
        self.assertEqual(cluster_levels(pivots, atr=1.0, cluster_pct=0.35, cluster_atr=0.5), [])
        (level,) = cluster_levels(pivots, atr=2.5, cluster_pct=0.35, cluster_atr=0.5)
        self.assertEqual(level.touches, 2)

    def test_deterministic_regardless_of_input_order(self):
        pivots = [pivot("high", i, 100 + (i % 3) * 0.1) for i in range(9)] + [pivot("low", 20 + i, 90 + i * 0.05) for i in range(4)]
        self.assertEqual(cluster_levels(pivots), cluster_levels(list(reversed(pivots))))

    def test_split_and_buffer(self):
        levels = cluster_levels([pivot("low", 1, 95), pivot("low", 2, 95.1), pivot("high", 3, 105), pivot("high", 4, 105.2),
                                 pivot("low", 5, 90), pivot("low", 6, 90.1)])
        supports, resistances = split_levels(levels, 100)
        self.assertEqual([round(l.price, 2) for l in supports], [95.05, 90.05])  # Nearest first.
        self.assertEqual([round(l.price, 2) for l in resistances], [105.1])
        self.assertAlmostEqual(buffer(100, None), 0.1)
        self.assertAlmostEqual(buffer(100, 2.0), 0.5)


if __name__ == "__main__":
    unittest.main()
