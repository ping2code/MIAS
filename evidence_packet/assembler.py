"""Pure evidence-packet assembler (Phase 7C v1): already-produced evidence in, one deterministic packet out.

It performs no I/O at all: no vendor, news, database, Redis or AI calls, and no
clock reads. ``as_of`` is the caller's **information cutoff** and must be
timezone-aware.

**Market context** (typed ``MarketContext``, never recomputed or mutated):

- not supplied: ``unavailable/not_supplied``;
- another symbol: ``symbol_mismatch``;
- ``context.as_of > as_of``: ``after_as_of``.

**Technical** (``{interval: durable snapshot_row}``; intervals 1d, 1h, 5m, in that
order):

- each row is adapted, validated and hash-checked;
- ``bar_end > as_of`` makes that timeframe missing with ``after_as_of``;
- all three present: ``available``; some: ``partial``; none: ``unavailable``;
- there is no combined verdict.

**News** (``NewsCollection``; RSS and SEC only in v1). Each input is checked in
this order, and the first failing check is its single counted exclusion reason:

1. ``unsupported_family``
2. ``near_duplicate_suppressed``
3. ``invalid_event``
4. ``symbol_mismatch`` (packet symbol not in ``event.symbols``; covers market-wide
   events with no symbols)
5. ``unknown_publication_time``
6. ``after_as_of``: ``published_at > as_of``, or a date-only publication whose
   date is not strictly before ``as_of``'s America/New_York date
7. ``observed_after_as_of``

**Dedupe** by ``(identity_version, event_key)``. No fuzzy matching; upstream dedupe
is trusted.

- Same content, ignoring ``observed_at``: one record is kept, with the earliest
  ``observed_at``; the others count as ``duplicate_identical``.
- Different content: **all** records of that identity are excluded and counted
  as ``identity_conflict``. The assembler never picks one.

**Availability:**

- no collection supplied: ``unavailable/not_supplied``;
- ``succeeded=False``: ``unavailable/source_error``;
- succeeded with zero kept items: ``available_empty/no_matching_items``.

**Order:**

- known publication before unknown;
- newest exchange (America/New_York) date first;
- within a date, timestamped items (latest first), then date-only items;
- then family, identity_version, event_key ascending.
"""
from collections import Counter
from datetime import datetime

from evidence_packet import models as m
from evidence_packet.adapters import AdapterError, adapt_news_event, adapt_technical_row
from evidence_packet.serialization import canonical_json, content_id, plain
from market_context.models import MarketContext
from market_data.models import EXCHANGE_TZ, SYMBOL


def _market_section(symbol, as_of, context):
    if context is None:
        return m.MarketContextSection(m.Availability(m.UNAVAILABLE, (m.NOT_SUPPLIED,)))
    if not isinstance(context, MarketContext):
        raise TypeError("market_context must be a market_context.models.MarketContext")
    if context.symbol != symbol:
        return m.MarketContextSection(m.Availability(m.UNAVAILABLE, (m.SYMBOL_MISMATCH,)))
    if context.as_of > as_of:
        return m.MarketContextSection(m.Availability(m.UNAVAILABLE, (m.AFTER_AS_OF,)))
    return m.MarketContextSection(m.Availability(m.AVAILABLE), context=context)


def _technical_section(symbol, as_of, rows, calendar):
    if rows is None:
        return m.TechnicalSection(m.Availability(m.UNAVAILABLE, (m.NOT_SUPPLIED,)),
                                  tuple(m.TechnicalTimeframeEvidence(interval=i, missing_reason=m.NOT_SUPPLIED)
                                        for i in m.TECHNICAL_INTERVALS))
    timeframes, reasons = [], []
    for interval in m.TECHNICAL_INTERVALS:
        row = rows.get(interval)
        reason = None
        if row is None:
            reason = m.NOT_SUPPLIED
        else:
            try:
                evidence = adapt_technical_row(row, symbol=symbol, interval=interval, calendar=calendar)
                if evidence.bar_end > as_of:
                    reason = m.AFTER_AS_OF
            except AdapterError as error:
                reason = error.reason
        if reason is None:
            timeframes.append(evidence)
        else:
            timeframes.append(m.TechnicalTimeframeEvidence(interval=interval, missing_reason=reason))
            reasons.append(f"{interval}:{reason}")
    present = len(m.TECHNICAL_INTERVALS) - len(reasons)
    status = m.AVAILABLE if not reasons else m.PARTIAL if present else m.UNAVAILABLE
    return m.TechnicalSection(m.Availability(status, tuple(sorted(reasons))), tuple(timeframes))


