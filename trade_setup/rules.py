"""Frozen Trade Setup rules (``phase10-rules-v1``) and frozen local copies of the upstream contracts it reads.

The upstream constants (Phase 8 MarketIntelligence, Phase 9 OptionsIntelligence) are copied here so trade_setup
never imports those packages at runtime; tests import the real packages to prove every copy is identical.
"""
import re

ASSESSMENT_FORMAT_VERSION = "phase10-v1"
RULES_VERSION = "phase10-rules-v1"
POLICY_FORMAT_VERSION = "phase10-policy-v1"
POINTER_VERSION = "phase10-pointer-v1"

CONTENT_ID = re.compile(r"sha256:[0-9a-f]{64}")
SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")   # = market_data.models.SYMBOL (test-pinned)

# --- Phase 8 MarketIntelligence (sealed upstream contract; structural validation only) ---
MI_FORMAT_VERSION, MI_RULES_VERSION = "phase8-v1", "phase8-rules-v1"
MI_TOP_LEVEL = ("intelligence_format_version", "intelligence_id", "rules_version", "synthesis_ref", "comparison",
                "evidence_coverage", "timeframe_structure", "market_context_alignment", "event_presence",
                "conflicts", "transitions", "attention", "provenance")
MI_SYNTHESIS_REF = ("synthesis_id", "synthesis_format_version", "synthesis_rules_version", "packet_id", "symbol",
                    "as_of")
MI_PATTERNS = ("all_bullish", "all_bearish", "all_non_directional", "opposed", "partially_directional", "incomplete")
MI_TECHNICAL_STATUSES = ("available", "partial", "unavailable")
MI_CONFLICT_CODES = ("timeframe_opposition", "market_context_opposes_timeframe")
MI_ATTENTION_CODES = {
    "timeframe_opposition_present": "conflict", "market_context_opposition_present": "conflict",
    "evidence_incomplete": "gap", "context_session_mismatch": "gap", "comparison_misaligned": "gap",
    "market_context_not_current": "gap", "news_unavailable": "gap", "sec_filing_present": "presence",
}

# --- Phase 9 OptionsIntelligence (sealed upstream contract; structural validation only) ---
OI_FORMATS = {"phase9-v1": "phase9-rules-v1", "phase9-v2": "phase9-rules-v2"}
OI_V2 = "phase9-v2"
OI_TOP_LEVEL = ("options_intelligence_format_version", "options_intelligence_id", "rules_version", "snapshot_ref",
                "market_intelligence_ref", "underlying", "chain_completeness", "expirations", "contracts",
                "activity", "volatility", "attention", "provenance")
OI_SNAPSHOT_REF = ("snapshot_id", "snapshot_format_version", "underlying", "as_of")
OI_CONTRACT_V1 = ("contract_id", "provider_symbol", "option_type", "expiration", "strike", "dte_calendar_days",
                  "expires_on_as_of_date", "strike_relation", "strike_distance", "strike_distance_relative",
                  "quote_state", "mid", "spread_absolute", "spread_relative", "spread_relative_reason",
                  "volume_state", "open_interest_state", "volume_exceeds_open_interest", "implied_volatility",
                  "greeks", "day", "source_pointers")
OI_CONTRACT_V2_EXTRA = ("current_session_volume", "open_interest_value", "open_interest_time_basis",
                        "shares_per_contract")
OI_QUOTE_STATES = ("complete", "locked", "crossed", "bid_missing", "ask_missing", "both_missing", "unavailable",
                   "excluded_after_as_of")
OI_ACTIVITY_STATES = ("positive", "zero", "missing", "unavailable", "excluded_after_as_of")
OI_SESSION_RELATIONS = ("current_session", "previous_session", "older_session", "unavailable")
OI_TIME_BASES = ("observed_at", "provider_as_of_date", "provider_snapshot_unverified", "unavailable")
OPTION_SIDES = ("call", "put")

# --- Market bias (locked, D3): no score, confidence or weighting ---
BIAS_BY_PATTERN = {"all_bullish": "bullish", "all_bearish": "bearish", "opposed": "conflicting",
                   "incomplete": "insufficient", "all_non_directional": "non_directional",
                   "partially_directional": "partially_directional"}
BIAS_STATES = ("bullish", "bearish", "conflicting", "insufficient", "non_directional", "partially_directional")
SIDE_BY_BIAS = {"bullish": "call", "bearish": "put"}
PATTERN_BY_BIAS = {"bullish": "all_bullish", "bearish": "all_bearish"}

# --- No-setup reasons (locked v1 set; underlying_price_unavailable deliberately excluded) ---
NO_SETUP_REASONS = ("market_evidence_insufficient", "market_evidence_conflicting",
                    "market_evidence_non_directional", "market_evidence_partially_directional",
                    "side_not_allowed_by_policy", "context_gate_blocked", "inputs_not_contemporaneous",
                    "execution_data_unavailable", "source_timing_unverified", "options_chain_truncated",
                    "no_candidate_satisfies_policy")
REASON_BY_BIAS = {"insufficient": "market_evidence_insufficient", "conflicting": "market_evidence_conflicting",
                  "non_directional": "market_evidence_non_directional",
                  "partially_directional": "market_evidence_partially_directional"}

# --- Outcome ---
# The frozen phase10-v1 outcome vocabulary (exactly two values).
SETUP_CANDIDATES, NO_SETUP = "setup_candidates", "no_setup"
OUTCOME_STATUSES = (SETUP_CANDIDATES, NO_SETUP)

