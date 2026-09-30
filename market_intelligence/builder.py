"""Pure builder: one verified EvidenceSynthesis (plus an optional previous one) -> one MarketIntelligence.

``build(synthesis)`` validates the synthesis (typed or canonical dict; ``synthesis_id`` and every derived fact are
re-verified), then organizes it into descriptive blocks. Every value is copied from the synthesis or condensed by a
fixed rule; nothing is weighted, ranked, scored or predicted, and market context never changes technical facts.

- ``evidence_coverage``: what is available, unavailable, insufficient, or empty (lists and enums, no ratios);
- ``timeframe_structure``: the synthesis pattern plus the shape of any opposition;
- ``market_context_alignment``: own-return and relative-return sign profiles kept apart, and per-timeframe tallies;
- ``event_presence``: news/SEC presence, counts and recency; upstream metadata labelled as upstream;
- ``conflicts``: the synthesis opposition contradictions, unresolved and unranked;
- ``attention``: closed operational flags (conflict, gap, presence), sorted by (category, code, subjects).

Every block above is derived from the **current** synthesis only.

**Phase 8B:** ``build(current, previous=None)``. Without ``previous`` the output is exactly the Phase 8A output
(``comparison`` is ``None``, ``transitions`` is ``()``). With an explicit, caller-supplied ``previous`` (same
symbol, earlier ``as_of``, a different synthesis):

- ``comparison``: ``comparable``, or ``not_comparable`` with reason codes when the synthesis format or rules
  versions differ; the previous synthesis reference; ``elapsed_seconds`` (a fact, no threshold);
- ``transitions``: closed, descriptive differences (only when comparable), each with ``previous:`` and ``current:``
  pointers. They describe differences between two cutoff-safe syntheses and never predict what happens next.

The previous synthesis never changes any current-state block.

``intelligence_id = "sha256:" + SHA-256(canonical body without intelligence_id)``. There is no ``generated_at``;
no clock, environment, network, database, file or AI is used.
"""
from dataclasses import replace

from market_intelligence import model as m
from market_intelligence import rules as r
from market_intelligence.canonical import content_id
from market_intelligence.validation import instant, validated_pair, validated_synthesis


def _ref_key(reference, basis):
    return f"{reference}:{basis}"


def _counts(entries):
    return tuple(m.Count(e["key"], e["count"]) for e in entries)


def _uniq(values):
    return tuple(sorted(set(values)))


def _coverage(data, frames, references):
    completeness = data["completeness"]
    return m.EvidenceCoverage(
        technical_status=completeness["technical"],
        available_intervals=tuple(i for i in r.INTERVALS
                                  if frames[i]["available"] and frames[i]["state"] != r.INSUFFICIENT_DATA),
        unavailable_intervals=tuple(m.UnavailableInterval(i, frames[i]["missing_reason"]) for i in r.INTERVALS
                                    if not frames[i]["available"]),
        insufficient_data_intervals=tuple(i for i in r.INTERVALS if frames[i]["state"] == r.INSUFFICIENT_DATA),
        market_context_status=completeness["market_context"],
        available_references=tuple(m.ReferenceKey(ref["reference"], ref["basis"]) for ref in references
                                   if ref["value_sign"] != r.UNAVAILABLE),
        unavailable_references=tuple(m.UnavailableReference(ref["reference"], ref["basis"],
                                                            tuple(ref["unavailable_reasons"]))
                                     for ref in references if ref["value_sign"] == r.UNAVAILABLE),
        news_state=completeness["news"],
        status_reasons=m.StatusReasons(tuple(completeness["market_context_reasons"]),
                                       tuple(completeness["technical_reasons"]), tuple(completeness["news_reasons"])),
        synthesis_pointers=("completeness", *(f"timeframes[{i}].available" for i in r.INTERVALS),
                            *(f"timeframes[{i}].state" for i in r.INTERVALS),
                            *(f"market_context.references[{_ref_key(ref['reference'], ref['basis'])}].value_sign"
                              for ref in references)),
        packet_pointers=_uniq(p for i in r.INTERVALS for p in frames[i]["sources"]))


