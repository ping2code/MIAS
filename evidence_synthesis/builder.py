"""Pure builder: one verified EvidencePacket → one EvidenceSynthesis. No I/O, no clock, no AI, no mutation.

``synthesize(packet)`` validates the packet (typed or canonical dict; the hash is re-verified), then derives only
descriptive facts:

- per timeframe (1d, 1h, 5m): the source technical facts, copied verbatim; the frozen ``state_direction``; the age
  relative to ``packet.as_of``;
- pairwise relations (1d,1h), (1h,5m), (1d,5m), plus the compact pattern (fixed precedence; no weighting, no
  winner);
- market context: strict signs of the symbol's own returns and of every benchmark relative return, for both bases
  and every benchmark in the packet (none preferred). Each timeframe × reference × basis gets a relation. Market
  context never changes technical facts;
- news/SEC: non-directional counts, publication extremes and ages, exclusion counts. No sentiment, and no
  technical-vs-news opposition;
- contradictions: actual cross-evidence opposition and unavailable, misaligned or inconsistent evidence. A
  ``mixed`` state alone is **not** a contradiction.

``synthesis_id = "sha256:" + SHA-256(canonical body without synthesis_id)``. The body includes
``synthesis_format_version``, ``rules_version`` and ``packet_ref``. There is no ``generated_at``.
"""
from collections import Counter
from dataclasses import replace

from evidence_synthesis import model as m
from evidence_synthesis import rules as r
from evidence_synthesis.canonical import content_id
from evidence_synthesis.validation import parse_day, parse_instant, validated_packet


def _seconds(later, earlier):
    return int((later - earlier).total_seconds())


def _counts(values):
    return tuple(m.Count(key, count) for key, count in sorted(Counter(values).items()))


def _timeframes(packet, as_of):
    facts = []
    for frame in packet["technical"]["timeframes"]:
        interval, row = frame["interval"], frame["row"]
        base = f"technical.{interval}"
        if row is None:
            facts.append(m.TimeframeFacts(interval=interval, available=False, state_direction=r.UNAVAILABLE,
                                          missing_reason=frame["missing_reason"],
                                          sources=(f"{base}.missing_reason",)))
            continue
        bar_end = parse_instant(frame["bar_end"], f"{base}.bar_end")
        facts.append(m.TimeframeFacts(
            interval=interval, available=True, state_direction=r.state_direction(row["technical_state"]),
            state=row["technical_state"], trend=row["trend"], breakout_state=row["breakout_state"],
            momentum=row["momentum"], ema_alignment=row["ema_alignment"], vwap_position=row["vwap_position"],
            technical_confidence=row["confidence"], bar_end=frame["bar_end"], age_seconds=_seconds(as_of, bar_end),
            sources=tuple(f"{base}.{key}" for key in (
                "bar_end", "row.technical_state", "row.trend", "row.breakout_state", "row.momentum",
                "row.ema_alignment", "row.vwap_position", "row.confidence"))))
    return tuple(facts)


def _relations(frames):
    by_interval = {f.interval: f for f in frames}
    relations = []
    for first, second in r.PAIRS:
        a, b = by_interval[first], by_interval[second]
        relations.append(m.TimeframeRelation(first, second, r.pair_relation(a.state_direction, b.state_direction),
                                             sources=(f"technical.{first}.row.technical_state",
                                                      f"technical.{second}.row.technical_state")))
    return tuple(relations)


def _alignment(frames):
    directions = [f.state_direction for f in frames]
    return m.TimeframeAlignment(pattern=r.timeframe_pattern(directions), bullish=directions.count(r.BULLISH),
                                bearish=directions.count(r.BEARISH), non_directional=directions.count(r.NON_DIRECTIONAL),
                                unavailable=directions.count(r.UNAVAILABLE))


def _market_context(packet, as_of, frames):
    section = packet["market_context"]
    context = section["context"]
    if context is None:
        return m.MarketContextFacts(available=False)
    series = context["symbol_context"]
    references = []
    for basis, key in (("prev_close", "return_since_prev_close"), ("open", "return_since_open")):
        references.append(m.MarketReference(
            reference=r.SELF_REFERENCE, basis=basis, value_sign=r.sign(series[key]),
            sources=(f"market_context.context.symbol_context.{key}",)))
    for comparison in context["comparisons"]:
        benchmark, basis = comparison["benchmark"], comparison["basis"]
        references.append(m.MarketReference(
            reference=benchmark, basis=basis, value_sign=r.sign(comparison["relative_return"]),
            aligned=comparison["aligned"], unavailable_reasons=tuple(sorted(comparison["unavailable"])),
            sources=(f"market_context.context.comparisons[{benchmark}:{basis}].relative_return",)))
    references.sort(key=lambda ref: (ref.reference != r.SELF_REFERENCE, ref.reference, r.BASES.index(ref.basis)))
    relations = tuple(m.MarketRelation(frame.interval, ref.reference, ref.basis,
                                       r.context_relation(frame.state_direction, ref.value_sign))
                      for frame in frames for ref in references)
    freshness = series.get("freshness") or {}
    context_as_of = parse_instant(context["as_of"], "market_context.as_of")
    return m.MarketContextFacts(
        available=True, session_date=context.get("session_date"), calendar_state=context.get("calendar_state"),
        freshness_status=freshness.get("status"), context_as_of=context["as_of"],
        context_age_seconds=_seconds(as_of, context_as_of), references=tuple(references), relations=relations)


