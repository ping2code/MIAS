"""Phase 5 pre-registered hypotheses and frozen evidence criteria (research only; never used by live code).

Each hypothesis is an immutable, fully specified definition: state(s), interval(s),
horizon, symbols, metric, null model, sample threshold and evaluation periods. Its
**content hash** is the SHA-256 of its canonical JSON.

- The hashes are recorded in ``docs/phase5-preregistered-hypotheses.md`` and pinned
  by ``tests/test_phase5_robustness.py``, so any later edit fails the tests.
- The matrix writes the hashes into ``plan.json`` at run time, which proves which
  definitions produced the results.

The classification rules below were written **before** any Phase 5 data was
fetched. They are neutral evidence descriptors, never trading recommendations,
and they never change production thresholds.
"""
from dataclasses import asdict, dataclass
import hashlib
import json

MIN_SAMPLE = 30
STRONG_SAMPLE = 100
H1_NEAR_ZERO_BPS = 10.0  # |excess| below this at 1d/5 bars is "near zero" (about 0.3x the smallest daily drift).
SUPPORT_SHARE, UNSUPPORT_SHARE = 0.75, 0.25
MIN_CELLS = 4


@dataclass(frozen=True)
class Hypothesis:
    id: str
    title: str
    states: tuple
    intervals: tuple
    horizons: tuple
    symbols: tuple
    metric: str
    expected: str
    null_model: str
    pivot_windows: tuple
    session_policy: str
    primary_periods: tuple
    holdout: str
    min_sample: int
    classification: str

    def canonical(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def content_hash(self):
        return hashlib.sha256(self.canonical().encode()).hexdigest()


PERIOD_A = "A=2025-01-02:2026-09-24"
PERIOD_B = "B=2024-10-01:2025-01-01"

H1 = Hypothesis(
    id="H1", title="Daily bearish_setup shows above-drift forward returns for META and MSFT",
    states=("bearish_setup",), intervals=("1d",), horizons=(5,), symbols=("META", "MSFT"),
    metric="excess_mean_return (state mean 5-bar forward return minus all-bars mean, same run)",
    expected="excess > 0 for each symbol", null_model="circular_shift(1000, seed=42042)", pivot_windows=(2,),
    session_policy="n/a (daily)", primary_periods=(PERIOD_A,),
    holdout=("required: an unseen period with n>=30 per symbol; none available (free tier: 4 unseen sessions "
             "before Period B, 1 after Period A). Secondary pseudo-holdout: Period A split at 2025-11-03 "
             "(in-sample, descriptive only)"),
    min_sample=MIN_SAMPLE,
    classification="H1 rules in classify_h1 (frozen)")

H1X = Hypothesis(
    id="H1X", title="Cross-sectional check of H1's pattern on symbols unseen in Phase 4D",
    states=("bearish_setup",), intervals=("1d",), horizons=(5,), symbols=("JPM", "UNH", "CAT", "XOM"),
    metric="excess_mean_return", expected="excess > 0 for each symbol", null_model="circular_shift(1000, seed=42042)",
    pivot_windows=(2,), session_policy="n/a (daily)", primary_periods=(PERIOD_A,),
    holdout="these symbols were never evaluated in Phase 4D: the check is itself out-of-sample (cross-sectional)",
    min_sample=MIN_SAMPLE, classification="same rules as H1 per symbol, without the holdout requirement (frozen)")

H2 = Hypothesis(
    id="H2", title="Intraday setup states confirm after a non-zero portion of the move (confirmation lag)",
    states=("bullish_setup", "bearish_setup"), intervals=("5m", "1h"), horizons=(),
    symbols=("META", "NVDA", "MSFT", "SPY", "JPM", "UNH", "CAT", "XOM"),
    metric=("move_before_confirmation = close at pivot-confirmation bar / pivot price - 1, per setup occurrence "
            "(mean, 95% bootstrap CI); confirmation_lag_bars reported"),
    expected="bullish > 0 and bearish < 0 (a pivot low is below later closes by construction: existence is "
             "partly mechanical; the magnitude is the informative part)",
    null_model="none for existence (structural); magnitude compared with the all-bars median |2-bar return|",
    pivot_windows=(2,), session_policy="labels not used", primary_periods=(PERIOD_A, PERIOD_B),
    holdout="JPM/UNH/CAT/XOM cells were never evaluated in Phase 4D and are reported separately",
    min_sample=MIN_SAMPLE, classification="H2 rules in classify_h2 (frozen)")

H3 = Hypothesis(
    id="H3", title="1h pivot-window changes affect setup lag and sample availability",
    states=("bullish_setup", "bearish_setup"), intervals=("1h",), horizons=(1, 3, 5, 10),
    symbols=("META", "NVDA", "MSFT", "SPY", "JPM", "UNH", "CAT", "XOM"),
    metric="setup occurrences and mean |move_before_confirmation| per window (plus reported excess returns)",
    expected="as the window grows 1->4: occurrences non-increasing and mean |move_before_confirmation| "
             "non-decreasing", null_model="circular_shift(1000, seed=42042) for reported excess returns only",
    pivot_windows=(1, 2, 3, 4), session_policy="session-bound 1,3 and cross-session 1,3,5,10 (returns reported only)",
    primary_periods=(PERIOD_A,), holdout="JPM/UNH/CAT/XOM never evaluated in Phase 4D; 1h pivot windows never evaluated",
    min_sample=MIN_SAMPLE, classification="H3 rules in classify_h3 (frozen); no window is ranked or selected")

REGISTRY = (H1, H1X, H2, H3)
REGISTERED_HASHES = {h.id: h.content_hash() for h in REGISTRY}


def _status_of_share(consistent, evaluable):
    if evaluable < MIN_CELLS:
        return "INSUFFICIENT"
    share = consistent / evaluable
    if share >= SUPPORT_SHARE:
        return "SUPPORTED"
    if share < UNSUPPORT_SHARE:
        return "UNSUPPORTED"
    return "MIXED"


def classify_symbol_excess(cell):
    """One symbol for H1/H1X. ``cell``: count, excess_bps, null_p2_5_bps, null_p97_5_bps (circular shift)."""
    if cell is None or cell["count"] < MIN_SAMPLE:
        return "INSUFFICIENT"
    if cell["excess_bps"] <= 0 or abs(cell["excess_bps"]) < H1_NEAR_ZERO_BPS:
        return "UNSUPPORTED"
    if cell["null_p2_5_bps"] <= cell["excess_bps"] <= cell["null_p97_5_bps"]:
        return "UNSUPPORTED"
    return "CONSISTENT"


def classify_h1(primary, holdout):
    """``primary``/``holdout``: {symbol: cell or None}.

    - Any symbol INSUFFICIENT in the primary period: INSUFFICIENT.
    - All UNSUPPORTED: UNSUPPORTED. A mix of CONSISTENT and UNSUPPORTED: MIXED.
    - All CONSISTENT: SUPPORTED only if every symbol's holdout cell is CONSISTENT too.
      Otherwise INSUFFICIENT (no independent confirmation) when the holdout is
      missing or too small, or MIXED when it disagrees.
    """
    classes = {s: classify_symbol_excess(c) for s, c in primary.items()}
    values = set(classes.values())
    if "INSUFFICIENT" in values:
        return "INSUFFICIENT", classes
    if values == {"UNSUPPORTED"}:
        return "UNSUPPORTED", classes
    if values != {"CONSISTENT"}:
        return "MIXED", classes
    held = {s: classify_symbol_excess((holdout or {}).get(s)) for s in primary}
    if set(held.values()) == {"CONSISTENT"}:
        return "SUPPORTED", classes
    if "INSUFFICIENT" in held.values():
        return "INSUFFICIENT", classes
    return "MIXED", classes


def classify_h1x(cells):
    classes = {s: classify_symbol_excess(c) for s, c in cells.items()}
    evaluable = [v for v in classes.values() if v != "INSUFFICIENT"]
    return _status_of_share(sum(v == "CONSISTENT" for v in evaluable), len(evaluable)), classes


def classify_h2(cells):
    """``cells``: [dict(direction, count, mean, ci95)] per symbol × interval × period × direction.

    A cell is evaluable if count >= 30. It is consistent if its 95% CI excludes zero
    on the expected side (bullish > 0, bearish < 0). Status comes from the share of
    consistent cells: >= 75% SUPPORTED, < 25% UNSUPPORTED, otherwise MIXED; fewer
    than 4 evaluable cells is INSUFFICIENT. Any evaluable cell whose CI excludes zero
    on the *opposite* side caps the status at MIXED.
    """
    evaluable = [c for c in cells if c["count"] >= MIN_SAMPLE]
    consistent = [c for c in evaluable if (c["ci95"][0] > 0 if c["direction"] == "bullish" else c["ci95"][1] < 0)]
    opposite = [c for c in evaluable if (c["ci95"][1] < 0 if c["direction"] == "bullish" else c["ci95"][0] > 0)]
    status = _status_of_share(len(consistent), len(evaluable))
    if opposite and status == "SUPPORTED":
        status = "MIXED"
    return status, dict(evaluable=len(evaluable), consistent=len(consistent), opposite=len(opposite))


def classify_h3(cells):
    """``cells``: [dict(occurrences=[w1..w4], move=[w1..w4] mean |move_before_confirmation| or None)].

    A cell (symbol × direction, Period A) is evaluable if every window has >= 30
    occurrences. It is consistent if occurrences are non-increasing *and*
    |move| is non-decreasing across windows 1->4 (ties allowed). Status uses the
    same share rules as H2.
    """
    evaluable = [c for c in cells if all(n >= MIN_SAMPLE for n in c["occurrences"]) and None not in c["move"]]
    consistent = [c for c in evaluable
                  if all(a >= b for a, b in zip(c["occurrences"], c["occurrences"][1:]))
                  and all(a <= b for a, b in zip(c["move"], c["move"][1:]))]
    return _status_of_share(len(consistent), len(evaluable)), dict(evaluable=len(evaluable),
                                                                    consistent=len(consistent))
