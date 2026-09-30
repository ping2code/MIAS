"""Immutable OptionsIntelligence model (``options_intelligence_format_version = "phase9-v1"``). Descriptive only.

There are deliberately no score, confidence, severity, priority, ranking, recommendation, selection, signal,
probability or expected-return fields. Numbers are canonical Decimal strings; counts are integers; keyed counts
are tuples of ``Count`` (never dicts), so every collection has a deterministic order.
"""
from dataclasses import dataclass

from options_intelligence.canonical import to_plain

TOP_LEVEL_FIELDS = ("options_intelligence_format_version", "options_intelligence_id", "rules_version",
                    "snapshot_ref", "market_intelligence_ref", "underlying", "chain_completeness", "expirations",
                    "contracts", "activity", "volatility", "attention", "provenance")


class _Plain:
    def to_dict(self):
        return to_plain(self)


@dataclass(frozen=True)
class Count(_Plain):
    key: str
    count: int


@dataclass(frozen=True)
class SnapshotRef(_Plain):
    snapshot_id: str
    snapshot_format_version: str
    underlying: str
    as_of: str


@dataclass(frozen=True)
class MarketIntelligenceRef(_Plain):
    intelligence_id: str
    synthesis_id: str
    symbol: str
    as_of: str
    as_of_gap_seconds: int            # snapshot as_of - MarketIntelligence as_of (>= 0)


@dataclass(frozen=True)
class Underlying(_Plain):
    symbol: str
    price_status: str                 # present | unavailable (copied from the snapshot)
    price: str
    price_observed_at: str
    price_age_seconds: int
    price_source: str
    price_reason: str


@dataclass(frozen=True)
class ImpliedVolatility(_Plain):
    status: str
    value: str
    time_basis: str


@dataclass(frozen=True)
class Greeks(_Plain):
    status: str
    delta: str
    gamma: str
    theta: str
    vega: str
    rho: str
    time_basis: str
    available_fields: tuple
    missing_fields: tuple
    out_of_bounds_fields: tuple       # mathematical sanity predicates only; descriptive, never a rejection


@dataclass(frozen=True)
class Day(_Plain):
    status: str
    observed_at: str
    age_seconds: int
    session_relation: str             # current_session | previous_session | older_session | unavailable


@dataclass(frozen=True)
class Contract(_Plain):
    contract_id: str
    provider_symbol: str
    option_type: str
    expiration: str
    strike: str
    dte_calendar_days: int
    expires_on_as_of_date: bool
    strike_relation: str
    strike_distance: str
    strike_distance_relative: str
    quote_state: str
    mid: str
    spread_absolute: str
    spread_relative: str
    spread_relative_reason: str
    volume_state: str
    open_interest_state: str
    volume_exceeds_open_interest: bool
    implied_volatility: ImpliedVolatility
    greeks: Greeks
    day: Day
    source_pointers: tuple


@dataclass(frozen=True)
class Activity(_Plain):
    volume_basis: str                 # volume totals count current-session day records only
    call_volume_total: str
    put_volume_total: str
    call_volume_records: int
    put_volume_records: int
    volume_records_not_current_session: int
    call_open_interest_total: str
    put_open_interest_total: str
    call_open_interest_records: int
    put_open_interest_records: int
    put_call_volume_ratio: str
    put_call_volume_ratio_reason: str
    put_call_open_interest_ratio: str
    put_call_open_interest_ratio_reason: str


@dataclass(frozen=True)
class Expiration(_Plain):
    expiration: str
    dte_calendar_days: int
    contract_count: int
    call_count: int
    put_count: int
    distinct_strike_count: int
    strike_min: str
    strike_max: str
    paired_strike_count: int
    quote_state_counts: tuple
    iv_available_count: int
    greeks_available_count: int
    volume_present_count: int
    open_interest_present_count: int
    activity: Activity


@dataclass(frozen=True)
class IvSummary(_Plain):
    expiration: str                   # None for the overall summary
    option_type: str                  # None for the overall summary
    available_count: int
    missing_count: int
    min: str
    max: str
    median: str


@dataclass(frozen=True)
class Volatility(_Plain):
    overall: IvSummary
    by_expiration_and_type: tuple


@dataclass(frozen=True)
class Completeness(_Plain):
    contract_count: int
    expiration_count: int
    records_received: int
    truncated: bool
    exclusions: tuple                 # Count per snapshot exclusion reason
    status_counts: tuple              # GroupCounts per fact group
    contracts_with: tuple             # Count: contracts whose group is present
    day_session_relation_counts: tuple


@dataclass(frozen=True)
class GroupCounts(_Plain):
    group: str
    counts: tuple


@dataclass(frozen=True)
class Attention(_Plain):
    category: str                     # data_quality | gap | presence (unordered; never importance)
    code: str
    scope: str                        # chain | contracts
    count: int
    contract_ids: tuple


@dataclass(frozen=True)
class Provenance(_Plain):
    snapshot_id: str
    market_intelligence_id: str
    rules_version: str
    pointer_version: str


@dataclass(frozen=True)
class OptionsIntelligence(_Plain):
    options_intelligence_format_version: str
    options_intelligence_id: str
    rules_version: str
    snapshot_ref: SnapshotRef
    market_intelligence_ref: MarketIntelligenceRef
    underlying: Underlying
    chain_completeness: Completeness
    expirations: tuple
    contracts: tuple
    activity: Activity
    volatility: Volatility
    attention: tuple
    provenance: Provenance

    def body(self):
        data = self.to_dict()
        data.pop("options_intelligence_id")
        return data
