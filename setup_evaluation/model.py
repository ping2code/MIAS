"""Immutable Setup Evaluation model: SetupEvaluation (``phase11-v1``), the evaluation protocol and the
SessionSchedule. No score, rank, label, average, winner or "best" field exists anywhere."""
from dataclasses import dataclass

from setup_evaluation.canonical import to_plain

EVALUATION_FIELDS = ("evaluation_format_version", "evaluation_id", "rules_version", "protocol_ref", "setup_ref",
                     "observation_ref", "schedule_ref", "horizon", "candidate_outcomes", "summary",
                     "invalidation_relation", "decision_trace", "provenance")
PROTOCOL_FIELDS = ("protocol_format_version", "protocol_id", "purpose", "prospective_start",
                   "evaluation_format_version", "rules_version", "schedule_format_version", "horizons",
                   "horizon_rule", "window_seconds", "window_rule", "mark", "entry", "quote_requirements",
                   "return_places", "return_rounding", "outcome_statuses", "return_statuses", "dollar_statuses",
                   "relation_statuses", "candidate_inclusion", "invalidation_rule", "missing_contract_rule",
                   "multiplier_rule", "exclusions")
SCHEDULE_FIELDS = ("schedule_format_version", "schedule_id", "calendar", "sessions")
OUTCOME_FIELDS = ("contract_id", "entry_reference_ask", "shares_per_contract", "observed_quote", "outcome_status",
                  "liquidation_reference_bid", "premium_change", "premium_return", "return_status",
                  "dollar_change_per_contract", "dollar_status")


class _Plain:
    def to_dict(self):
        return to_plain(self)


def _sealed_body(obj, id_field):
    data = obj.to_dict()
    data.pop(id_field)
    return data


@dataclass(frozen=True)
class Session(_Plain):
    session_date: str
    regular_open: str                 # canonical UTC ISO 8601
    regular_close: str


@dataclass(frozen=True)
class SessionSchedule(_Plain):
    schedule_format_version: str
    schedule_id: str
    calendar: str                     # XNYS
    sessions: tuple                   # Session, strictly increasing

    def body(self):
        return _sealed_body(self, "schedule_id")


@dataclass(frozen=True)
class Protocol(_Plain):
    protocol_format_version: str
    protocol_id: str
    purpose: str                      # test | production (production needs activation)
    prospective_start: str            # None for test protocols; canonical UTC for production
    evaluation_format_version: str
    rules_version: str
    schedule_format_version: str
    horizons: tuple
    horizon_rule: str
    window_seconds: int
    window_rule: str
    mark: str
    entry: str
    quote_requirements: tuple
    return_places: int
    return_rounding: str
    outcome_statuses: tuple
    return_statuses: tuple
    dollar_statuses: tuple
    relation_statuses: tuple
    candidate_inclusion: str
    invalidation_rule: str
    missing_contract_rule: str
    multiplier_rule: str
    exclusions: tuple                 # sorted

    def body(self):
        return _sealed_body(self, "protocol_id")


@dataclass(frozen=True)
class ProtocolRef(_Plain):
    protocol_format_version: str
    protocol_id: str
    purpose: str


@dataclass(frozen=True)
class SetupRef(_Plain):
    assessment_id: str
    assessment_format_version: str
    assessment_rules_version: str
    policy_id: str
    symbol: str
    side: str
    assessment_as_of: str             # the temporal anchor
    candidate_count: int


@dataclass(frozen=True)
class ObservationRef(_Plain):
    snapshot_id: str
    snapshot_format_version: str
    underlying: str
    as_of: str
    session_date: str
    truncated: bool


@dataclass(frozen=True)
class ScheduleRef(_Plain):
    schedule_id: str
    schedule_format_version: str


@dataclass(frozen=True)
class Horizon(_Plain):
    name: str                         # session_1 | session_5
    target_session_date: str
    target_open: str
    target_close: str
    window_start: str                 # target_close - 30 minutes


@dataclass(frozen=True)
class ObservedQuote(_Plain):
    status: str
    bid: str
    ask: str
    observed_at: str
    time_basis: str


@dataclass(frozen=True)
class CandidateOutcome(_Plain):
    contract_id: str
    entry_reference_ask: str          # Phase 10 entry reference (never a fill)
    shares_per_contract: str          # from the Phase 10 candidate; None if unavailable (never assumed)
    observed_quote: ObservedQuote     # copied from the snapshot; None when the contract is not in it
    outcome_status: str
    liquidation_reference_bid: str    # the later bid; None unless observed (never a fill)
    premium_change: str
    premium_return: str               # exactly 8 places, ROUND_HALF_EVEN; None unless computed
    return_status: str
    dollar_change_per_contract: str
    dollar_status: str


@dataclass(frozen=True)
class Count(_Plain):
    key: str
    count: int


@dataclass(frozen=True)
class SignCounts(_Plain):
    positive: int
    negative: int
    zero: int


@dataclass(frozen=True)
class Summary(_Plain):
    candidate_count: int
    outcome_status_counts: tuple      # Count, sorted by key, non-zero only
    premium_change_sign_counts: SignCounts   # observed candidates only


@dataclass(frozen=True)
class InvalidationRelation(_Plain):
    status: str                       # invalidated_by_target | no_invalidation_observed | not_evaluated
    check_ids: tuple                  # every supplied check, sorted
    invalidating_check_ids: tuple     # supplied checks that are invalidated with MI as_of <= target_close, sorted


@dataclass(frozen=True)
class TraceStep(_Plain):
    step: int
    rule: str
    result: str                       # always pass in a sealed object (failures are input errors)
    pointers: tuple


@dataclass(frozen=True)
class Provenance(_Plain):
    assessment_id: str
    snapshot_id: str
    schedule_id: str
    protocol_id: str
    invalidation_check_ids: tuple
    rules_version: str
    pointer_version: str


@dataclass(frozen=True)
class SetupEvaluation(_Plain):
    evaluation_format_version: str
    evaluation_id: str
    rules_version: str
    protocol_ref: ProtocolRef
    setup_ref: SetupRef
    observation_ref: ObservationRef
    schedule_ref: ScheduleRef
    horizon: Horizon
    candidate_outcomes: tuple         # every assessment candidate, canonical order
    summary: Summary
    invalidation_relation: InvalidationRelation
    decision_trace: tuple
    provenance: Provenance

    def body(self):
        return _sealed_body(self, "evaluation_id")