def _time_reason(item, as_of):
    facts = item.facts
    if facts.timestamp_precision in ("second", "minute") and facts.published_at is not None:
        if facts.published_at > as_of:
            return m.AFTER_AS_OF
    elif facts.timestamp_precision == "date" and facts.publication_date is not None:
        if not facts.publication_date < as_of.astimezone(EXCHANGE_TZ).date():
            return m.AFTER_AS_OF
    else:
        return m.UNKNOWN_PUBLICATION_TIME
    if item.observed_at > as_of:
        return m.OBSERVED_AFTER_AS_OF
    return None


def _content_key(item):
    data = item.to_dict()
    data.pop("observed_at")
    return canonical_json(data)


def _order_key(item):
    """Known before unknown; newest America/New_York publication date first; within a date, timestamped items first
    (latest first), then date-only items (no time of day is ever assumed); then family, identity_version, event_key."""
    facts = item.facts
    if facts.published_at is not None:
        local = facts.published_at.astimezone(EXCHANGE_TZ)
        seconds = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
        known = (0, -local.date().toordinal(), 0, -seconds)
    elif facts.publication_date is not None:
        known = (0, -facts.publication_date.toordinal(), 1, 0)
    else:
        known = (1, 0, 0, 0)
    return (*known, facts.family, item.identity_version, item.event_key)


def _news_section(symbol, as_of, collection):
    if collection is None:
        return m.NewsSection(m.Availability(m.UNAVAILABLE, (m.NOT_SUPPLIED,)))
    if not isinstance(collection, m.NewsCollection) or not isinstance(collection.succeeded, bool):
        raise TypeError("news must be a NewsCollection with an explicit succeeded flag")
    if not collection.succeeded:
        return m.NewsSection(m.Availability(m.UNAVAILABLE, (m.SOURCE_ERROR,)))
    excluded, candidates = Counter(), {}
    for news_input in collection.inputs:
        if not isinstance(news_input, m.NewsInput):
            raise TypeError("news inputs must be NewsInput")
        try:
            item = adapt_news_event(news_input)
        except AdapterError as error:
            excluded[error.reason] += 1
            continue
        if symbol not in item.relevance.symbols:
            excluded[m.SYMBOL_MISMATCH] += 1
            continue
        reason = _time_reason(item, as_of)
        if reason:
            excluded[reason] += 1
            continue
        candidates.setdefault((item.identity_version, item.event_key), []).append(item)
    items = []
    for records in candidates.values():
        if len({_content_key(r) for r in records}) > 1:
            excluded[m.IDENTITY_CONFLICT] += len(records)
            continue
        items.append(min(records, key=lambda r: r.observed_at))
        if len(records) > 1:
            excluded[m.DUPLICATE_IDENTICAL] += len(records) - 1
    items.sort(key=_order_key)
    exclusions = tuple(m.Exclusion(reason, count) for reason, count in sorted(excluded.items()))
    availability = m.Availability(m.AVAILABLE) if items else m.Availability(m.AVAILABLE_EMPTY, (m.NO_MATCHING_ITEMS,))
    return m.NewsSection(availability, tuple(items), exclusions)


def assemble(symbol, as_of, *, calendar, market_context=None, technical_rows=None, news=None):
    """One EvidencePacket for ``symbol`` at the information cutoff ``as_of``. It concludes nothing."""
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("symbol must be an upper-case ticker")
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("as_of must be a timezone-aware datetime")
    sections = dict(market_context=_market_section(symbol, as_of, market_context),
                    technical=_technical_section(symbol, as_of, technical_rows, calendar),
                    news=_news_section(symbol, as_of, news))
    provenance = m.PacketProvenance()
    body = dict(format_version=m.FORMAT_VERSION, symbol=symbol, as_of=plain(as_of),
                provenance=provenance.to_dict(), **{k: v.to_dict() for k, v in sections.items()})
    return m.EvidencePacket(format_version=m.FORMAT_VERSION, packet_id=content_id(body), symbol=symbol, as_of=as_of,
                            provenance=provenance, **sections)
