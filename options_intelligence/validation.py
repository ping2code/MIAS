"""Fail-closed validation for OptionsIntelligence (pure). Nothing is ever repaired.

Three checks:

- ``market_intelligence_reference(mi, snapshot)``: an optional MarketIntelligence (typed object with ``to_dict()``
  or its canonical dict) is accepted only when its format and rules are supported, its ``intelligence_id``
  recomputes, its symbol is the snapshot underlying and its ``as_of`` is not after the snapshot ``as_of``. It is
  used as a reference only.
- ``validated_options_intelligence(data)``: a sealed object on its own: version, rules, exact keys,
  ``options_intelligence_id`` recomputes, closed vocabularies, canonical collection order, attention and pointer
  shape, and provenance agreement.
- ``verify_against_snapshot(data, snapshot, ...)``: the structural check, then a full re-derivation from the
  snapshot, which must match byte for byte. This catches any inconsistent derived fact (DTE, strike relation,
  quote state, mid and spread, activity states and totals, ratios, IV and expiration summaries, completeness,
  attention, pointers). The message names the first differing path.
"""
from datetime import datetime
from decimal import Decimal
import re

from options_intelligence import model as m
from options_intelligence import rules as r
from options_intelligence.canonical import canonical_json, content_id

CONTENT_ID = re.compile(r"sha256:[0-9a-f]{64}")
MI_TOP_LEVEL = frozenset(("intelligence_format_version", "intelligence_id", "rules_version", "synthesis_ref",
                          "comparison", "evidence_coverage", "timeframe_structure", "market_context_alignment",
                          "event_presence", "conflicts", "transitions", "attention", "provenance"))
MI_SYNTHESIS_REF = frozenset(("synthesis_id", "synthesis_format_version", "synthesis_rules_version", "packet_id",
                              "symbol", "as_of"))
POINTER = re.compile(r"contracts\[(?P<id>[A-Z0-9.]+)\]\.[a-z_]+\.[a-z_]+|underlying_price\.value|as_of")


class OptionsIntelligenceError(ValueError):
    """Invalid OptionsIntelligence input or object (the message names the problem)."""


def _require(condition, message):
    if not condition:
        raise OptionsIntelligenceError(message)


