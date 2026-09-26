"""Phase 6 frozen prospective-validation registry: H1', H2' and the collection/gate protocol (research only).

Everything here is frozen *before* prospective collection starts:

- the hypothesis definitions and their evidence rules;
- the collection universe and intervals;
- the prospective-start rule;
- the evaluation gate.

Each item has a SHA-256 content hash, pinned by ``tests/test_prospective_validation.py``.

- ``REGISTRY_HASH`` (over the hypothesis and protocol hashes) is stored in every
  evidence-ledger row and is part of the evidence identity.
- Any revision must use new IDs (H1'', H2'', …) and therefore a new registry hash.
  It can never overwrite or mix with ``phase6-v1`` evidence.

This module deliberately never imports ``evaluation``, so the live runner can use
it without loading research code.
"""
from dataclasses import asdict, dataclass
import hashlib
import json

REGISTRY_VERSION = "phase6-v1"
PROSPECTIVE_FORMAT_VERSION = "phase6-v1"
ENGINE_VERSION = "phase4c-v2"
SEED = 42042
NULL_VERSION = "circular_shift-v1"
MIN_SAMPLE = 30


def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Protocol:
    version: str
    collection_symbols: tuple
    collection_intervals: tuple
    pivot_window: int
    engine_version: str
    provider: str
    prospective_start_rule: str
    earliest_evaluation_rule: str
    min_complete_sessions: int
    complete_session_rule: str
    session_completeness_rule: str
    evaluation_data_rule: str
    seed: int
    null_version: str

    def content_hash(self):
        return _hash(asdict(self))


@dataclass(frozen=True)
class ProspectiveHypothesis:
    id: str
    version: str
    description: str
    state: tuple
    interval: tuple
    horizon: tuple
    symbols: tuple
    config: str
    metric: str
    weighting: str
    baseline: str
    null_method: str
    min_sample: str
    verdict_rules: str

    def content_hash(self):
        return _hash(asdict(self))


PROTOCOL = Protocol(
    version=REGISTRY_VERSION,
    collection_symbols=("META", "NVDA", "MSFT", "SPY", "JPM", "UNH", "CAT", "XOM"),
    collection_intervals=("5m", "1h", "1d"),
    pivot_window=2,
    engine_version=ENGINE_VERSION,
    provider="polygon",
    prospective_start_rule=("first XNYS session whose regular-market open occurs strictly after the committer "
                            "timestamp of the Phase 6 freeze commit (pinned in evidence/prospective_start.json)"),
    earliest_evaluation_rule="first XNYS session on or after prospective_start_session + 6 calendar months",
    min_complete_sessions=120,
    complete_session_rule=("a prospective session counts only if accepted (collected) ledger evidence exists for "
                           "every collection symbol x interval and none of those identities has a conflict"),
    session_completeness_rule=("a session's bars are complete only if the session has closed before the data cutoff "
                               "and the bar count equals the XNYS expectation: 5m 78 (42 early close), 1h 7 (4), "
                               "1d 1; otherwise the attempt fails closed as incomplete_session"),
    evaluation_data_rule=("at the gate, bars are re-fetched; states may use pre-start history as indicator warm-up, "
                          "but every observation bar t and its full forward window must lie in accepted, "
                          "complete prospective sessions whose re-fetched bar_content_hash equals the ledger; "
                          "sessions whose hash differs are excluded and reported"),
    seed=SEED,
    null_version=NULL_VERSION)

H1P = ProspectiveHypothesis(
    id="H1'", version="1",
    description="Pooled, equal-symbol-weighted daily bearish_setup excess return across independent sectors",
    state=("bearish_setup",), interval=("1d",), horizon=(5,),
    symbols=("META", "MSFT", "JPM", "UNH", "CAT", "XOM"),
    config="production (pivot_window 2, all production thresholds)",
    metric=("pooled_excess = mean over qualifying symbols of (symbol mean 5-bar forward close-to-close return of "
            "prospective bearish_setup bars - symbol mean 5-bar forward return of all prospective bars)"),
    weighting="equal per qualifying symbol (not per observation)",
    baseline="per-symbol all-bars drift over the same prospective observation window",
    null_method=("per-symbol circular shift of the prospective state sequence against unchanged returns (shift in "
                 "[12, N-12]), 1000 iterations, seed 42042, stream key per symbol; pooled null = equal-weight mean "
                 "of the per-symbol null excesses at the same iteration index"),
    min_sample="qualifying symbol: >= 5 observations; pooled: >= 30 observations and >= 4 qualifying symbols",
    verdict_rules=("INSUFFICIENT if the pooled sample rule fails; UNSUPPORTED if pooled_excess <= 0, or "
                   "abs(pooled_excess) < 10 bps, or pooled_excess <= pooled null p97.5; SUPPORTED if pooled_excess > "
                   "pooled null p97.5 and >= 2/3 of qualifying symbols have positive excess; MIXED otherwise"))

H2P = ProspectiveHypothesis(
    id="H2'", version="1",
    description="Intraday setup confirmation lag with pivot-kind-specific move semantics",
    state=("bullish_setup", "bearish_setup"), interval=("5m", "1h"), horizon=(),
    symbols=("META", "NVDA", "MSFT", "SPY", "JPM", "UNH", "CAT", "XOM"),
    config="production (pivot_window 2)",
    metric=("per setup occurrence (first bar of a setup run): completing pivot = the more recently confirmed of "
            "the snapshot's significant high/low; kind = low or high; confirmation_lag_bars = confirmed index - "
            "pivot index; move_before_confirmation = close at the confirmation bar / pivot price - 1. Expected "
            "sign by kind: low pivot > 0, high pivot < 0. Primary cells are direction-aligned kinds (bullish_setup "
            "completed by a low, bearish_setup completed by a high); counter-kind cells are descriptive only"),
    weighting="none (cell-level)",
    baseline="magnitude reference: median abs(2-bar close-to-close return) of all prospective bars, same run",
    null_method="none for existence (structural); magnitude criterion below",
    min_sample="primary cell (symbol x interval x aligned kind) evaluable if >= 30 occurrences",
    verdict_rules=("a primary cell is consistent if its 95% bootstrap CI of mean move_before_confirmation (seed "
                   "42042, 1000 iterations) excludes zero on the expected side AND mean abs(move) >= 0.25 x the "
                   "magnitude reference; >= 75% of evaluable cells consistent: SUPPORTED; < 25%: UNSUPPORTED; "
                   "otherwise MIXED; fewer than 4 evaluable cells: INSUFFICIENT; any evaluable cell significant on "
                   "the opposite side caps the verdict at MIXED"))

HYPOTHESES = (H1P, H2P)
HYPOTHESIS_HASHES = {h.id: h.content_hash() for h in HYPOTHESES}
PROTOCOL_HASH = PROTOCOL.content_hash()
REGISTRY_HASH = _hash(dict(hypotheses=HYPOTHESIS_HASHES, protocol=PROTOCOL_HASH, version=REGISTRY_VERSION))


def registry_identity():
    """The registry fields stored in each evidence-ledger row."""
    return dict(version=REGISTRY_VERSION, hash=REGISTRY_HASH, hypotheses=dict(HYPOTHESIS_HASHES))