def _news(packet, as_of):
    section, symbol = packet["news"], packet["symbol"]
    items = section["items"]
    stamps = sorted((parse_instant(i["facts"]["published_at"], "published_at"), i["facts"]["published_at"])
                    for i in items if i["facts"].get("published_at") is not None)
    days = sorted(i["facts"]["publication_date"] for i in items if i["facts"].get("publication_date") is not None)
    as_of_day = as_of.astimezone(r.EXCHANGE_TZ).date()
    return m.NewsFacts(
        availability=section["availability"]["status"], item_count=len(items),
        counts_by_family=_counts(i["facts"]["family"] for i in items),
        direct_relevance_count=sum(symbol in i["relevance"]["direct_symbols"] for i in items),
        related_relevance_count=sum(symbol in i["relevance"]["related_symbols"] and
                                    symbol not in i["relevance"]["direct_symbols"] for i in items),
        upstream_alert_decision_counts=_counts(i["delivery"].get("alert_decision") or "none" for i in items),
        upstream_impact_level_counts=_counts(i["upstream_score"].get("impact_level") or "none" for i in items),
        newest_publication_timestamp=stamps[-1][1] if stamps else None,
        oldest_publication_timestamp=stamps[0][1] if stamps else None,
        newest_publication_age_seconds=_seconds(as_of, stamps[-1][0]) if stamps else None,
        newest_publication_date=days[-1] if days else None, oldest_publication_date=days[0] if days else None,
        newest_publication_date_age_days=(as_of_day - parse_day(days[-1], "publication_date")).days if days else None,
        exclusion_counts=tuple(m.Count(e["reason"], e["count"]) for e in sorted(section["excluded"],
                                                                               key=lambda e: e["reason"])),
        sources=tuple(sorted(f"news.items[{i['identity_version']}:{i['event_key']}]" for i in items)))


def _contradictions(packet, frames, relations, market):
    found = []
    for frame in frames:
        if frame.state_direction == r.UNAVAILABLE:
            found.append(m.Contradiction(r.TIMEFRAME_UNAVAILABLE, (frame.interval,), frame.sources[:2]))
    for relation in relations:
        if relation.relation == r.OPPOSE:
            found.append(m.Contradiction(r.TIMEFRAME_OPPOSITION, (relation.first, relation.second), relation.sources))
    if not market.available:
        found.append(m.Contradiction(r.MARKET_CONTEXT_UNAVAILABLE, ("market_context",),
                                     ("market_context.availability",)))
    else:
        refs = {(ref.reference, ref.basis): ref for ref in market.references}
        for relation in market.relations:
            if relation.relation == r.OPPOSE:
                ref = refs[(relation.reference, relation.basis)]
                found.append(m.Contradiction(r.MARKET_CONTEXT_OPPOSES_TIMEFRAME,
                                             (relation.interval, relation.reference, relation.basis),
                                             (f"technical.{relation.interval}.row.technical_state", *ref.sources)))
        for ref in market.references:
            if "misaligned" in ref.unavailable_reasons:
                found.append(m.Contradiction(r.COMPARISON_MISALIGNED, (ref.reference, ref.basis), ref.sources))
        latest = max((f for f in frames if f.bar_end is not None),
                     key=lambda f: (parse_instant(f.bar_end, "bar_end"), -r.INTERVALS.index(f.interval)), default=None)
        if market.session_date is not None and latest is not None:
            latest_day = parse_instant(latest.bar_end, "bar_end").astimezone(r.EXCHANGE_TZ).date().isoformat()
            if latest_day != market.session_date:
                found.append(m.Contradiction(r.CONTEXT_SESSION_MISMATCH, ("market_context", latest.interval),
                                             ("market_context.context.session_date",
                                              f"technical.{latest.interval}.bar_end")))
    if packet["news"]["availability"]["status"] == "unavailable":
        found.append(m.Contradiction(r.NEWS_UNAVAILABLE, ("news",), ("news.availability",)))
    return tuple(sorted(found, key=lambda c: (c.code, c.subjects)))


def synthesize(packet):
    """EvidenceSynthesis for one valid phase7c-v1 packet; raises PacketValidationError for invalid input."""
    data = validated_packet(packet)
    as_of = parse_instant(data["as_of"], "packet as_of")
    frames = _timeframes(data, as_of)
    relations = _relations(frames)
    market = _market_context(data, as_of, frames)
    tech, mc, news = (data[k]["availability"] for k in ("technical", "market_context", "news"))
    fields = dict(
        synthesis_format_version=r.SYNTHESIS_FORMAT_VERSION, rules_version=r.RULES_VERSION,
        packet_ref=m.PacketRef(data["packet_id"], data["format_version"], data["symbol"], data["as_of"]),
        completeness=m.Completeness(
            market_context=mc["status"], technical=tech["status"], news=news["status"],
            market_context_reasons=tuple(sorted(mc["reasons"])), technical_reasons=tuple(sorted(tech["reasons"])),
            news_reasons=tuple(sorted(news["reasons"])),
            technical_missing=tuple(m.MissingTimeframe(f.interval, f.missing_reason) for f in frames
                                    if not f.available)),
        timeframes=frames, timeframe_relations=relations, timeframe_alignment=_alignment(frames),
        market_context=market, news=_news(data, as_of),
        contradictions=_contradictions(data, frames, relations, market),
        provenance=m.Provenance(data["packet_id"], data["format_version"], r.SYNTHESIS_FORMAT_VERSION,
                                r.RULES_VERSION))
    draft = m.EvidenceSynthesis(synthesis_id="", **fields)
    return replace(draft, synthesis_id=content_id(draft.body()))
