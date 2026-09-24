"""Deterministic swing (pivot) detection and HH/HL/LH/LL market structure, with no look-ahead.

**Pivots** use a symmetric N-bar window (``pivot_window``, default 2). Bar ``i`` is:

- a **swing high** if ``high[i]`` is strictly greater than each of the N highs to
  its left and greater than or equal to each of the N highs to its right;
- a **swing low** if ``low[i]`` is strictly less than each of the N lows to its left
  and less than or equal to each of the N lows to its right.

Tie rule: in a flat top or bottom only the leftmost bar of the plateau qualifies.
It beats the bars to its right, and later bars of the plateau fail the strict
left comparison. A single bar may be both a swing high and a swing low (an
outside bar). The first N and last N bars can never be pivots.

**Confirmation (no look-ahead):** a pivot at bar ``i`` is only *confirmed* at bar
``i + N``, when its right-side bars exist. Replay and analysis at bar ``t`` see
only pivots with ``confirmed_index <= t``.

**Labels** (Higher High, Lower High, Higher Low, Lower Low): each confirmed swing
high is compared only with the previous confirmed swing high (HH if higher, LH
if lower, EH if equal within ``equal_tolerance_pct``). Each swing low is compared
only with the previous swing low (HL, LL or EL). A high is never compared with a
low. The first high and first low have no label.

**Trend** (from the latest labelled high and low):

| Last high | Last low | Trend |
|---|---|---|
| HH | HL | ``bullish`` |
| LH | LL | ``bearish`` |
| LH | HL | ``range`` (contracting) |
| EH | any | ``range`` (equal highs or lows) |
| any | EL | ``range`` (equal highs or lows) |
| HH | LL | ``mixed`` (expanding, conflicting) |
| missing | missing | ``insufficient_data`` (fewer than two highs or two lows) |
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Pivot:
    kind: str            # "high" or "low"
    index: int           # bar index of the pivot
    confirmed_index: int  # first bar index at which the pivot is known (index + window)
    price: float
    timestamp: object
    label: str = None    # HH/LH/EH for highs, HL/LL/EL for lows; None for the first of its kind

    def to_dict(self):
        return dict(kind=self.kind, label=self.label, price=round(self.price, 4), timestamp=self.timestamp.isoformat(),
                    index=self.index, confirmed_index=self.confirmed_index)


def find_pivots(bars, window=2):
    """All pivots with their confirmation index (labels are assigned by ``label_pivots``)."""
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise ValueError("pivot window must be a positive integer")
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    pivots = []
    for i in range(window, len(bars) - window):
        left, right = range(i - window, i), range(i + 1, i + window + 1)
        if all(highs[i] > highs[j] for j in left) and all(highs[i] >= highs[j] for j in right):
            pivots.append(Pivot("high", i, i + window, highs[i], bars[i].timestamp))
        if all(lows[i] < lows[j] for j in left) and all(lows[i] <= lows[j] for j in right):
            pivots.append(Pivot("low", i, i + window, lows[i], bars[i].timestamp))
    return pivots


def _compare(price, previous, tolerance_pct):
    if abs(price - previous) <= previous * tolerance_pct / 100.0:
        return "E"
    return "H" if price > previous else "L"


def label_pivots(pivots, equal_tolerance_pct=0.0):
    """Label highs against previous highs and lows against previous lows (in confirmation order)."""
    labelled, last = [], {"high": None, "low": None}
    for pivot in sorted(pivots, key=lambda p: (p.confirmed_index, p.index, p.kind)):
        previous = last[pivot.kind]
        label = None
        if previous is not None:
            relation = _compare(pivot.price, previous.price, equal_tolerance_pct)
            label = relation + ("H" if pivot.kind == "high" else "L")
        labelled_pivot = Pivot(pivot.kind, pivot.index, pivot.confirmed_index, pivot.price, pivot.timestamp, label)
        labelled.append(labelled_pivot)
        last[pivot.kind] = labelled_pivot
    return labelled


def confirmed(pivots, at_index):
    """Pivots known at bar ``at_index`` (no look-ahead)."""
    return [p for p in pivots if p.confirmed_index <= at_index]


def structure_state(pivots):
    """Trend and evidence from already-confirmed, labelled pivots."""
    highs = [p for p in pivots if p.kind == "high"]
    lows = [p for p in pivots if p.kind == "low"]
    last_high, last_low = (highs[-1] if highs else None), (lows[-1] if lows else None)
    high_type = last_high.label if last_high else None
    low_type = last_low.label if last_low else None
    if high_type is None or low_type is None:
        trend = "insufficient_data"
    elif high_type == "HH" and low_type == "HL":
        trend = "bullish"
    elif high_type == "LH" and low_type == "LL":
        trend = "bearish"
    elif high_type == "EH" or low_type == "EL" or (high_type == "LH" and low_type == "HL"):
        trend = "range"
    else:
        trend = "mixed"
    return dict(trend=trend, last_high_type=high_type, last_low_type=low_type,
                last_significant_high=last_high.to_dict() if last_high else None,
                last_significant_low=last_low.to_dict() if last_low else None,
                confirmed_highs=len(highs), confirmed_lows=len(lows))
