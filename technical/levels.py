"""Deterministic support/resistance from confirmed pivots, and breakout/breakdown with a buffer.

**Levels:** confirmed pivot prices (highs and lows together, since a level can
change roles) are sorted by price and clustered greedily. A pivot joins the
current cluster while its price is within ``tolerance`` of the cluster's running
mean, where ``tolerance = max(cluster_pct% x price, cluster_atr x ATR)``. A
cluster with at least ``min_touches`` pivots is a level:

- **price** is the mean pivot price;
- **touches** is the pivot count;
- **first_seen** and **last_seen** are the earliest and latest pivot timestamps;
- **strength** records the touch count, the high/low mix and the pivot span.

Relative to the current close, a level above is ``resistance`` and a level below
is ``support``. Only pivots confirmed at the evaluated bar are used (no
look-ahead).

**Breakouts** are evaluated against the levels known at the *previous* bar, so
the breakout bar never helps form the level it breaks:

- ``breakout``: close > level + buffer, the previous close was not above
  level + buffer, and at least one of the last ``failure_lookback`` closes was at
  or below the level (so the level really was resistance; a bounce off support
  within the buffer is not a breakout);
- ``breakdown``: close < level - buffer, the previous close was not below
  level - buffer, and at least one of the last ``failure_lookback`` closes was at
  or above the level;
- ``failed_breakout``: a breakout of level L happened within the last
  ``failure_lookback`` bars and the close is now back below L;
- ``failed_breakdown``: symmetric;
- failures are checked first, so falling straight back through a just-broken
  level is reported as ``failed_breakout``, not as a new ``breakdown``;
- ``none``: otherwise.

``buffer = max(buffer_pct% x price, buffer_atr x ATR)``, so a one-cent crossing
never counts.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    price: float
    touches: int
    first_seen: object
    last_seen: object
    highs: int
    lows: int

    def as_dict(self, current_price):
        return dict(price=round(self.price, 4), type="resistance" if self.price > current_price else "support",
                    touches=self.touches, first_seen=self.first_seen.isoformat(), last_seen=self.last_seen.isoformat(),
                    strength=dict(touches=self.touches, from_highs=self.highs, from_lows=self.lows,
                                  span_seconds=int((self.last_seen - self.first_seen).total_seconds())))


def _tolerance(price, atr, pct, atr_mult):
    return max(price * pct / 100.0, (atr or 0.0) * atr_mult)


def cluster_levels(pivots, *, atr=None, cluster_pct=0.35, cluster_atr=0.5, min_touches=2):
    """Levels from already-confirmed pivots (deterministic: sorted by price, then bar index)."""
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


def split_levels(levels, price):
    """(supports below price nearest first, resistances above price nearest first)."""
    supports = sorted((l for l in levels if l.price < price), key=lambda l: price - l.price)
    resistances = sorted((l for l in levels if l.price > price), key=lambda l: l.price - price)
    return supports, resistances


def buffer(price, atr, buffer_pct=0.1, buffer_atr=0.25):
    return _tolerance(price, atr, buffer_pct, buffer_atr)


def breakout_state(close, prior_closes, prior_levels, atr, recent_events, *, buffer_pct=0.1, buffer_atr=0.25):
    """Classify the current bar against the previous bar's levels and recent (unexpired) break events.

    ``prior_closes`` holds the closes before this bar (oldest first; the last is the
    previous close). ``recent_events`` holds (kind, level_price) pairs from the last
    ``failure_lookback`` bars. Returns (state, level_price or None, buffer value).
    """
    pad = buffer(close, atr, buffer_pct, buffer_atr)
    for kind, level_price in reversed(recent_events):  # A reversal of a fresh break is reported as its failure first.
        if kind == "breakout" and close < level_price:
            return "failed_breakout", level_price, pad
        if kind == "breakdown" and close > level_price:
            return "failed_breakdown", level_price, pad
    if prior_closes:
        previous, lowest, highest = prior_closes[-1], min(prior_closes), max(prior_closes)
        for level in sorted(prior_levels, key=lambda l: l.price):
            if close > level.price + pad and previous <= level.price + pad and lowest <= level.price:
                return "breakout", level.price, pad
        for level in sorted(prior_levels, key=lambda l: -l.price):
            if close < level.price - pad and previous >= level.price - pad and highest >= level.price:
                return "breakdown", level.price, pad
    return "none", None, pad
