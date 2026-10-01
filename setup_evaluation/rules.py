"""Frozen Setup Evaluation rules (``phase11-rules-v1``) and frozen local copies of the upstream contracts it reads.

Upstream constants are copied (never imported at runtime); tests prove every copy equals the real package's.
"""
import re

EVALUATION_FORMAT_VERSION = "phase11-v1"
RULES_VERSION = "phase11-rules-v1"
POINTER_VERSION = "phase11-pointer-v1"
PROTOCOL_FORMAT_VERSION = "phase11-evaluation-protocol-v1"
SCHEDULE_FORMAT_VERSION = "phase11-sessions-v1"

CONTENT_ID = re.compile(r"sha256:[0-9a-f]{64}")
SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")   # = market_data.models.SYMBOL (test-pinned)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
POINTER = re.compile(r"(setup|snapshot|schedule|protocol|check):[a-z_.\[\]*]+")

# --- Production activation (decision H; correction 3) ---
# Pinned to the checked-in production protocol
# (setup_evaluation/protocols/phase11-evaluation-protocol-v1.production.json). The activation is authoritative only
# once the activation commit is merged into main. Assessments with assessment_as_of < PRODUCTION_PROSPECTIVE_START are
# permanently ineligible (no backfill).
PRODUCTION_PROTOCOL_ID = "sha256:69e050733aacf4fd66c2e1a050d750d833d747b37ef1ec233a0094c0e9900a5c"
PRODUCTION_PROSPECTIVE_START = "2026-10-02T13:30:00+00:00"   # the next regular XNYS open after the activation commit
PROTOCOL_PURPOSES = ("test", "production")

# --- Horizons (decision C; correction 1): the n-th regular XNYS session whose regular_open > assessment_as_of ---
HORIZONS = {"session_1": 1, "session_5": 5}
HORIZON_RULE = "nth_regular_session_with_open_strictly_after_assessment_as_of"
WINDOW_SECONDS = 1800          # [target_close - 30 minutes, target_close], inclusive, backward-looking only
WINDOW_RULE = "inclusive_backward_from_target_close"

# --- Mark (decision F) and returns (decision G) ---
MARK = "liquidation_reference_bid"
ENTRY = "entry_reference_ask"
QUOTE_REQUIREMENTS = ("quote_present", "time_basis_observed_at", "two_sided", "non_crossed", "observed_at_in_window")
RETURN_PLACES = 8
RETURN_ROUNDING = "ROUND_HALF_EVEN"

# --- Closed vocabularies ---
OBSERVED = "observed"
OUTCOME_STATUSES = ("observed", "quote_unavailable", "quote_not_two_sided", "quote_crossed", "quote_after_cutoff",
                    "quote_stale", "quote_timing_unverified", "contract_absent", "observation_incomplete",
                    "expired_before_target")
RETURN_STATUSES = ("computed", "entry_reference_zero", "not_observed")
DOLLAR_STATUSES = ("computed", "multiplier_unavailable", "contract_terms_changed", "not_observed")
RELATION_STATUSES = ("invalidated_by_target", "no_invalidation_observed", "not_evaluated")
CANDIDATE_INCLUSION = "all_assessment_candidates_in_canonical_order"
INVALIDATION_RULE = "supplied_checks_only"
MISSING_CONTRACT_RULE = "contract_absent_if_complete_chain_else_observation_incomplete"
MULTIPLIER_RULE = "assessment_candidate_multiplier_never_assumed"
# Explicitly excluded from v1 (recorded in the protocol; none of these exists in the implementation). Sorted.
EXCLUSIONS = ("expiration_settlement", "labels", "mae", "mfe", "portfolio_pnl", "position_sizing", "ranking",
              "retrospective_selection")
# Fixed decision-trace steps and pointers (every step passes in a sealed object; failures are input errors).
TRACE_STEPS = (
    ("protocol", ("protocol:protocol_id",)),
    ("setup", ("setup:candidates[*]", "setup:outcome.status")),
    ("symbol_match", ("setup:inputs.symbol", "snapshot:underlying")),
    ("horizon_resolution", ("schedule:sessions[*]", "setup:inputs.assessment_as_of")),
    ("observation_window", ("snapshot:as_of", "snapshot:session.calendar_state", "snapshot:session.session_date")),
    ("candidate_set", ("setup:candidates[*].source.contract_id", "snapshot:contracts[*].identity.contract_id")),
    ("invalidation_relation", ("check:market_intelligence_ref.as_of", "check:result", "check:setup_ref.assessment_id")),
)
TRACE_RULES = tuple(rule for rule, _ in TRACE_STEPS)

# --- Phase 10 TradeSetupAssessment (sealed upstream contract; local structural validation) ---
ASSESSMENT_FORMAT_VERSION, ASSESSMENT_RULES_VERSION = "phase10-v1", "phase10-rules-v1"
ASSESSMENT_TOP_LEVEL = ("assessment_format_version", "assessment_id", "rules_version", "policy", "inputs", "outcome",
                        "market_bias", "execution_readiness", "candidates", "rejections", "decision_trace",
                        "provenance")
SETUP_CANDIDATES = "setup_candidates"
OPTION_SIDES = ("call", "put")
# --- Phase 10D InvalidationCheck ---
INVALIDATION_FORMAT_VERSION, INVALIDATION_RULES_VERSION = "phase10-invalidation-v1", "phase10-invalidation-rules-v1"
INVALIDATION_TOP_LEVEL = ("invalidation_format_version", "invalidation_id", "rules_version", "setup_ref",
                          "market_intelligence_ref", "symbol", "required_market_state", "observed_market_state",
                          "result", "reason", "decision_trace", "provenance")
INVALIDATION_RESULTS = ("holds", "invalidated", "not_evaluable")
# --- Phase 9 OptionsSnapshot ---
SNAPSHOT_FORMAT_VERSION = "phase9-snapshot-v1"
SNAPSHOT_TOP_LEVEL = ("snapshot_format_version", "snapshot_id", "underlying", "as_of", "session", "underlying_price",
                      "scope", "contracts", "exclusions", "provenance")
FACT_STATUSES = ("present", "missing", "unavailable", "excluded_after_as_of")
TIME_BASES = ("observed_at", "provider_as_of_date", "provider_snapshot_unverified", "unavailable")
CALENDAR_STATES = ("regular", "pre", "post", "closed")
QUOTE_FIELDS = ("status", "bid", "ask", "observed_at", "time_basis")