def _instant(value, name):
    _require(isinstance(value, str), f"{name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise OptionsIntelligenceError(f"{name} is not ISO 8601") from None
    _require(parsed.utcoffset() is not None, f"{name} must be timezone-aware")
    return parsed


def market_intelligence_reference(market_intelligence, snapshot):
    """The validated reference fields for an optional MarketIntelligence (None when absent)."""
    if market_intelligence is None:
        return None
    data = market_intelligence.to_dict() if callable(getattr(market_intelligence, "to_dict", None)) \
        else market_intelligence
    _require(isinstance(data, dict) and set(data) == MI_TOP_LEVEL,
             "market intelligence must have exactly the 13 phase8-v1 keys")
    _require(data["intelligence_format_version"] in r.SUPPORTED_MARKET_INTELLIGENCE_FORMATS,
             "unsupported market intelligence format version")
    _require(data["rules_version"] in r.SUPPORTED_MARKET_INTELLIGENCE_RULES,
             "unsupported market intelligence rules version")
    _require(isinstance(data["intelligence_id"], str) and CONTENT_ID.fullmatch(data["intelligence_id"]),
             "market intelligence id is malformed")
    try:
        recomputed = content_id({k: v for k, v in data.items() if k != "intelligence_id"})
    except (TypeError, ValueError):
        raise OptionsIntelligenceError("market intelligence body is not canonical JSON") from None
    _require(recomputed == data["intelligence_id"],
             "market intelligence id does not match its body (tampered or corrupt)")
    ref = data["synthesis_ref"]
    _require(isinstance(ref, dict) and set(ref) == MI_SYNTHESIS_REF, "market intelligence synthesis_ref is malformed")
    _require(ref["symbol"] == snapshot["underlying"], "market intelligence symbol does not match the snapshot underlying")
    mi_as_of = _instant(ref["as_of"], "market intelligence as_of")
    snapshot_as_of = _instant(snapshot["as_of"], "snapshot as_of")
    _require(mi_as_of <= snapshot_as_of, "market intelligence as_of is later than the snapshot as_of")
    return m.MarketIntelligenceRef(data["intelligence_id"], ref["synthesis_id"], ref["symbol"], ref["as_of"],
                                   int((snapshot_as_of - mi_as_of).total_seconds()))


def _contract_key(contract):
    return (contract["expiration"], contract["option_type"], Decimal(contract["strike"]), contract["contract_id"])


def validated_options_intelligence(intelligence):
    """Structural, standalone validation of a sealed OptionsIntelligence; returns the plain dict."""
    data = intelligence.to_dict() if isinstance(intelligence, m.OptionsIntelligence) else intelligence
    _require(isinstance(data, dict) and set(data) == set(m.TOP_LEVEL_FIELDS),
             "options intelligence must have exactly the 13 phase9-v1 keys")
    _require(data["options_intelligence_format_version"] == r.OPTIONS_INTELLIGENCE_FORMAT_VERSION,
             "unsupported options intelligence format version")
    _require(data["rules_version"] == r.RULES_VERSION, "unsupported options intelligence rules version")
    _require(isinstance(data["options_intelligence_id"], str)
             and CONTENT_ID.fullmatch(data["options_intelligence_id"]), "options_intelligence_id is malformed")
    try:
        recomputed = content_id({k: v for k, v in data.items() if k != "options_intelligence_id"})
    except (TypeError, ValueError):
        raise OptionsIntelligenceError("options intelligence body is not canonical JSON") from None
    _require(recomputed == data["options_intelligence_id"],
             "options_intelligence_id does not match the body (tampered or corrupt)")
    ref = data["snapshot_ref"]
    _require(isinstance(ref, dict) and set(ref) == {"snapshot_id", "snapshot_format_version", "underlying", "as_of"}
             and ref["snapshot_format_version"] in r.SUPPORTED_SNAPSHOT_FORMATS
             and isinstance(ref["snapshot_id"], str) and CONTENT_ID.fullmatch(ref["snapshot_id"]),
             "snapshot_ref is malformed")
    _instant(ref["as_of"], "snapshot_ref.as_of")
    mi = data["market_intelligence_ref"]
    _require(mi is None or (isinstance(mi, dict) and set(mi) == {"intelligence_id", "synthesis_id", "symbol", "as_of",
                                                                 "as_of_gap_seconds"}
                            and mi["symbol"] == ref["underlying"] and isinstance(mi["as_of_gap_seconds"], int)
                            and mi["as_of_gap_seconds"] >= 0), "market_intelligence_ref is malformed")
    provenance = data["provenance"]
    _require(isinstance(provenance, dict) and provenance.get("snapshot_id") == ref["snapshot_id"]
             and provenance.get("market_intelligence_id") == (mi["intelligence_id"] if mi else None)
             and provenance.get("rules_version") == r.RULES_VERSION
             and provenance.get("pointer_version") == r.POINTER_VERSION,
             "provenance is inconsistent with the references")
    contracts = data["contracts"]
    _require(isinstance(contracts, list), "contracts must be a list")
    for c in contracts:
        _require(c.get("strike_relation") in r.STRIKE_RELATIONS, "contract strike_relation is not supported")
        _require(c.get("quote_state") in r.QUOTE_STATES, "contract quote_state is not supported")
        _require(c.get("volume_state") in r.ACTIVITY_STATES and c.get("open_interest_state") in r.ACTIVITY_STATES,
                 "contract activity state is not supported")
        _require(c.get("day", {}).get("session_relation") in r.SESSION_RELATIONS,
                 "contract session_relation is not supported")
        pointers = c.get("source_pointers")
        _require(isinstance(pointers, list) and pointers == sorted(set(pointers)), "source_pointers must be sorted")
        for pointer in pointers:
            match = POINTER.fullmatch(pointer)
            _require(match and match.group("id") in (None, c["contract_id"]),
                     "source pointer is malformed or names another contract")
    keys = [_contract_key(c) for c in contracts]
    _require(keys == sorted(keys) and len({k[3] for k in keys}) == len(keys),
             "contracts are not unique and in canonical order")
    expirations = [e["expiration"] for e in data["expirations"]]
    _require(expirations == sorted(set(expirations)), "expirations are not unique and ascending")
    attention = data["attention"]
    codes = []
    for a in attention:
        _require(a.get("code") in r.ATTENTION_CODES and a.get("category") == r.ATTENTION_CODES[a["code"]],
                 "attention code or category is not supported")
        _require(a.get("scope") in (r.CHAIN_SCOPE, r.CONTRACT_SCOPE), "attention scope is not supported")
        ids = a.get("contract_ids")
        _require(isinstance(ids, list) and ids == sorted(set(ids)), "attention contract_ids must be sorted and unique")
        _require(a["scope"] == r.CHAIN_SCOPE or a.get("count") == len(ids), "attention count is inconsistent")
        _require(set(a) == {"category", "code", "scope", "count", "contract_ids"},
                 "attention entries carry only category, code, scope, count and contract_ids")
        codes.append((a["category"], a["code"]))
    _require(codes == sorted(set(codes)), "attention must be sorted by (category, code) and unique")
    return data


def _first_difference(expected, actual, path=""):
    if type(expected) is not type(actual):
        return path or "<root>"
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                return f"{path}.{key}".lstrip(".")
            found = _first_difference(expected[key], actual[key], f"{path}.{key}")
            if found:
                return found.lstrip(".")
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}[len]".lstrip(".")
        for i, (a, b) in enumerate(zip(expected, actual)):
            found = _first_difference(a, b, f"{path}[{i}]")
            if found:
                return found.lstrip(".")
        return None
    return None if expected == actual else (path.lstrip(".") or "<root>")


def verify_against_snapshot(intelligence, snapshot, market_intelligence=None, *, calendar=None):
    """Structural validation, then re-derivation from the snapshot (and reference); returns the plain dict."""
    from options_intelligence.builder import build
    data = validated_options_intelligence(intelligence)
    expected = build(snapshot, market_intelligence, calendar=calendar).to_dict()
    if canonical_json(expected) != canonical_json(data):
        # The id always differs when the body does (and was verified above), so report the first body difference.
        body = lambda d: {k: v for k, v in d.items() if k != "options_intelligence_id"}  # noqa: E731
        raise OptionsIntelligenceError(
            f"options intelligence does not match its snapshot at {_first_difference(body(expected), body(data))}")
    return data
