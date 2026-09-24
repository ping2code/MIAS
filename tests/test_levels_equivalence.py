"""Phase 4C support/resistance optimization: bit-identical to the Phase 4 clustering implementation.

``reference_cluster_levels`` is the Phase 4 implementation, copied verbatim, as the
oracle. The optimized ``technical.levels.cluster_levels`` must return equal
``Level`` values (exact float equality) on randomized and adversarial inputs.
"""
from datetime import datetime, timedelta, timezone
import random
import unittest

from technical.levels import Level, _tolerance, cluster_levels
from technical.structure import Pivot

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def reference_cluster_levels(pivots, *, atr=None, cluster_pct=0.35, cluster_atr=0.5, min_touches=2):
    ordered = sorted(pivots, key=lambda p: (p.price, p.index))
    clusters, current = [], []
    for pivot in ordered:
        if current:
            mean = sum(p.price for p in current) / len(current)
            if pivot.price - mean > _tolerance(mean, atr, cluster_pct, cluster_atr):
                clusters.append(current)
                current = []
        current.append(pivot)
    if current:
        clusters.append(current)
    levels = []
    for cluster in clusters:
        if len(cluster) >= min_touches:
            levels.append(Level(price=sum(p.price for p in cluster) / len(cluster), touches=len(cluster),
                                first_seen=min(p.timestamp for p in cluster), last_seen=max(p.timestamp for p in cluster),
                                highs=sum(p.kind == "high" for p in cluster), lows=sum(p.kind == "low" for p in cluster)))
    return levels


def pivots(rng, n, base, spread):
    return [Pivot(rng.choice(("high", "low")), i, i + 2, base * (1 + rng.gauss(0, spread)), T0 + timedelta(minutes=5 * i))
            for i in range(n)]


class EquivalenceTests(unittest.TestCase):
    def assert_same(self, pv, **kwargs):
        expected, got = reference_cluster_levels(pv, **kwargs), cluster_levels(pv, **kwargs)
        self.assertEqual(got, expected)
        for a, b in zip(got, expected):
            self.assertEqual(a.price.hex(), b.price.hex())  # Bit-identical, not merely close.

    def test_randomized(self):
        rng = random.Random(20260924)
        for _ in range(2000):
            pv = pivots(rng, rng.randint(0, 220), rng.uniform(1, 3000), rng.choice((0.001, 0.005, 0.02)))
            self.assert_same(pv, atr=rng.choice((None, 0.0, rng.uniform(0.001, 50))), cluster_pct=rng.uniform(0, 1.5),
                             cluster_atr=rng.uniform(0, 2), min_touches=rng.randint(1, 5))

    def test_adversarial_floats(self):
        rng = random.Random(1)
        # Many near-equal prices whose naive running sums differ from compensated sums.
        tricky = [Pivot("high", i, i + 2, 0.1 * (1 + 1e-16 * i), T0 + timedelta(minutes=i)) for i in range(300)]
        self.assert_same(tricky)
        self.assert_same([Pivot("low", i, i + 2, 1e-9 + i * 1e-18, T0) for i in range(100)], min_touches=1)
        self.assert_same(pivots(rng, 200, 740.0, 0.0001), atr=1.3)
        self.assert_same([])


if __name__ == "__main__":
    unittest.main()