def _structure(data, frames, relations):
    pattern = data["timeframe_alignment"]["pattern"]
    directions = {i: frames[i]["state_direction"] for i in r.INTERVALS}
    shape, isolated = r.opposition_shape(pattern, directions, {pair: rel["relation"] for pair, rel in relations.items()})
    return m.TimeframeStructure(
        pattern=pattern,
        directional_intervals=tuple(i for i in r.INTERVALS if directions[i] in (r.BULLISH, r.BEARISH)),
        non_directional_intervals=tuple(i for i in r.INTERVALS if directions[i] == r.NON_DIRECTIONAL),
        unavailable_intervals=tuple(i for i in r.INTERVALS if directions[i] == r.UNAVAILABLE),
        opposition_shape=shape, isolated_interval=isolated,
        opposing_pairs=tuple(pair for pair in r.PAIRS if relations[pair]["relation"] == r.OPPOSE),
        synthesis_pointers=("timeframe_alignment.pattern",
                            *(f"timeframes[{i}].state_direction" for i in r.INTERVALS),
                            *(f"timeframe_relations[{a}:{b}].relation" for a, b in r.PAIRS)),
        # Each timeframe's own resolving pointer (state, or missing_reason). Relation sources are not used: for a
        # missing timeframe they name an absent row (Phase 7H §12).
        packet_pointers=_uniq(p for i in r.INTERVALS for p in frames[i]["sources"]
                              if p.endswith((".row.technical_state", ".missing_reason"))))


def _alignment(market, references):
    if not market["available"]:
        return m.MarketContextAlignment(
            available=False, freshness_status=None, context_age_seconds=None, own_return_signs=(),
            own_return_profile="unavailable", relative_return_signs=(), relative_return_profile="unavailable",
            by_interval=(), synthesis_pointers=("market_context.available",), packet_pointers=())
    own = tuple(m.ReferenceSign(ref["reference"], ref["basis"], ref["value_sign"]) for ref in references
                if ref["reference"] == r.SELF_REFERENCE)
    relative = tuple(m.ReferenceSign(ref["reference"], ref["basis"], ref["value_sign"]) for ref in references
                     if ref["reference"] != r.SELF_REFERENCE)
    by_interval = []
    for interval in r.INTERVALS:
        rows = [rel for rel in market["relations"] if rel["interval"] == interval]
        tally = {name: sum(rel["relation"] == name for rel in rows) for name in r.RELATIONS}
        by_interval.append(m.IntervalContext(
            interval=interval, agree_count=tally[r.AGREE], oppose_count=tally[r.OPPOSE],
            non_directional_count=tally[r.NON_DIRECTIONAL], unavailable_count=tally[r.UNAVAILABLE],
            opposing_references=tuple(m.ReferenceKey(rel["reference"], rel["basis"]) for rel in rows
                                      if rel["relation"] == r.OPPOSE),
            synthesis_pointers=tuple(f"market_context.relations[{interval}:{_ref_key(rel['reference'], rel['basis'])}]"
                                     f".relation" for rel in rows)))
    return m.MarketContextAlignment(
        available=True, freshness_status=market["freshness_status"],
        context_age_seconds=market["context_age_seconds"],
        own_return_signs=own, own_return_profile=r.sign_profile(s.value_sign for s in own),
        relative_return_signs=relative, relative_return_profile=r.sign_profile(s.value_sign for s in relative),
        by_interval=tuple(by_interval),
        synthesis_pointers=("market_context.freshness_status", "market_context.context_age_seconds",
                            *(f"market_context.references[{_ref_key(ref['reference'], ref['basis'])}].value_sign"
                              for ref in references)),
        packet_pointers=_uniq(p for ref in references for p in ref["sources"]))


