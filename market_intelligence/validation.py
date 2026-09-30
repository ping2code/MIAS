"""Fail-closed validation of one EvidenceSynthesis before Market Intelligence. Nothing is ever repaired.

**Inputs:** a typed ``evidence_synthesis.model.EvidenceSynthesis`` (via its own ``to_dict()``) or its canonical
dict. Both go through the same checks:

- a supported ``synthesis_format_version`` (``phase7g-v1``) and ``rules_version`` (``phase7g-rules-v1``);
- exact key sets at every level, closed vocabularies, JSON-native types (integers are never booleans);
- ``synthesis_id`` recomputes: ``"sha256:" + SHA-256(canonical body without synthesis_id)``;
- **consistency**: every derived synthesis fact is re-derived with the Phase 7G rule functions and must match, so a
  body edited and then resealed is still rejected:
  - ``state_direction`` from ``state``; each timeframe relation; the pattern and its counts;
  - each market-context relation from direction and sign; the reference order;
  - ages from ``packet_ref.as_of`` and the copied timestamps;
  - the complete contradiction key set (code, subjects), including the session-date rule;
  - completeness, availability and provenance agreement.

**Previous synthesis (Phase 8B):** ``validated_pair`` validates the current and the previous synthesis
independently, then requires the same symbol, ``previous.as_of < current.as_of`` and two different syntheses.
A version difference between two individually valid syntheses is not an error: the comparison is then reported
as ``not_comparable``.

**INVALID vs INCOMPLETE:** invalid input raises ``MarketIntelligenceInputError`` with a stable message.
Incomplete evidence (missing or insufficient-data timeframes, unavailable market context or benchmarks, empty or
unavailable news) is valid and is described, never rejected.

Validation reads and never mutates its input.
"""
from datetime import date, datetime
from itertools import product
import re

from evidence_synthesis import rules as sr
from evidence_synthesis.model import EvidenceSynthesis
from market_intelligence import rules
from market_intelligence.canonical import content_id

SYNTHESIS_KEYS = frozenset(("synthesis_format_version", "synthesis_id", "rules_version", "packet_ref", "completeness",
                            "timeframes", "timeframe_relations", "timeframe_alignment", "market_context", "news",
                            "contradictions", "provenance"))
PACKET_REF_KEYS = frozenset(("packet_id", "packet_format_version", "symbol", "as_of"))
COMPLETENESS_KEYS = frozenset(("market_context", "technical", "news", "market_context_reasons", "technical_reasons",
                               "news_reasons", "technical_missing"))
TIMEFRAME_KEYS = frozenset(("interval", "available", "state_direction", "state", "trend", "breakout_state",
                            "momentum", "ema_alignment", "vwap_position", "technical_confidence", "bar_end",
                            "age_seconds", "missing_reason", "sources"))
ROW_FACTS = ("state", "trend", "breakout_state", "momentum", "ema_alignment", "vwap_position", "technical_confidence",
             "bar_end", "age_seconds")
MARKET_KEYS = frozenset(("available", "session_date", "calendar_state", "freshness_status", "context_as_of",
                         "context_age_seconds", "references", "relations"))
REFERENCE_KEYS = frozenset(("reference", "basis", "value_sign", "aligned", "unavailable_reasons", "sources"))
NEWS_KEYS = frozenset(("availability", "item_count", "counts_by_family", "direct_relevance_count",
                       "related_relevance_count", "upstream_alert_decision_counts", "upstream_impact_level_counts",
                       "newest_publication_timestamp", "oldest_publication_timestamp",
                       "newest_publication_age_seconds", "newest_publication_date", "oldest_publication_date",
                       "newest_publication_date_age_days", "exclusion_counts", "sources"))
PROVENANCE_KEYS = frozenset(("packet_id", "packet_format_version", "synthesis_format_version", "rules_version",
                             "source_domains"))
CONTENT_ID = re.compile(r"sha256:[0-9a-f]{64}")
NEWS_STATES = ("available", "available_empty", "partial", "unavailable")


class MarketIntelligenceInputError(ValueError):
    """The input is not a valid, supported EvidenceSynthesis (fail closed; the message names the problem)."""


def _require(condition, message):
    if not condition:
        raise MarketIntelligenceInputError(message)


def _keys(value, expected, name):
    _require(isinstance(value, dict) and set(value) == expected, f"{name} has unexpected keys")


def _int(value, name, minimum=None):
    _require(isinstance(value, int) and not isinstance(value, bool), f"{name} must be an integer")
    _require(minimum is None or value >= minimum, f"{name} must be at least {minimum}")


