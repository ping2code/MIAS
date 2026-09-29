"""Fail-closed validation of one ``phase7c-v1`` EvidencePacket before synthesis. Nothing is ever repaired.

**Inputs:** a typed ``evidence_packet.models.EvidencePacket`` (converted with its own ``to_dict()``) or its
canonical JSON/dict representation. Both go through the same checks:

- ``format_version == "phase7c-v1"``;
- exactly the 8 top-level keys;
- ``packet_id`` recomputes: ``"sha256:" + SHA-256(canonical body without packet_id)``. A tampered body is
  rejected;
- structure and vocabularies of every domain, and cross-field consistency (availability status against content);
- the Phase 7C assembler invariants that synthesis relies on (hardened in Phase 7H):
  - no evidence after ``as_of``: technical ``bar_end``, news ``published_at`` and ``observed_at`` must be at or
    before ``as_of``; a date-only ``publication_date`` must be strictly before ``as_of``'s America/New_York date
    (the assembler's ``after_as_of`` rule);
  - exclusion reasons are unique and each count is an integer >= 1 (the assembler emits a sorted ``Counter``);
  - benchmark names are tickers (``market_data.models.SYMBOL``, the same validator the Phase 7B runner applies to
    ``--benchmarks``). This also rules out the reserved synthesis reference ``"self"``.

**INVALID vs INCOMPLETE:**

- INVALID raises ``PacketValidationError``: malformed or tampered input, an unsupported version, a missing key, a
  bad vocabulary value, or an inconsistent structure.
- INCOMPLETE is still **valid**: a technical timeframe the packet itself marks missing, unavailable market
  context, unavailable or empty news. The synthesis describes it.

Validation reads and never mutates the input.
"""
from datetime import date, datetime
import re

from evidence_packet.models import EvidencePacket
from evidence_synthesis import rules
from evidence_synthesis.canonical import content_id
from market_data.models import EXCHANGE_TZ, SYMBOL

PACKET_KEYS = frozenset(("format_version", "packet_id", "symbol", "as_of", "market_context", "technical", "news",
                         "provenance"))
PACKET_ID = re.compile(r"sha256:[0-9a-f]{64}")
AVAILABILITY = ("available", "available_empty", "partial", "unavailable")


class PacketValidationError(ValueError):
    """The input is not a valid phase7c-v1 EvidencePacket (fail closed; the message names the problem)."""


def _require(condition, message):
    if not condition:
        raise PacketValidationError(message)


def _optional_str(value, name):
    _require(value is None or isinstance(value, str), f"{name} must be a string or null")


def _decimal(value, name):
    """A canonical decimal string or null (strict: floats and non-finite values are malformed)."""
    if value is None:
        return
    try:
        rules.sign(value)
    except ValueError:
        raise PacketValidationError(f"{name} must be a canonical decimal string") from None


