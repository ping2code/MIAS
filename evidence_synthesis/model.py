"""Immutable EvidenceSynthesis models (``synthesis_format_version = "phase7g-v1"``). Descriptive facts only.

There are deliberately no top-level ``direction``, ``confidence``, ``score``, ``strength``, ``recommendation``,
``readiness``, ``setup``, ``signal`` or ``prediction`` fields. Per-timeframe ``technical_confidence`` is copied
from the source technical row and is never recomputed. Every value is JSON-native (str, int, bool, None) or a
tuple of models, so serialization is exact.
"""
from dataclasses import dataclass

from evidence_synthesis.canonical import to_plain


class _Plain:
    def to_dict(self):
        return to_plain(self)


@dataclass(frozen=True)
class Count(_Plain):
    key: str
    count: int


@dataclass(frozen=True)
class PacketRef(_Plain):
    packet_id: str
    packet_format_version: str
    symbol: str
    as_of: str


@dataclass(frozen=True)
class MissingTimeframe(_Plain):
    interval: str
    reason: str


@dataclass(frozen=True)
class Completeness(_Plain):
    market_context: str                # the packet's own availability status per domain
    technical: str
    news: str
    market_context_reasons: tuple = ()
    technical_reasons: tuple = ()
    news_reasons: tuple = ()
    technical_missing: tuple = ()      # MissingTimeframe records


@dataclass(frozen=True)
class TimeframeFacts(_Plain):
    interval: str
    available: bool
    state_direction: str               # derived: bullish | bearish | non_directional | unavailable
    state: str = None                  # source facts, copied verbatim from the technical row
    trend: str = None
    breakout_state: str = None
    momentum: str = None
    ema_alignment: str = None
    vwap_position: str = None
    technical_confidence: str = None
    bar_end: str = None
    age_seconds: int = None            # packet.as_of - bar_end (freshness; no threshold)
    missing_reason: str = None
    sources: tuple = ()


@dataclass(frozen=True)
class TimeframeRelation(_Plain):
    first: str
    second: str
    relation: str                      # agree | oppose | non_directional | unavailable
    sources: tuple = ()


@dataclass(frozen=True)
class TimeframeAlignment(_Plain):
    pattern: str
    bullish: int
    bearish: int
    non_directional: int
    unavailable: int


@dataclass(frozen=True)
class MarketReference(_Plain):
    reference: str                     # "self" (the symbol's own returns) or a benchmark symbol
    basis: str                         # prev_close | open
    value_sign: str                    # positive | negative | zero | unavailable (strict sign, no deadband)
    aligned: bool = None               # benchmark comparisons only (None for "self")
    unavailable_reasons: tuple = ()
    sources: tuple = ()


@dataclass(frozen=True)
class MarketRelation(_Plain):
    interval: str
    reference: str
    basis: str
    relation: str                      # agree | oppose | non_directional | unavailable


@dataclass(frozen=True)
class MarketContextFacts(_Plain):
    available: bool
    session_date: str = None
    calendar_state: str = None
    freshness_status: str = None       # preserved from the packet's MarketContext (never re-judged)
    context_as_of: str = None
    context_age_seconds: int = None    # packet.as_of - context.as_of
    references: tuple = ()
    relations: tuple = ()


@dataclass(frozen=True)
class NewsFacts(_Plain):
    availability: str                  # available | available_empty | partial | unavailable (packet status)
    item_count: int = 0
    counts_by_family: tuple = ()
    direct_relevance_count: int = 0
    related_relevance_count: int = 0
    upstream_alert_decision_counts: tuple = ()   # delivery metadata recorded upstream (not a market fact)
    upstream_impact_level_counts: tuple = ()     # upstream heuristic recorded by the collector
    newest_publication_timestamp: str = None
    oldest_publication_timestamp: str = None
    newest_publication_age_seconds: int = None
    newest_publication_date: str = None          # date-only publications (e.g. SEC filing dates)
    oldest_publication_date: str = None
    newest_publication_date_age_days: int = None
    exclusion_counts: tuple = ()
    sources: tuple = ()


@dataclass(frozen=True)
class Contradiction(_Plain):
    code: str
    subjects: tuple
    pointers: tuple = ()


@dataclass(frozen=True)
class Provenance(_Plain):
    packet_id: str
    packet_format_version: str
    synthesis_format_version: str
    rules_version: str
    source_domains: tuple = ("market_context", "technical", "news")


@dataclass(frozen=True)
class EvidenceSynthesis(_Plain):
    synthesis_format_version: str
    synthesis_id: str
    rules_version: str
    packet_ref: PacketRef
    completeness: Completeness
    timeframes: tuple
    timeframe_relations: tuple
    timeframe_alignment: TimeframeAlignment
    market_context: MarketContextFacts
    news: NewsFacts
    contradictions: tuple
    provenance: Provenance

    def body(self):
        """The hashed body: everything except synthesis_id."""
        data = self.to_dict()
        data.pop("synthesis_id")
        return data