def _str(value, name, optional=False):
    _require((optional and value is None) or (isinstance(value, str) and value), f"{name} must be a string")


def _strings(value, name):
    _require(isinstance(value, list) and all(isinstance(v, str) and v for v in value), f"{name} must be a string list")


def instant(value, name):
    _require(isinstance(value, str), f"{name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MarketIntelligenceInputError(f"{name} is not ISO 8601") from None
    _require(parsed.utcoffset() is not None, f"{name} must be timezone-aware")
    return parsed


def day(value, name):
    _require(isinstance(value, str), f"{name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise MarketIntelligenceInputError(f"{name} is not an ISO date") from None


def _seconds(later, earlier):
    return int((later - earlier).total_seconds())


def _counts(value, name, total=None):
    _require(isinstance(value, list), f"{name} must be a list")
    keys = []
    for entry in value:
        _keys(entry, {"key", "count"}, name)
        _str(entry["key"], f"{name}.key")
        _int(entry["count"], f"{name}.count", 1)
        keys.append(entry["key"])
    _require(keys == sorted(set(keys)), f"{name} must be sorted and unique")
    _require(total is None or sum(e["count"] for e in value) == total, f"{name} does not add up to item_count")


# --------------------------------------------------------------------------- sections

def _timeframes(data, as_of):
    frames = data["timeframes"]
    _require(isinstance(frames, list) and [f.get("interval") if isinstance(f, dict) else None for f in frames]
             == list(rules.INTERVALS), "timeframes must be exactly 1d, 1h, 5m in order")
    for frame in frames:
        interval = frame["interval"]
        _keys(frame, TIMEFRAME_KEYS, f"timeframes[{interval}]")
        _require(isinstance(frame["available"], bool), f"timeframes[{interval}].available must be a boolean")
        _strings(frame["sources"], f"timeframes[{interval}].sources")
        if not frame["available"]:
            _require(frame["state_direction"] == rules.UNAVAILABLE and all(frame[k] is None for k in ROW_FACTS),
                     f"timeframes[{interval}] is unavailable but carries row facts")
            _str(frame["missing_reason"], f"timeframes[{interval}].missing_reason")
            continue
        _require(frame["missing_reason"] is None, f"timeframes[{interval}] is available but has a missing_reason")
        _require(frame["state"] in sr.TECHNICAL_STATES, f"timeframes[{interval}].state is not supported")
        _require(frame["state_direction"] == sr.state_direction(frame["state"]),
                 f"timeframes[{interval}].state_direction is inconsistent with its state")
        _require(frame["trend"] in sr.TECHNICAL_TRENDS, f"timeframes[{interval}].trend is not supported")
        _require(frame["breakout_state"] in sr.BREAKOUT_STATES, f"timeframes[{interval}].breakout_state is not supported")
        _require(frame["technical_confidence"] in sr.TECHNICAL_CONFIDENCES,
                 f"timeframes[{interval}].technical_confidence is not supported")
        for key in ("momentum", "ema_alignment", "vwap_position"):
            _str(frame[key], f"timeframes[{interval}].{key}", optional=True)
        bar_end = instant(frame["bar_end"], f"timeframes[{interval}].bar_end")
        _require(bar_end <= as_of, f"timeframes[{interval}].bar_end is later than as_of")
        _int(frame["age_seconds"], f"timeframes[{interval}].age_seconds", 0)
        _require(frame["age_seconds"] == _seconds(as_of, bar_end), f"timeframes[{interval}].age_seconds is inconsistent")
    return {f["interval"]: f for f in frames}


def _relations_and_alignment(data, frames):
    relations = data["timeframe_relations"]
    _require(isinstance(relations, list) and len(relations) == len(rules.PAIRS), "timeframe_relations is malformed")
    for relation, (first, second) in zip(relations, rules.PAIRS):
        _keys(relation, {"first", "second", "relation", "sources"}, "timeframe_relations entry")
        _require((relation["first"], relation["second"]) == (first, second),
                 "timeframe_relations must follow the pair order (1d,1h), (1h,5m), (1d,5m)")
        _require(relation["relation"] in rules.RELATIONS, f"timeframe_relations[{first}:{second}].relation is not supported")
        _require(relation["relation"] == sr.pair_relation(frames[first]["state_direction"],
                                                          frames[second]["state_direction"]),
                 f"timeframe_relations[{first}:{second}].relation is inconsistent with the directions")
        _strings(relation["sources"], f"timeframe_relations[{first}:{second}].sources")
    alignment = data["timeframe_alignment"]
    _keys(alignment, {"pattern", "bullish", "bearish", "non_directional", "unavailable"}, "timeframe_alignment")
    directions = [frames[i]["state_direction"] for i in rules.INTERVALS]
    _require(alignment["pattern"] in sr.PATTERNS, "timeframe_alignment.pattern is not supported")
    _require(alignment["pattern"] == sr.timeframe_pattern(directions),
             "timeframe_alignment.pattern is inconsistent with the directions")
    for key in (rules.BULLISH, rules.BEARISH, rules.NON_DIRECTIONAL, rules.UNAVAILABLE):
        _int(alignment[key], f"timeframe_alignment.{key}", 0)
        _require(alignment[key] == directions.count(key), f"timeframe_alignment.{key} is inconsistent")
    return {(r["first"], r["second"]): r for r in relations}


def _market_context(data, frames, as_of):
    market = data["market_context"]
    _keys(market, MARKET_KEYS, "market_context")
    _require(isinstance(market["available"], bool), "market_context.available must be a boolean")
    _require(isinstance(market["references"], list) and isinstance(market["relations"], list),
             "market_context references/relations must be lists")
    if not market["available"]:
        _require(all(market[k] is None for k in MARKET_KEYS - {"available", "references", "relations"})
                 and not market["references"] and not market["relations"],
                 "unavailable market_context must not carry facts")
        return market, {}
    for key in ("session_date", "calendar_state", "freshness_status"):
        _str(market[key], f"market_context.{key}", optional=True)
    if market["session_date"] is not None:
        day(market["session_date"], "market_context.session_date")
    context_as_of = instant(market["context_as_of"], "market_context.context_as_of")
    _require(context_as_of <= as_of, "market_context.context_as_of is later than as_of")
    _int(market["context_age_seconds"], "market_context.context_age_seconds", 0)
    _require(market["context_age_seconds"] == _seconds(as_of, context_as_of),
             "market_context.context_age_seconds is inconsistent")
    references = {}
    for ref in market["references"]:
        _keys(ref, REFERENCE_KEYS, "market_context reference")
        _str(ref["reference"], "market_context reference name")
        _require(ref["basis"] in rules.BASES, "market_context reference basis is not supported")
        key = (ref["reference"], ref["basis"])
        _require(key not in references, "duplicate market_context reference")
        _require(ref["value_sign"] in rules.VALUE_SIGNS, "market_context reference value_sign is not supported")
        _require(ref["aligned"] in (True, False, None), "market_context reference aligned is malformed")
        _strings(ref["unavailable_reasons"], "market_context reference unavailable_reasons")
        _require(ref["unavailable_reasons"] == sorted(ref["unavailable_reasons"]),
                 "market_context reference unavailable_reasons must be sorted")
        _strings(ref["sources"], "market_context reference sources")
        references[key] = ref
    order = [(r["reference"] != rules.SELF_REFERENCE, r["reference"], rules.BASES.index(r["basis"]))
             for r in market["references"]]
    _require(order == sorted(order), "market_context references are not in canonical order")
    _require([k for k in references if k[0] == rules.SELF_REFERENCE]
             == [(rules.SELF_REFERENCE, b) for b in rules.BASES], "market_context must carry both self references")
    expected = [(i, ref, basis) for i, (ref, basis) in product(rules.INTERVALS, references)]
    actual = []
    for relation in market["relations"]:
        _keys(relation, {"interval", "reference", "basis", "relation"}, "market_context relation")
        actual.append((relation["interval"], relation["reference"], relation["basis"]))
        _require(relation["relation"] in rules.RELATIONS, "market_context relation is not supported")
        if actual[-1] in expected:
            ref = references[(relation["reference"], relation["basis"])]
            _require(relation["relation"] == sr.context_relation(frames[relation["interval"]]["state_direction"],
                                                                 ref["value_sign"]),
                     "market_context relation is inconsistent with direction and sign")
    _require(actual == expected, "market_context relations must cover every timeframe x reference in order")
    return market, references


def _news(data, as_of):
    news = data["news"]
    _keys(news, NEWS_KEYS, "news")
    _require(news["availability"] in NEWS_STATES, "news.availability is not supported")
    _int(news["item_count"], "news.item_count", 0)
    _require((news["item_count"] > 0) == (news["availability"] == "available"),
             "news.availability is inconsistent with item_count")
    total = news["item_count"]
    _counts(news["counts_by_family"], "news.counts_by_family", total)
    _counts(news["upstream_alert_decision_counts"], "news.upstream_alert_decision_counts", total)
    _counts(news["upstream_impact_level_counts"], "news.upstream_impact_level_counts", total)
    _counts(news["exclusion_counts"], "news.exclusion_counts")
    for key in ("direct_relevance_count", "related_relevance_count"):
        _int(news[key], f"news.{key}", 0)
    _require(news["direct_relevance_count"] + news["related_relevance_count"] <= total,
             "news relevance counts exceed item_count")
    _strings(news["sources"], "news.sources")
    _require(news["sources"] == sorted(set(news["sources"])) and len(news["sources"]) == total,
             "news.sources must be sorted, unique and one per item")
    newest, oldest = news["newest_publication_timestamp"], news["oldest_publication_timestamp"]
    _require((newest is None) == (oldest is None) == (news["newest_publication_age_seconds"] is None),
             "news publication timestamps are inconsistent")
    if newest is not None:
        newest_at, oldest_at = instant(newest, "news.newest_publication_timestamp"), \
            instant(oldest, "news.oldest_publication_timestamp")
        _require(oldest_at <= newest_at <= as_of, "news publication timestamps are out of order")
        _require(news["newest_publication_age_seconds"] == _seconds(as_of, newest_at),
                 "news.newest_publication_age_seconds is inconsistent")
    newest_day, oldest_day = news["newest_publication_date"], news["oldest_publication_date"]
    _require((newest_day is None) == (oldest_day is None) == (news["newest_publication_date_age_days"] is None),
             "news publication dates are inconsistent")
    if newest_day is not None:
        as_of_day = as_of.astimezone(sr.EXCHANGE_TZ).date()
        newest_date, oldest_date = day(newest_day, "news.newest_publication_date"), \
            day(oldest_day, "news.oldest_publication_date")
        _require(oldest_date <= newest_date < as_of_day, "news publication dates are out of order")
        _require(news["newest_publication_date_age_days"] == (as_of_day - newest_date).days,
                 "news.newest_publication_date_age_days is inconsistent")
    return news


def _completeness(data, frames, market, news):
    completeness = data["completeness"]
    _keys(completeness, COMPLETENESS_KEYS, "completeness")
    for key in ("market_context_reasons", "technical_reasons", "news_reasons"):
        _strings(completeness[key], f"completeness.{key}")
        _require(completeness[key] == sorted(completeness[key]), f"completeness.{key} must be sorted")
    present = sum(frames[i]["available"] for i in rules.INTERVALS)
    _require(completeness["technical"] == ("available" if present == 3 else "partial" if present else "unavailable"),
             "completeness.technical is inconsistent with the timeframes")
    _require(completeness["market_context"] == ("available" if market["available"] else "unavailable"),
             "completeness.market_context is inconsistent with market_context")
    _require(completeness["news"] == news["availability"], "completeness.news is inconsistent with news")
    missing = completeness["technical_missing"]
    _require(isinstance(missing, list) and all(isinstance(m, dict) and set(m) == {"interval", "reason"} for m in missing),
             "completeness.technical_missing is malformed")
    _require([(m["interval"], m["reason"]) for m in missing]
             == [(i, frames[i]["missing_reason"]) for i in rules.INTERVALS if not frames[i]["available"]],
             "completeness.technical_missing is inconsistent with the timeframes")
    return completeness


def _expected_contradictions(frames, relations, market, references, news):
    """The complete Phase 7G contradiction key set, re-derived from the validated facts."""
    expected = {(sr.TIMEFRAME_UNAVAILABLE, (i,)) for i in rules.INTERVALS
                if frames[i]["state_direction"] == rules.UNAVAILABLE}
    expected |= {(sr.TIMEFRAME_OPPOSITION, pair) for pair, r in relations.items() if r["relation"] == rules.OPPOSE}
    if not market["available"]:
        expected.add((sr.MARKET_CONTEXT_UNAVAILABLE, ("market_context",)))
    else:
        expected |= {(sr.MARKET_CONTEXT_OPPOSES_TIMEFRAME, (r["interval"], r["reference"], r["basis"]))
                     for r in market["relations"] if r["relation"] == rules.OPPOSE}
        expected |= {(sr.COMPARISON_MISALIGNED, key) for key, ref in references.items()
                     if rules.MISALIGNED in ref["unavailable_reasons"]}
        present = [(instant(frames[i]["bar_end"], "bar_end"), -rules.INTERVALS.index(i), i)
                   for i in rules.INTERVALS if frames[i]["bar_end"] is not None]
        if market["session_date"] is not None and present:
            latest = max(present)
            if latest[0].astimezone(sr.EXCHANGE_TZ).date().isoformat() != market["session_date"]:
                expected.add((sr.CONTEXT_SESSION_MISMATCH, ("market_context", latest[2])))
    if news["availability"] == "unavailable":
        expected.add((sr.NEWS_UNAVAILABLE, ("news",)))
    return expected


def _contradictions(data, expected):
    contradictions = data["contradictions"]
    _require(isinstance(contradictions, list), "contradictions must be a list")
    keys = []
    for c in contradictions:
        _keys(c, {"code", "subjects", "pointers"}, "contradiction")
        _require(c["code"] in sr.CONTRADICTION_CODES, "contradiction code is not supported")
        _strings(c["subjects"], "contradiction subjects")
        _strings(c["pointers"], "contradiction pointers")
        _require(len(c["pointers"]) > 0, "contradiction pointers must not be empty")
        keys.append((c["code"], tuple(c["subjects"])))
    _require(keys == sorted(set(keys)), "contradictions must be sorted by (code, subjects) and unique")
    _require(set(keys) == expected, "contradictions are inconsistent with the synthesis facts")


def validated_synthesis(synthesis):
    """The synthesis as a plain dict, after every check. Typed syntheses use their own to_dict()."""
    if isinstance(synthesis, EvidenceSynthesis):
        data = synthesis.to_dict()
    elif isinstance(synthesis, dict):
        data = synthesis
    else:
        raise MarketIntelligenceInputError("input must be an EvidenceSynthesis or its canonical dict")
    _require(set(data) == SYNTHESIS_KEYS, "synthesis must have exactly the 12 phase7g-v1 top-level keys")
    _require(data["synthesis_format_version"] in rules.SUPPORTED_SYNTHESIS_FORMATS,
             "unsupported synthesis format version")
    _require(data["rules_version"] in rules.SUPPORTED_SYNTHESIS_RULES, "unsupported synthesis rules version")
    _require(isinstance(data["synthesis_id"], str) and CONTENT_ID.fullmatch(data["synthesis_id"]),
             "synthesis_id is malformed")
    try:
        recomputed = content_id({k: v for k, v in data.items() if k != "synthesis_id"})
    except (TypeError, ValueError):
        raise MarketIntelligenceInputError("synthesis body is not canonical JSON") from None
    _require(recomputed == data["synthesis_id"], "synthesis_id does not match the synthesis body (tampered or corrupt)")
    ref = data["packet_ref"]
    _keys(ref, PACKET_REF_KEYS, "packet_ref")
    _require(isinstance(ref["packet_id"], str) and CONTENT_ID.fullmatch(ref["packet_id"]), "packet_ref.packet_id is malformed")
    _str(ref["packet_format_version"], "packet_ref.packet_format_version")
    _str(ref["symbol"], "packet_ref.symbol")
    as_of = instant(ref["as_of"], "packet_ref.as_of")
    frames = _timeframes(data, as_of)
    relations = _relations_and_alignment(data, frames)
    market, references = _market_context(data, frames, as_of)
    news = _news(data, as_of)
    _completeness(data, frames, market, news)
    _contradictions(data, _expected_contradictions(frames, relations, market, references, news))
    provenance = data["provenance"]
    _keys(provenance, PROVENANCE_KEYS, "provenance")
    _require((provenance["packet_id"], provenance["packet_format_version"], provenance["synthesis_format_version"],
              provenance["rules_version"]) == (ref["packet_id"], ref["packet_format_version"],
                                               data["synthesis_format_version"], data["rules_version"]),
             "provenance is inconsistent with the synthesis")
    _strings(provenance["source_domains"], "provenance.source_domains")
    return data


def validated_pair(current, previous):
    """(current, previous) as plain dicts: each fully validated, then checked as a comparable pair of inputs."""
    current, previous = validated_synthesis(current), validated_synthesis(previous)
    _require(previous["synthesis_id"] != current["synthesis_id"],
             "previous and current synthesis are the same synthesis")
    _require(previous["packet_ref"]["symbol"] == current["packet_ref"]["symbol"],
             "previous synthesis symbol does not match the current synthesis")
    _require(instant(previous["packet_ref"]["as_of"], "previous as_of") < instant(current["packet_ref"]["as_of"],
                                                                                  "current as_of"),
             "previous synthesis as_of must be earlier than the current synthesis as_of")
    return current, previous