def parse_instant(value, name):
    _require(isinstance(value, str), f"{name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise PacketValidationError(f"{name} is not ISO 8601") from None
    _require(parsed.utcoffset() is not None, f"{name} must be timezone-aware")
    return parsed


def parse_day(value, name):
    _require(isinstance(value, str), f"{name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise PacketValidationError(f"{name} is not an ISO date") from None


def _availability(section, name, allowed):
    _require(isinstance(section, dict), f"{name} must be an object")
    availability = section.get("availability")
    _require(isinstance(availability, dict) and set(availability) == {"status", "reasons"},
             f"{name}.availability is malformed")
    _require(availability["status"] in allowed, f"{name}.availability.status is not supported")
    reasons = availability["reasons"]
    _require(isinstance(reasons, list) and all(isinstance(r, str) for r in reasons),
             f"{name}.availability.reasons is malformed")
    return availability["status"]


def _technical(section, symbol, as_of):
    status = _availability(section, "technical", ("available", "partial", "unavailable"))
    frames = section.get("timeframes")
    _require(isinstance(frames, list) and [f.get("interval") if isinstance(f, dict) else None for f in frames]
             == list(rules.INTERVALS), "technical.timeframes must be exactly 1d, 1h, 5m in order")
    present = 0
    for frame in frames:
        interval = frame["interval"]
        _require(set(frame) == {"interval", "bar_end", "row", "missing_reason"},
                 f"technical.{interval} has unexpected keys")
        row, missing = frame["row"], frame["missing_reason"]
        _require((row is None) != (missing is None), f"technical.{interval} needs exactly one of row/missing_reason")
        if row is None:
            _require(isinstance(missing, str) and missing, f"technical.{interval}.missing_reason is malformed")
            _require(frame["bar_end"] is None, f"technical.{interval}.bar_end must be null when missing")
            continue
        present += 1
        _require(parse_instant(frame["bar_end"], f"technical.{interval}.bar_end") <= as_of,
                 f"technical.{interval}.bar_end is later than the packet as_of")
        _require(isinstance(row, dict), f"technical.{interval}.row must be an object")
        _require(row.get("symbol") == symbol and row.get("interval") == interval,
                 f"technical.{interval}.row symbol/interval mismatch")
        _require(row.get("technical_state") in rules.TECHNICAL_STATES, f"technical.{interval} has an unknown state")
        _require(row.get("trend") in rules.TECHNICAL_TRENDS, f"technical.{interval} has an unknown trend")
        _require(row.get("breakout_state") in rules.BREAKOUT_STATES, f"technical.{interval} has an unknown breakout")
        _require(row.get("confidence") in rules.TECHNICAL_CONFIDENCES, f"technical.{interval} has an unknown confidence")
        for key in ("momentum", "ema_alignment", "vwap_position"):
            _require(key in row, f"technical.{interval}.row lacks {key}")
            _optional_str(row[key], f"technical.{interval}.{key}")
    expected = "available" if present == 3 else "partial" if present else "unavailable"
    _require(status == expected, "technical.availability.status is inconsistent with its timeframes")


def _market_context(section, symbol, as_of):
    status = _availability(section, "market_context", ("available", "unavailable"))
    _require(set(section) == {"availability", "context"}, "market_context has unexpected keys")
    context = section["context"]
    if status != "available":
        _require(context is None, "unavailable market_context must not carry a context")
        return
    _require(isinstance(context, dict), "market_context.context must be an object")
    _require(context.get("context_format_version") == rules.MARKET_CONTEXT_FORMAT_VERSION,
             "unsupported market context format version")
    _require(context.get("symbol") == symbol, "market_context symbol mismatch")
    context_as_of = parse_instant(context.get("as_of"), "market_context.as_of")
    _require(context_as_of <= as_of, "market_context.as_of is later than the packet as_of")
    _optional_str(context.get("calendar_state"), "market_context.calendar_state")
    if context.get("session_date") is not None:
        parse_day(context["session_date"], "market_context.session_date")
    series = context.get("symbol_context")
    _require(isinstance(series, dict), "market_context.symbol_context must be an object")
    for key in ("return_since_prev_close", "return_since_open"):
        _require(key in series, f"market_context.symbol_context lacks {key}")
        _decimal(series[key], f"market_context.symbol_context.{key}")
    freshness = series.get("freshness")
    _require(freshness is None or (isinstance(freshness, dict) and isinstance(freshness.get("status"), str)),
             "market_context freshness is malformed")
    comparisons = context.get("comparisons")
    _require(isinstance(comparisons, list), "market_context.comparisons must be a list")
    seen = set()
    for comparison in comparisons:
        _require(isinstance(comparison, dict), "market_context comparison must be an object")
        key = (comparison.get("benchmark"), comparison.get("basis"))
        _require(isinstance(key[0], str) and SYMBOL.fullmatch(key[0]) and key[1] in rules.BASES,
                 "market_context comparison identity is malformed")
        _require(key not in seen, "duplicate market_context comparison")
        seen.add(key)
        _decimal(comparison.get("relative_return"), "comparison.relative_return")
        _require(comparison.get("aligned") in (True, False, None), "comparison.aligned is malformed")
        unavailable = comparison.get("unavailable")
        _require(isinstance(unavailable, list) and all(isinstance(r, str) for r in unavailable),
                 "comparison.unavailable is malformed")


def _news(section, as_of):
    status = _availability(section, "news", AVAILABILITY)
    as_of_day = as_of.astimezone(EXCHANGE_TZ).date()
    _require(set(section) == {"availability", "items", "excluded"}, "news has unexpected keys")
    items, excluded = section["items"], section["excluded"]
    _require(isinstance(items, list) and isinstance(excluded, list), "news items/excluded must be lists")
    _require(bool(items) == (status == "available"), "news.availability.status is inconsistent with its items")
    identities = set()
    for item in items:
        _require(isinstance(item, dict), "news item must be an object")
        identity = (item.get("identity_version"), item.get("event_key"))
        _require(all(isinstance(v, str) and v for v in identity), "news item identity is malformed")
        _require(identity not in identities, "duplicate news item identity")
        identities.add(identity)
        facts, relevance = item.get("facts"), item.get("relevance")
        score, delivery = item.get("upstream_score"), item.get("delivery")
        _require(all(isinstance(v, dict) for v in (facts, relevance, score, delivery)), "news item is malformed")
        _require(facts.get("family") in ("news", "sec"), "news item family is not supported")
        if facts.get("published_at") is not None:
            _require(parse_instant(facts["published_at"], "news published_at") <= as_of,
                     "news published_at is later than the packet as_of")
        if facts.get("publication_date") is not None:
            _require(parse_day(facts["publication_date"], "news publication_date") < as_of_day,
                     "news publication_date is not before the packet as_of date")
        for key in ("symbols", "direct_symbols", "related_symbols"):
            _require(isinstance(relevance.get(key), list), f"news relevance.{key} is malformed")
        _optional_str(score.get("impact_level"), "news impact_level")
        _optional_str(delivery.get("alert_decision"), "news alert_decision")
        _require(parse_instant(item.get("observed_at"), "news observed_at") <= as_of,
                 "news observed_at is later than the packet as_of")
    reasons = set()
    for entry in excluded:
        _require(isinstance(entry, dict) and isinstance(entry.get("reason"), str)
                 and isinstance(entry.get("count"), int) and not isinstance(entry.get("count"), bool),
                 "news exclusion entry is malformed")
        _require(entry["count"] >= 1, "news exclusion count must be at least 1")
        _require(entry["reason"] not in reasons, "duplicate news exclusion reason")
        reasons.add(entry["reason"])


def validated_packet(packet):
    """The packet as a plain dict, after every check. Typed packets use their own to_dict()."""
    if isinstance(packet, EvidencePacket):
        data = packet.to_dict()
    elif isinstance(packet, dict):
        data = packet
    else:
        raise PacketValidationError("input must be an EvidencePacket or its canonical dict")
    _require(set(data) == PACKET_KEYS, "packet must have exactly the 8 phase7c-v1 top-level keys")
    _require(data["format_version"] == rules.PACKET_FORMAT_VERSION, "unsupported packet format version")
    _require(isinstance(data["packet_id"], str) and PACKET_ID.fullmatch(data["packet_id"]), "packet_id is malformed")
    body = {k: v for k, v in data.items() if k != "packet_id"}
    try:
        recomputed = content_id(body)
    except (TypeError, ValueError):
        raise PacketValidationError("packet body is not canonical JSON") from None
    _require(recomputed == data["packet_id"], "packet_id does not match the packet body (tampered or corrupt)")
    _require(isinstance(data["symbol"], str) and SYMBOL.fullmatch(data["symbol"]), "packet symbol is malformed")
    as_of = parse_instant(data["as_of"], "packet as_of")
    _require(isinstance(data["provenance"], dict), "packet provenance must be an object")
    _market_context(data["market_context"], data["symbol"], as_of)
    _technical(data["technical"], data["symbol"], as_of)
    _news(data["news"], as_of)
    return data
