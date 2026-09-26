"""Immutable, typed evidence-packet models (``format_version = "phase7c-v1"``). Facts are transported, never concluded.

There are deliberately **no packet-level** score, confidence, direction,
sentiment or recommendation fields. Domain fields keep their original meaning
and their own place:

- the technical ``confidence`` stays inside its durable technical row;
- the upstream news ``impact_score`` stays inside ``NewsUpstreamScore``;
- ``alert_decision`` stays inside ``NewsDelivery``, as delivery policy, not a fact.
"""
from dataclasses import dataclass, fields

from evidence_packet.serialization import plain

FORMAT_VERSION = "phase7c-v1"
NEWS_ADAPTER_VERSION = "phase7c-news-adapter-v1"  # This Phase 7C adapter's own label (not an upstream version).
TECHNICAL_INTERVALS = ("1d", "1h", "5m")
SUPPORTED_FAMILIES = ("news", "sec")

# Availability statuses.
AVAILABLE, AVAILABLE_EMPTY, PARTIAL, UNAVAILABLE = "available", "available_empty", "partial", "unavailable"

# Reason codes (sets are serialized sorted).
NOT_SUPPLIED = "not_supplied"
SOURCE_ERROR = "source_error"
AFTER_AS_OF = "after_as_of"
OBSERVED_AFTER_AS_OF = "observed_after_as_of"
SYMBOL_MISMATCH = "symbol_mismatch"
NO_MATCHING_ITEMS = "no_matching_items"
UNKNOWN_PUBLICATION_TIME = "unknown_publication_time"
UNSUPPORTED_FAMILY = "unsupported_family"
NEAR_DUPLICATE_SUPPRESSED = "near_duplicate_suppressed"
INVALID_EVENT = "invalid_event"
DUPLICATE_IDENTICAL = "duplicate_identical"
IDENTITY_CONFLICT = "identity_conflict"
INVALID_ROW = "invalid_row"
CONTENT_HASH_MISMATCH = "content_hash_mismatch"


class _Plain:
    def to_dict(self):
        return {f.name: plain(getattr(self, f.name)) for f in fields(self)}


@dataclass(frozen=True)
class Availability(_Plain):
    status: str
    reasons: tuple = ()


@dataclass(frozen=True)
class MarketContextSection(_Plain):
    availability: Availability
    context: object = None            # market_context.models.MarketContext (typed; serialized by its own to_dict)


@dataclass(frozen=True)
class TechnicalTimeframeEvidence(_Plain):
    interval: str
    bar_end: object = None            # ExchangeCalendar.bar_end of the row's bar (for the as-of cutoff)
    row: object = None                # Frozen private copy of the durable snapshot_row (ROW_FIELDS only)
    missing_reason: str = None


@dataclass(frozen=True)
class TechnicalSection(_Plain):
    availability: Availability
    timeframes: tuple = ()            # Exactly TECHNICAL_INTERVALS order: 1d, 1h, 5m


@dataclass(frozen=True)
class NewsFacts(_Plain):
    family: str
    headline: str
    summary: str = None               # Normalized, bounded source summary (never AI)
    source: str = None
    publisher: str = None
    canonical_url: str = None
    published_at: object = None       # Aware datetime (second/minute precision) or None
    publication_date: object = None   # Date-only publication (SEC filing date) or None
    timestamp_precision: str = None
    publication_basis: str = None
    sec_form: str = None
    accession_number: str = None


@dataclass(frozen=True)
class NewsRelevance(_Plain):
    symbols: tuple = ()
    direct_symbols: tuple = ()
    related_symbols: tuple = ()
    relevant: bool = None


@dataclass(frozen=True)
class NewsUpstreamScore(_Plain):
    """Recorded upstream heuristics, transported verbatim. They may be time-of-scoring dependent or AI-adjusted."""
    impact_score: object = None
    impact_level: str = None
    score_reasons: tuple = ()
    original_impact_score: object = None
    quality_adjustment: object = None


@dataclass(frozen=True)
class NewsDelivery(_Plain):
    """Delivery policy metadata, not a market fact. Nothing in the packet branches on it."""
    alert_decision: str = None
    collector_outcome: str = None


@dataclass(frozen=True)
class NewsEvidenceItem(_Plain):
    identity_version: str
    event_key: str
    facts: NewsFacts
    relevance: NewsRelevance
    upstream_score: NewsUpstreamScore
    delivery: NewsDelivery
    observed_at: object


@dataclass(frozen=True)
class Exclusion(_Plain):
    reason: str
    count: int


@dataclass(frozen=True)
class NewsSection(_Plain):
    availability: Availability
    items: tuple = ()
    excluded: tuple = ()              # Exclusion counts sorted by reason; nothing is silently discarded


@dataclass(frozen=True)
class NewsInput:
    """One already-produced upstream event, as supplied by the caller (never collected here)."""
    family: str                       # news | sec | fed | macro | treasury | geopolitical
    event: object                     # The post-decision collector event mapping
    observed_at: object               # When MIAS observed it (aware datetime)
    collector_outcome: str = "processed"


@dataclass(frozen=True)
class NewsCollection:
    """``succeeded`` must be stated explicitly: an empty list alone never means a successful collection."""
    succeeded: bool
    inputs: tuple = ()


@dataclass(frozen=True)
class PacketProvenance(_Plain):
    assembler: str = "evidence_packet"
    news_adapter_version: str = NEWS_ADAPTER_VERSION
    technical_intervals: tuple = TECHNICAL_INTERVALS
    supported_news_families: tuple = SUPPORTED_FAMILIES


@dataclass(frozen=True)
class EvidencePacket(_Plain):
    format_version: str
    packet_id: str
    symbol: str
    as_of: object
    market_context: MarketContextSection
    technical: TechnicalSection
    news: NewsSection
    provenance: PacketProvenance

    def body(self):
        """The hashed body: everything except packet_id."""
        data = self.to_dict()
        data.pop("packet_id")
        return data
