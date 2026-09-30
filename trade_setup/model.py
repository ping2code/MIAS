"""Immutable TradeSetupAssessment model (``assessment_format_version = "phase10-v1"``).

Exactly 12 top-level fields. There are deliberately no score, confidence, ranking, sizing, target, stop or
reward/risk fields. Keyed counts are tuples of ``Count`` (never dicts), so every collection has a deterministic order.
"""
from dataclasses import dataclass

from trade_setup.canonical import to_plain

TOP_LEVEL_FIELDS = ("assessment_format_version", "assessment_id", "rules_version", "policy", "inputs", "outcome",
                    "market_bias", "execution_readiness", "candidates", "rejections", "decision_trace", "provenance")
POLICY_FIELDS = ("policy_format_version", "policy_id", "allowed_sides", "min_dte", "max_dte", "allow_same_day_expiry",
                 "allow_locked_quote", "max_spread_relative", "abs_delta_min", "abs_delta_max", "min_volume",
                 "min_open_interest", "max_premium_per_contract", "require_iv", "allow_unverified_time_basis",
                 "require_current_session_day", "require_complete_chain", "max_input_gap_seconds",
                 "block_on_market_context_opposition", "block_on_market_context_not_current")


class _Plain:
    def to_dict(self):
        return to_plain(self)


@dataclass(frozen=True)
class Count(_Plain):
    key: str
    count: int


@dataclass(frozen=True)
class Policy(_Plain):
    policy_format_version: str
    policy_id: str
    allowed_sides: tuple
    min_dte: int
    max_dte: int
    allow_same_day_expiry: bool
    allow_locked_quote: bool
    max_spread_relative: str          # canonical Decimal string, or None (rule off)
    abs_delta_min: str                # canonical Decimal strings, both or neither
    abs_delta_max: str
    min_volume: int                   # None: rule off (needs phase9-v2 when on)
    min_open_interest: int            # None: rule off (needs phase9-v2 when on)
    max_premium_per_contract: str     # None: rule off (needs phase9-v2 when on)
    require_iv: bool
    allow_unverified_time_basis: bool
    require_current_session_day: bool
    require_complete_chain: bool
    max_input_gap_seconds: int
    block_on_market_context_opposition: bool
    block_on_market_context_not_current: bool


@dataclass(frozen=True)
class MarketIntelligenceRef(_Plain):
    intelligence_id: str
    intelligence_format_version: str
    rules_version: str
    symbol: str
    as_of: str


@dataclass(frozen=True)
class OptionsIntelligenceRef(_Plain):
    options_intelligence_id: str
    options_intelligence_format_version: str
    rules_version: str
    snapshot_id: str
    underlying: str
    as_of: str


@dataclass(frozen=True)
class Inputs(_Plain):
    market_intelligence_ref: MarketIntelligenceRef
    options_intelligence_ref: OptionsIntelligenceRef
    symbol: str
    assessment_as_of: str             # the later of the two input as_of values (canonical UTC)
    input_gap_seconds: int            # |difference| in whole seconds (truncated)


@dataclass(frozen=True)
class Outcome(_Plain):
    status: str                       # setup_candidates | no_setup (the frozen phase10-v1 vocabulary)
    no_setup_reasons: tuple           # sorted, closed vocabulary; empty unless no_setup


@dataclass(frozen=True)
class Invalidation(_Plain):
    """Market-state invalidation descriptor (D10): the setup holds only while the pattern remains."""
    rule: str                         # pattern_must_remain
    required_pattern: str             # all_bullish | all_bearish
    established_by: str               # the MarketIntelligence id that established the bias


@dataclass(frozen=True)
class MarketBias(_Plain):
    state: str                        # bullish | bearish | conflicting | insufficient | non_directional | partially_directional
    side: str                         # call | put | None
    pattern: str                      # MarketIntelligence timeframe_structure.pattern (copied)
    technical_status: str             # MarketIntelligence evidence_coverage.technical_status (copied)
    invalidation: Invalidation        # None unless directional


@dataclass(frozen=True)
class ExecutionReadiness(_Plain):
    """Chain-level facts from OptionsIntelligence; no per-contract filtering (that is Phase 10C)."""
    options_intelligence_format_version: str
    contract_count: int
    truncated: bool
    quote_state_counts: tuple
    usable_two_sided_quote_count: int  # complete quotes, plus locked quotes when the policy allows them
    greeks_time_basis_counts: tuple
    iv_time_basis_counts: tuple
    day_session_relation_counts: tuple
    numeric_activity_facts_available: bool   # True for phase9-v2 input
    shares_per_contract_present_count: int   # None for phase9-v1 input


@dataclass(frozen=True)
class TraceStep(_Plain):
    step: int
    rule: str
    result: str                       # pass | fail | not_evaluated
    reason: str                       # the no-setup reason on fail; a not-evaluated reason; None on pass
    pointers: tuple                   # sorted mi:/oi:/policy: pointers


@dataclass(frozen=True)
class Provenance(_Plain):
    market_intelligence_id: str
    options_intelligence_id: str
    options_intelligence_format_version: str
    policy_id: str
    rules_version: str
    pointer_version: str


@dataclass(frozen=True)
class TradeSetupAssessment(_Plain):
    assessment_format_version: str
    assessment_id: str
    rules_version: str
    policy: Policy
    inputs: Inputs
    outcome: Outcome
    market_bias: MarketBias
    execution_readiness: ExecutionReadiness
    candidates: tuple                 # empty in Phase 10B (screening is Phase 10C)
    rejections: tuple                 # empty in Phase 10B
    decision_trace: tuple
    provenance: Provenance

    def body(self):
        data = self.to_dict()
        data.pop("assessment_id")
        return data


@dataclass(frozen=True)
class PreScreeningEligibility(_Plain):
    """Internal, unsealed result of the global gates when every one passed: the input to contract screening.

    Not a TradeSetupAssessment, not phase10-v1, not an outcome, never persisted and not a public contract. It has no
    id or format version. ``decision_trace`` holds the global-gate steps only; screening appends its own step.
    """
    eligible_for_contract_screening: bool   # always True (a failed gate gives a no_setup assessment instead)
    eligible_side: str                      # call | put (equal to market_bias.side)
    policy: Policy
    inputs: Inputs
    market_bias: MarketBias
    execution_readiness: ExecutionReadiness
    decision_trace: tuple                   # global-gate steps 1..7, each pass or not_evaluated
    provenance: Provenance
