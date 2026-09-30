"""Frozen, versioned Market Intelligence rules (``phase8-rules-v1``): constant tables and pure functions only.

Everything is descriptive. There is no weighting, ranking, scoring, confidence, severity or staleness threshold, and
nothing is tuned on outcome data. The synthesis vocabulary and its pure rule functions come from
``evidence_synthesis.rules`` (the only project dependency), so re-derivation uses exactly the Phase 7G rules.
"""
from evidence_synthesis import rules as synthesis_rules

INTELLIGENCE_FORMAT_VERSION = "phase8-v1"
RULES_VERSION = "phase8-rules-v1"

# The only synthesis contracts this rule set understands. Anything else is invalid input (fail closed).
SUPPORTED_SYNTHESIS_FORMATS = frozenset({"phase7g-v1"})
SUPPORTED_SYNTHESIS_RULES = frozenset({"phase7g-rules-v1"})

INTERVALS = synthesis_rules.INTERVALS
PAIRS = synthesis_rules.PAIRS
BASES = synthesis_rules.BASES
SELF_REFERENCE = synthesis_rules.SELF_REFERENCE
BULLISH, BEARISH = synthesis_rules.BULLISH, synthesis_rules.BEARISH
NON_DIRECTIONAL, UNAVAILABLE = synthesis_rules.NON_DIRECTIONAL, synthesis_rules.UNAVAILABLE
AGREE, OPPOSE = synthesis_rules.AGREE, synthesis_rules.OPPOSE
POSITIVE, NEGATIVE, ZERO = synthesis_rules.POSITIVE, synthesis_rules.NEGATIVE, synthesis_rules.ZERO
RELATIONS = (AGREE, OPPOSE, NON_DIRECTIONAL, UNAVAILABLE)
VALUE_SIGNS = (POSITIVE, NEGATIVE, ZERO, UNAVAILABLE)
INSUFFICIENT_DATA = "insufficient_data"
SEC_IDENTITY_VERSION = "sec-v1"  # Phase 7C identity version of SEC filing items.

# Timeframe structure.
OPPOSITION_SHAPES = ("none", "isolated_interval", "single_pair", "not_applicable")

# Market-context sign profiles (own returns and relative returns are always profiled separately).
SIGN_PROFILES = ("all_positive", "all_negative", "all_zero", "mixed", "unavailable")

# Conflicts: evidence that disagrees. Only the synthesis opposition contradictions qualify; gaps (unavailable,
# misaligned or out-of-session evidence) are coverage and attention facts, never conflicts.
CONFLICT_CODES = (synthesis_rules.TIMEFRAME_OPPOSITION, synthesis_rules.MARKET_CONTEXT_OPPOSES_TIMEFRAME)

# Attention: structural conditions a human reviewer should look at. Not investment importance; unordered categories.
CONFLICT, GAP, PRESENCE = "conflict", "gap", "presence"
ATTENTION_CATEGORIES = (CONFLICT, GAP, PRESENCE)
ATTENTION_CODES = {
    "timeframe_opposition_present": CONFLICT,
    "market_context_opposition_present": CONFLICT,
    "evidence_incomplete": GAP,
    "context_session_mismatch": GAP,
    "comparison_misaligned": GAP,
    "market_context_not_current": GAP,
    "news_unavailable": GAP,
    "sec_filing_present": PRESENCE,
}
# Upstream Phase 7B freshness statuses that mean "not current". market_closed and current are not problems.
NOT_CURRENT_FRESHNESS = frozenset({"lagging", "no_data", "unknown"})
MISALIGNED = "misaligned"

# Phase 8B: comparison with an explicit, caller-supplied previous synthesis (same symbol, earlier as_of).
COMPARABLE, NOT_COMPARABLE = "comparable", "not_comparable"
COMPARISON_STATUSES = (COMPARABLE, NOT_COMPARABLE)
FORMAT_VERSION_MISMATCH = "synthesis_format_version_mismatch"
RULES_VERSION_MISMATCH = "synthesis_rules_version_mismatch"
NOT_COMPARABLE_REASONS = (FORMAT_VERSION_MISMATCH, RULES_VERSION_MISMATCH)
DOMAINS = ("market_context", "technical", "news")
ALIGNED_PATTERNS = ("all_bullish", "all_bearish")
# Contradiction codes that are gaps (missing, misaligned or out-of-session evidence), as opposed to CONFLICT_CODES.
GAP_CODES = (synthesis_rules.TIMEFRAME_UNAVAILABLE, synthesis_rules.MARKET_CONTEXT_UNAVAILABLE,
             synthesis_rules.COMPARISON_MISALIGNED, synthesis_rules.CONTEXT_SESSION_MISMATCH,
             synthesis_rules.NEWS_UNAVAILABLE)
# Closed, descriptive transition codes. None of them says a change is good or bad.
TRANSITION_CODES = (
    "timeframe_state_changed", "timeframe_direction_changed", "pattern_changed", "entered_alignment",
    "exited_alignment", "opposition_appeared", "opposition_resolved", "gap_appeared", "gap_resolved",
    "domain_became_available", "domain_became_unavailable", "reference_sign_changed", "news_identities_added",
    "news_identities_removed")


def sign_profile(signs):
    """all_positive | all_negative | all_zero | mixed | unavailable over the available signs (no weighting)."""
    available = {s for s in signs if s != UNAVAILABLE}
    if not available:
        return "unavailable"
    if len(available) > 1:
        return "mixed"
    return {POSITIVE: "all_positive", NEGATIVE: "all_negative", ZERO: "all_zero"}[available.pop()]


def opposition_shape(pattern, directions, relations):
    """Shape of any opposition among the three timeframes, by fixed rules (no ranking of timeframes).

    ``directions``: {interval: direction}; ``relations``: {(first, second): relation}. Returns (shape, isolated).
    """
    if pattern == "incomplete":
        return "not_applicable", None
    opposing = [pair for pair in PAIRS if relations[pair] == OPPOSE]
    if not opposing:
        return "none", None
    directional = [i for i in INTERVALS if directions[i] in (BULLISH, BEARISH)]
    if len(directional) == 3 and len(opposing) == 2:
        isolated = (set(opposing[0]) & set(opposing[1])).pop()
        return "isolated_interval", isolated
    if len(directional) == 2 and len(opposing) == 1:
        return "single_pair", None
    raise ValueError("opposition shape is not total for this input")  # Unreachable for consistent syntheses.
