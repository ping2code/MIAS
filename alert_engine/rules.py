"""Frozen Phase 12 alert rules (``phase12-rules-v1``): versions, the closed v1 alert codes and their fixed shapes.

Every alert code has a fixed subject kind, source roles, transition shape, fact set and decision trace. An AlertEvent
exists only when its rule fired, so every trace step is ``pass``. There is no severity, score, ranking,
recommendation, AI field or delivery field anywhere.
"""
import re

ALERT_FORMAT_VERSION = "phase12-v1"
RULES_VERSION = "phase12-rules-v1"
POINTER_VERSION = "phase12-pointer-v1"

CONTENT_ID = re.compile(r"sha256:[0-9a-f]{64}")
POINTER = re.compile(r"(assessment|check|previous|current):[a-z_.\[\]*]+")

SETUP_AVAILABLE, SETUP_INVALIDATED, MARKET_PATTERN_CHANGED = (
    "setup_available", "setup_invalidated", "market_pattern_changed")
ALERT_CODES = (SETUP_AVAILABLE, SETUP_INVALIDATED, MARKET_PATTERN_CHANGED)

# Subject kinds: a setup (one TradeSetupAssessment) or a symbol.
SETUP, SYMBOL = "setup", "symbol"
# Source roles (sorted canonical order) and object kinds.
ROLES = ("current", "previous", "setup")
TRADE_SETUP_ASSESSMENT, INVALIDATION_CHECK, MARKET_INTELLIGENCE = (
    "trade_setup_assessment", "invalidation_check", "market_intelligence")

# Upstream sealed contracts (values pinned to the real packages by tests).
ASSESSMENT_VERSIONS = ("phase10-v1", "phase10-rules-v1")
INVALIDATION_VERSIONS = ("phase10-invalidation-v1", "phase10-invalidation-rules-v1")
MI_VERSIONS = ("phase8-v1", "phase8-rules-v1")
SETUP_CANDIDATES = "setup_candidates"
INVALIDATED = "invalidated"
PATTERNS = ("all_bullish", "all_bearish", "all_non_directional", "opposed", "partially_directional", "incomplete")
SIDES = ("call", "put")
DIRECTIONAL_BIAS = ("bullish", "bearish")
TECHNICAL_STATUSES = ("available", "partial", "unavailable")

# Per code: subject kind, source roles -> object kind, facts (sorted keys) and the fixed decision trace.
SHAPES = {
    SETUP_AVAILABLE: dict(
        subject=SETUP, roles={"current": TRADE_SETUP_ASSESSMENT},
        facts=("candidate_count", "eligible_side", "market_bias", "symbol"), transition=False,
        trace=(("input_valid", ("assessment:assessment_id",)),
               ("outcome_setup_candidates", ("assessment:outcome.status",)))),
    SETUP_INVALIDATED: dict(
        subject=SETUP, roles={"current": INVALIDATION_CHECK, "setup": TRADE_SETUP_ASSESSMENT},
        facts=("observed_pattern", "observed_technical_status", "required_pattern", "side", "symbol"),
        transition=False,
        trace=(("input_valid", ("check:invalidation_id",)),
               ("result_invalidated", ("check:reason", "check:result")))),
    MARKET_PATTERN_CHANGED: dict(
        subject=SYMBOL, roles={"current": MARKET_INTELLIGENCE, "previous": MARKET_INTELLIGENCE},
        facts=("elapsed_seconds", "symbol"), transition=True,
        trace=(("inputs_valid", ("current:intelligence_id", "previous:intelligence_id")),
               ("same_symbol", ("current:synthesis_ref.symbol", "previous:synthesis_ref.symbol")),
               ("as_of_ordered", ("current:synthesis_ref.as_of", "previous:synthesis_ref.as_of")),
               ("comparable", ("current:synthesis_ref.synthesis_format_version",
                               "current:synthesis_ref.synthesis_rules_version",
                               "previous:synthesis_ref.synthesis_format_version",
                               "previous:synthesis_ref.synthesis_rules_version")),
               ("pattern_changed", ("current:timeframe_structure.pattern",
                                    "previous:timeframe_structure.pattern")))),
}