# --- Decision trace ---
PASS, FAIL, NOT_EVALUATED = "pass", "fail", "not_evaluated"
TRACE_RESULTS = (PASS, FAIL, NOT_EVALUATED)
# global_gate_failed: contract screening is never evaluated once any global gate has failed.
NOT_EVALUATED_REASONS = ("policy_disabled", "market_bias_not_directional", "global_gate_failed")
GLOBAL_GATE_RULES = ("input_contemporaneity", "market_bias", "side_allowed", "context_opposition_gate",
                     "context_current_gate", "chain_completeness", "execution_data_readiness")
TRACE_RULES = GLOBAL_GATE_RULES + ("contract_screening",)
POINTER = re.compile(r"(mi|oi|policy):[a-z_.\[\]*]+")

# Numeric policy rules that need phase9-v2 facts (R4): a phase9-v1 input with any of them enabled is an error.
V2_POLICY_RULES = ("min_volume", "min_open_interest", "max_premium_per_contract")

# --- Contract screening (Phase 10C) ---
# Step-8 results: (pass, candidates_available) | (fail, no_candidate_satisfies_policy) |
# (not_evaluated, global_gate_failed).
CANDIDATES_AVAILABLE = "candidates_available"
SCREENING_STEP_RESULTS = ((PASS, CANDIDATES_AVAILABLE), (FAIL, "no_candidate_satisfies_policy"),
                          (NOT_EVALUATED, "global_gate_failed"))
# The no_setup reasons a screened (globally eligible) input can carry.
SCREENING_NO_SETUP_REASONS = ("execution_data_unavailable", "no_candidate_satisfies_policy", "source_timing_unverified")

# Frozen per-contract rejection vocabulary (10A/10C; multiplier_unavailable added by the 10C decision).
REJECTION_REASONS = ("side_mismatch", "expiration_outside_policy", "same_day_expiry_excluded", "quote_unavailable",
                     "quote_one_sided", "quote_crossed", "quote_locked_excluded", "quote_after_as_of",
                     "spread_relative_unavailable", "spread_above_policy", "delta_unavailable",
                     "delta_outside_policy", "time_basis_unverified", "iv_unavailable", "volume_below_policy",
                     "open_interest_below_policy", "day_not_current_session", "premium_above_policy",
                     "multiplier_unavailable")
# Unusable quote states (complete is usable; locked is usable only when the policy allows it).
QUOTE_STATE_REASONS = {"unavailable": "quote_unavailable", "bid_missing": "quote_one_sided",
                       "ask_missing": "quote_one_sided", "both_missing": "quote_one_sided",
                       "crossed": "quote_crossed", "locked": "quote_locked_excluded",
                       "excluded_after_as_of": "quote_after_as_of"}
VERIFIED_TIME_BASES = ("observed_at", "provider_as_of_date")
PREMIUM_RISK_STATUSES = ("computed", "multiplier_unavailable")

# The frozen screening order: (rule, policy_field, source_pointers). Every contract is evaluated against every
# enabled rule in this order (never stopping at the first failure); the order never depends on the data.
SCREENING_RULES = (
    ("side", "allowed_sides", ("oi:contracts[*].option_type",)),
    ("dte_minimum", "min_dte", ("oi:contracts[*].dte_calendar_days",)),
    ("dte_maximum", "max_dte", ("oi:contracts[*].dte_calendar_days",)),
    ("same_day_expiry", "allow_same_day_expiry", ("oi:contracts[*].dte_calendar_days",)),
    ("quote_state", "allow_locked_quote", ("oi:contracts[*].quote_state",)),
    ("spread_availability", "max_spread_relative", ("oi:contracts[*].spread_relative",)),
    ("spread_threshold", "max_spread_relative", ("oi:contracts[*].spread_relative",)),
    ("delta_minimum", "abs_delta_min", ("oi:contracts[*].greeks.delta", "oi:contracts[*].greeks.out_of_bounds_fields")),
    ("delta_maximum", "abs_delta_max", ("oi:contracts[*].greeks.delta", "oi:contracts[*].greeks.out_of_bounds_fields")),
    ("greeks_time_basis", "allow_unverified_time_basis", ("oi:contracts[*].greeks.time_basis",)),
    ("iv_availability", "require_iv", ("oi:contracts[*].implied_volatility.value",)),
    ("iv_time_basis", "allow_unverified_time_basis", ("oi:contracts[*].implied_volatility.time_basis",)),
    ("day_session", "require_current_session_day", ("oi:contracts[*].day.session_relation",)),
    ("volume_threshold", "min_volume", ("oi:contracts[*].current_session_volume",)),
    ("open_interest_threshold", "min_open_interest", ("oi:contracts[*].open_interest_value",)),
    ("open_interest_time_basis", "allow_unverified_time_basis", ("oi:contracts[*].open_interest_time_basis",)),
    ("multiplier_availability", "max_premium_per_contract", ("oi:contracts[*].shares_per_contract",)),
    ("premium_cap", "max_premium_per_contract", ("oi:contracts[*].mid", "oi:contracts[*].shares_per_contract",
                                                 "oi:contracts[*].spread_absolute")),
)
SCREENING_RULE_NAMES = tuple(rule for rule, _, _ in SCREENING_RULES)