def _events(news, as_of):
    oldest_at = news["oldest_publication_timestamp"]
    return m.EventPresence(
        news_state=news["availability"], item_count=news["item_count"],
        counts_by_family=_counts(news["counts_by_family"]),
        counts_by_relevance=(m.Count("direct", news["direct_relevance_count"]),
                             m.Count("related_only", news["related_relevance_count"])),
        upstream_alert_decision_counts=_counts(news["upstream_alert_decision_counts"]),
        upstream_impact_level_counts=_counts(news["upstream_impact_level_counts"]),
        newest_publication=m.Publication(news["newest_publication_timestamp"], news["newest_publication_date"]),
        oldest_publication=m.Publication(oldest_at, news["oldest_publication_date"]),
        newest_age_seconds=news["newest_publication_age_seconds"],
        oldest_age_seconds=None if oldest_at is None else int((as_of - instant(oldest_at, "oldest")).total_seconds()),
        newest_date_age_days=news["newest_publication_date_age_days"],
        exclusion_counts=_counts(news["exclusion_counts"]),
        synthesis_pointers=("news.availability", "news.item_count", "news.counts_by_family",
                            "news.direct_relevance_count", "news.related_relevance_count",
                            "news.upstream_alert_decision_counts", "news.upstream_impact_level_counts",
                            "news.newest_publication_timestamp", "news.oldest_publication_timestamp",
                            "news.newest_publication_date", "news.oldest_publication_date",
                            "news.exclusion_counts"),
        packet_pointers=tuple(news["sources"]))


def _contradiction_pointer(c):
    return f"contradictions[{c['code']}:{','.join(c['subjects'])}]"


def _conflicts(data):
    conflicts = []
    for c in data["contradictions"]:
        if c["code"] not in r.CONFLICT_CODES:
            continue
        subjects = tuple(c["subjects"])
        if c["code"] == "timeframe_opposition":
            related = (f"timeframe_relations[{subjects[0]}:{subjects[1]}].relation",)
        else:
            related = (f"market_context.relations[{subjects[0]}:{subjects[1]}:{subjects[2]}].relation",)
        conflicts.append(m.Conflict(c["code"], subjects, (_contradiction_pointer(c), *related), tuple(c["pointers"])))
    return tuple(sorted(conflicts, key=lambda x: (x.code, x.subjects)))


def _attention(data, frames, market, references, alignment, events):
    flags = []

    def flag(code, subjects, synthesis_pointers, packet_pointers=()):
        flags.append(m.Attention(r.ATTENTION_CODES[code], code, tuple(subjects), tuple(synthesis_pointers),
                                 _uniq(packet_pointers)))

    by_code = {}
    for c in data["contradictions"]:
        by_code.setdefault(c["code"], []).append(c)
    for c in by_code.get("timeframe_opposition", []):
        flag("timeframe_opposition_present", c["subjects"], (_contradiction_pointer(c),), c["pointers"])
    for row in alignment.by_interval:
        if row.oppose_count:
            opposing = [c for c in by_code.get("market_context_opposes_timeframe", []) if c["subjects"][0] == row.interval]
            flag("market_context_opposition_present", (row.interval,), tuple(_contradiction_pointer(c) for c in opposing),
                 [p for c in opposing for p in c["pointers"]])
    for interval in r.INTERVALS:
        if frames[interval]["state_direction"] == r.UNAVAILABLE:
            flag("evidence_incomplete", ("technical", interval),
                 (f"timeframes[{interval}].state_direction",), frames[interval]["sources"][:2])
    if not market["available"]:
        flag("evidence_incomplete", ("market_context",), ("market_context.available",))
    for ref in references:
        if ref["value_sign"] == r.UNAVAILABLE and r.MISALIGNED not in ref["unavailable_reasons"]:
            flag("evidence_incomplete", ("market_context", ref["reference"], ref["basis"]),
                 (f"market_context.references[{_ref_key(ref['reference'], ref['basis'])}].value_sign",), ref["sources"])
    for c in by_code.get("comparison_misaligned", []):
        flag("comparison_misaligned", c["subjects"], (_contradiction_pointer(c),), c["pointers"])
    for c in by_code.get("context_session_mismatch", []):
        flag("context_session_mismatch", c["subjects"], (_contradiction_pointer(c),), c["pointers"])
    if market["available"] and market["freshness_status"] in r.NOT_CURRENT_FRESHNESS:
        flag("market_context_not_current", ("market_context", market["freshness_status"]),
             ("market_context.freshness_status",))
    for c in by_code.get("news_unavailable", []):
        flag("news_unavailable", ("news",), (_contradiction_pointer(c), "news.availability"), c["pointers"])
    sec = next((c.count for c in events.counts_by_family if c.key == "sec"), 0)
    if sec:
        flag("sec_filing_present", ("sec",), ("news.counts_by_family[sec]",),
             [p for p in data["news"]["sources"] if p.startswith(f"news.items[{r.SEC_IDENTITY_VERSION}:")])
    return tuple(sorted(flags, key=lambda a: (a.category, a.code, a.subjects)))


