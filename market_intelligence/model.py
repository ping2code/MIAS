"""Immutable MarketIntelligence models (``intelligence_format_version = "phase8-v1"``). Descriptive facts only.

There are deliberately no score, confidence, severity, priority, ranking, forecast or decision fields. Every value
is JSON-native (str, int, bool, None) or a tuple of models, so serialization is exact.

Pointers:

- ``synthesis_pointers``: key-based paths into the EvidenceSynthesis, e.g. ``timeframes[1d].state_direction``,
  ``timeframe_relations[1d:5m].relation``, ``market_context.references[QQQ:open].value_sign``;
- ``packet_pointers``: the synthesis's own EvidencePacket pointers, copied through unchanged.
"""
from dataclasses import dataclass

from market_intelligence.canonical import to_plain


class _Plain:
    def to_dict(self):
        return to_plain(self)


@dataclass(frozen=True)
class Count(_Plain):
    key: str
    count: int


@dataclass(frozen=True)
class SynthesisRef(_Plain):
    synthesis_id: str
    synthesis_format_version: str
    synthesis_rules_version: str
    packet_id: str
    symbol: str
    as_of: str


@dataclass(frozen=True)
class UnavailableInterval(_Plain):
    interval: str
    reason: str


@dataclass(frozen=True)
class ReferenceKey(_Plain):
    reference: str
    basis: str


@dataclass(frozen=True)
class UnavailableReference(_Plain):
    reference: str
    basis: str
    reasons: tuple


@dataclass(frozen=True)
class StatusReasons(_Plain):
    market_context: tuple
    technical: tuple
    news: tuple


@dataclass(frozen=True)
class EvidenceCoverage(_Plain):
    technical_status: str                    # the synthesis completeness status, copied
    available_intervals: tuple               # a row with a directional or non-directional state
    unavailable_intervals: tuple             # UnavailableInterval: no row (the packet's missing reason)
    insufficient_data_intervals: tuple       # a row whose state is insufficient_data
    market_context_status: str
    available_references: tuple             # ReferenceKey
    unavailable_references: tuple            # UnavailableReference
    news_state: str                          # available | available_empty | partial | unavailable
    status_reasons: StatusReasons
    synthesis_pointers: tuple = ()
    packet_pointers: tuple = ()


@dataclass(frozen=True)
class TimeframeStructure(_Plain):
    pattern: str                             # timeframe_alignment.pattern, copied (no new directional summary)
    directional_intervals: tuple
    non_directional_intervals: tuple
    unavailable_intervals: tuple             # state_direction unavailable (no row, or insufficient_data)
    opposition_shape: str                    # none | isolated_interval | single_pair | not_applicable
    isolated_interval: str                   # set only for isolated_interval
    opposing_pairs: tuple                    # (first, second) in the synthesis pair order
    synthesis_pointers: tuple = ()
    packet_pointers: tuple = ()


@dataclass(frozen=True)
class ReferenceSign(_Plain):
    reference: str
    basis: str
    value_sign: str


@dataclass(frozen=True)
class IntervalContext(_Plain):
    interval: str
    agree_count: int
    oppose_count: int
    non_directional_count: int
    unavailable_count: int
    opposing_references: tuple               # ReferenceKey
    synthesis_pointers: tuple = ()


@dataclass(frozen=True)
class MarketContextAlignment(_Plain):
    available: bool
    freshness_status: str
    context_age_seconds: int
    own_return_signs: tuple                  # ReferenceSign for the symbol's own returns, per basis
    own_return_profile: str
    relative_return_signs: tuple             # ReferenceSign per benchmark and basis (relative returns)
    relative_return_profile: str
    by_interval: tuple                       # IntervalContext (empty when market context is unavailable)
    synthesis_pointers: tuple = ()
    packet_pointers: tuple = ()


@dataclass(frozen=True)
class Publication(_Plain):
    timestamp: str
    date: str


@dataclass(frozen=True)
class EventPresence(_Plain):
    news_state: str
    item_count: int
    counts_by_family: tuple                  # Count
    counts_by_relevance: tuple               # Count: direct, related_only
    upstream_alert_decision_counts: tuple    # upstream delivery metadata, never a Phase 8 judgment
    upstream_impact_level_counts: tuple      # upstream collector heuristic, never a Phase 8 judgment
    newest_publication: Publication
    oldest_publication: Publication
    newest_age_seconds: int
    oldest_age_seconds: int
    newest_date_age_days: int
    exclusion_counts: tuple                  # Count
    synthesis_pointers: tuple = ()
    packet_pointers: tuple = ()


@dataclass(frozen=True)
class Conflict(_Plain):
    code: str
    subjects: tuple
    synthesis_pointers: tuple
    packet_pointers: tuple


@dataclass(frozen=True)
class Attention(_Plain):
    category: str                            # conflict | gap | presence (unordered; not importance)
    code: str
    subjects: tuple
    synthesis_pointers: tuple
    packet_pointers: tuple


@dataclass(frozen=True)
class Comparison(_Plain):
    """Phase 8B: set only when a previous synthesis is supplied (None for current-only builds)."""
    status: str                              # comparable | not_comparable
    reasons: tuple                           # closed reason codes; empty when comparable
    previous_ref: SynthesisRef
    elapsed_seconds: int                     # current as_of - previous as_of (> 0; a fact, no threshold)


@dataclass(frozen=True)
class Transition(_Plain):
    """Phase 8B: a descriptive difference between the previous and the current synthesis."""
    code: str
    subjects: tuple
    previous_pointers: tuple                 # "previous:<synthesis pointer>" (empty if absent in previous)
    current_pointers: tuple                  # "current:<synthesis pointer>" (empty if absent in current)


@dataclass(frozen=True)
class Provenance(_Plain):
    synthesis_id: str
    synthesis_format_version: str
    synthesis_rules_version: str
    packet_id: str
    domains_used: tuple


@dataclass(frozen=True)
class MarketIntelligence(_Plain):
    intelligence_format_version: str
    intelligence_id: str
    rules_version: str
    synthesis_ref: SynthesisRef
    comparison: Comparison                   # None unless a previous synthesis is supplied (Phase 8B)
    evidence_coverage: EvidenceCoverage
    timeframe_structure: TimeframeStructure
    market_context_alignment: MarketContextAlignment
    event_presence: EventPresence
    conflicts: tuple
    transitions: tuple                       # Transition; () unless comparable with a previous synthesis
    attention: tuple
    provenance: Provenance

    def body(self):
        """The hashed body: everything except intelligence_id."""
        data = self.to_dict()
        data.pop("intelligence_id")
        return data