def _comparison(previous, current):
    prev_ref = previous["packet_ref"]
    reasons = tuple(reason for reason, key in ((r.FORMAT_VERSION_MISMATCH, "synthesis_format_version"),
                                               (r.RULES_VERSION_MISMATCH, "rules_version"))
                    if previous[key] != current[key])
    elapsed = int((instant(current["packet_ref"]["as_of"], "current as_of")
                   - instant(prev_ref["as_of"], "previous as_of")).total_seconds())
    return m.Comparison(status=r.NOT_COMPARABLE if reasons else r.COMPARABLE, reasons=reasons,
                        previous_ref=m.SynthesisRef(previous["synthesis_id"], previous["synthesis_format_version"],
                                                    previous["rules_version"], prev_ref["packet_id"],
                                                    prev_ref["symbol"], prev_ref["as_of"]),
                        elapsed_seconds=elapsed)


def _identity(item):
    """news.items[<identity_version>:<event_key>] -> <identity_version>:<event_key>."""
    return item[len("news.items["):-1]


def _transitions(previous, current):
    """Closed, descriptive differences between two comparable syntheses, by stable identity (never list position)."""
    out = []

    def transition(code, subjects, previous_pointers=(), current_pointers=()):
        out.append(m.Transition(code, tuple(subjects), tuple(f"previous:{p}" for p in previous_pointers),
                                tuple(f"current:{p}" for p in current_pointers)))

    before = {f["interval"]: f for f in previous["timeframes"]}
    after = {f["interval"]: f for f in current["timeframes"]}
    for i in r.INTERVALS:
        p, c = before[i], after[i]
        if p["available"] and c["available"] and p["state"] != c["state"]:
            pointer = (f"timeframes[{i}].state",)
            transition("timeframe_state_changed", (i, p["state"], c["state"]), pointer, pointer)
        if p["state_direction"] != c["state_direction"]:
            pointer = (f"timeframes[{i}].state_direction",)
            transition("timeframe_direction_changed", (i, p["state_direction"], c["state_direction"]), pointer,
                       pointer)
    earlier, later = previous["timeframe_alignment"]["pattern"], current["timeframe_alignment"]["pattern"]
    if earlier != later:
        pointer = ("timeframe_alignment.pattern",)
        transition("pattern_changed", (earlier, later), pointer, pointer)
        if later in r.ALIGNED_PATTERNS and earlier not in r.ALIGNED_PATTERNS:
            transition("entered_alignment", (earlier, later), pointer, pointer)
        if earlier in r.ALIGNED_PATTERNS and later not in r.ALIGNED_PATTERNS:
            transition("exited_alignment", (earlier, later), pointer, pointer)

    def identities(data):
        return {(c["code"], tuple(c["subjects"])): _contradiction_pointer(c) for c in data["contradictions"]}
    old, new = identities(previous), identities(current)
    for key in sorted(new.keys() - old.keys()):
        code = "opposition_appeared" if key[0] in r.CONFLICT_CODES else "gap_appeared"
        transition(code, (key[0], *key[1]), (), (new[key],))
    for key in sorted(old.keys() - new.keys()):
        code = "opposition_resolved" if key[0] in r.CONFLICT_CODES else "gap_resolved"
        transition(code, (key[0], *key[1]), (old[key],), ())
    for domain in r.DOMAINS:
        earlier, later = previous["completeness"][domain], current["completeness"][domain]
        pointer = (f"completeness.{domain}",)
        if earlier == "unavailable" and later != "unavailable":
            transition("domain_became_available", (domain, earlier, later), pointer, pointer)
        if earlier != "unavailable" and later == "unavailable":
            transition("domain_became_unavailable", (domain, earlier, later), pointer, pointer)
    signs_before = {(x["reference"], x["basis"]): x["value_sign"] for x in previous["market_context"]["references"]}
    for ref in current["market_context"]["references"]:
        key = (ref["reference"], ref["basis"])
        if key in signs_before and signs_before[key] != ref["value_sign"]:
            pointer = (f"market_context.references[{_ref_key(*key)}].value_sign",)
            transition("reference_sign_changed", (*key, signs_before[key], ref["value_sign"]), pointer, pointer)
    items_before, items_after = set(previous["news"]["sources"]), set(current["news"]["sources"])
    added, removed = sorted(items_after - items_before), sorted(items_before - items_after)
    if added:
        ids = [_identity(item) for item in added]
        transition("news_identities_added", (str(len(ids)), *ids), (), tuple(f"news.sources[{i}]" for i in ids))
    if removed:
        ids = [_identity(item) for item in removed]
        transition("news_identities_removed", (str(len(ids)), *ids), tuple(f"news.sources[{i}]" for i in ids), ())
    return tuple(sorted(out, key=lambda t: (t.code, t.subjects)))


def build(current, previous=None):
    """MarketIntelligence for one valid EvidenceSynthesis, optionally compared with an explicit previous one.

    Raises MarketIntelligenceInputError for invalid input or an invalid pair.
    """
    if previous is None:
        data, before, comparison = validated_synthesis(current), None, None
    else:
        data, before = validated_pair(current, previous)
        comparison = _comparison(before, data)
    ref = data["packet_ref"]
    as_of = instant(ref["as_of"], "packet_ref.as_of")
    frames = {f["interval"]: f for f in data["timeframes"]}
    relations = {(rel["first"], rel["second"]): rel for rel in data["timeframe_relations"]}
    market, news = data["market_context"], data["news"]
    references = market["references"]
    structure = _structure(data, frames, relations)
    alignment = _alignment(market, references)
    events = _events(news, as_of)
    fields = dict(
        intelligence_format_version=r.INTELLIGENCE_FORMAT_VERSION, rules_version=r.RULES_VERSION,
        synthesis_ref=m.SynthesisRef(data["synthesis_id"], data["synthesis_format_version"], data["rules_version"],
                                     ref["packet_id"], ref["symbol"], ref["as_of"]),
        comparison=comparison,
        evidence_coverage=_coverage(data, frames, references),
        timeframe_structure=structure, market_context_alignment=alignment, event_presence=events,
        conflicts=_conflicts(data),
        transitions=_transitions(before, data) if comparison and comparison.status == r.COMPARABLE else (),
        attention=_attention(data, frames, market, references, alignment, events),
        provenance=m.Provenance(data["synthesis_id"], data["synthesis_format_version"], data["rules_version"],
                                ref["packet_id"], tuple(data["provenance"]["source_domains"])))
    draft = m.MarketIntelligence(intelligence_id="", **fields)
    return replace(draft, intelligence_id=content_id(draft.body()))
