"""Fail-closed validation for Phase 12 alerts (pure). Nothing is ever repaired.

Inputs are sealed upstream objects, validated with the existing pure Phase 10 validators (standard library only):
- ``trade_setup.validation.validated_assessment`` for a TradeSetupAssessment;
- ``trade_setup.invalidation.validated_invalidation`` for an InvalidationCheck;
- ``trade_setup.validation.validated_market_intelligence`` for a MarketIntelligence.

Each recomputes the object's content id and checks its closed contract. An invalid input raises
``AlertInputError`` and never becomes a "no alert" result.

``validated_alert`` checks a sealed AlertEvent on its own: exact schema, versions and id, the code's fixed subject,
roles, transition, facts and trace, ``as_of`` equal to the current source's, and provenance. ``verify_alert`` then
rebuilds it from the explicit sealed inputs and requires identical bytes.
"""
from datetime import datetime

from alert_engine import model as m
from alert_engine import rules as r
from alert_engine.canonical import canonical_json, content_id


class AlertInputError(ValueError):
    """Invalid input or alert (the message names the problem)."""


def _require(condition, message):
    if not condition:
        raise AlertInputError(message)


def _plain(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


def instant(value, name):
    _require(isinstance(value, str), f"{name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AlertInputError(f"{name} must be an ISO 8601 string") from None
    _require(parsed.utcoffset() is not None, f"{name} must be timezone-aware")
    return parsed


def _upstream(validator, value, name):
    from trade_setup.validation import TradeSetupInputError
    try:
        return validator(value)
    except TradeSetupInputError as error:
        raise AlertInputError(f"{name} is invalid: {error}") from None


def validated_assessment(assessment):
    from trade_setup.validation import validated_assessment as upstream
    return _upstream(upstream, assessment, "trade setup assessment")


def validated_check(check):
    from trade_setup.invalidation import validated_invalidation
    return _upstream(validated_invalidation, check, "invalidation check")


def validated_market_intelligence(intelligence, name):
    from trade_setup.validation import validated_market_intelligence as upstream
    return _upstream(upstream, intelligence, name)


def validated_pair(previous, current):
    """(previous, current) MarketIntelligence dicts, validated as a Phase 8 pair: distinct objects, same symbol,
    previous as_of strictly earlier."""
    previous = validated_market_intelligence(previous, "previous market intelligence")
    current = validated_market_intelligence(current, "current market intelligence")
    p, c = previous["synthesis_ref"], current["synthesis_ref"]
    _require(previous["intelligence_id"] != current["intelligence_id"] and p["synthesis_id"] != c["synthesis_id"],
             "previous and current market intelligence are the same object")
    _require(p["symbol"] == c["symbol"], "previous and current market intelligence symbols do not match")
    _require(instant(p["as_of"], "previous as_of") < instant(c["as_of"], "current as_of"),
             "previous market intelligence as_of must be earlier than the current as_of")
    return previous, current


def validated_alert(alert):
    """Structural, standalone validation of a sealed AlertEvent; returns the plain dict."""
    data = _plain(alert)
    _require(isinstance(data, dict) and set(data) == set(m.ALERT_FIELDS), "alert must have exactly the 11 phase12-v1 keys")
    _require(data["alert_format_version"] == r.ALERT_FORMAT_VERSION, "unsupported alert format version")
    _require(data["rules_version"] == r.RULES_VERSION, "unsupported alert rules version")
    _require(isinstance(data["alert_id"], str) and r.CONTENT_ID.fullmatch(data["alert_id"])
             and content_id({k: v for k, v in data.items() if k != "alert_id"}) == data["alert_id"],
             "alert id does not match its body (tampered or corrupt)")
    _require(data["alert_code"] in r.ALERT_CODES, "alert code is not supported")
    shape = r.SHAPES[data["alert_code"]]

    subject = data["subject"]
    _require(isinstance(subject, dict) and set(subject) == {"kind", "symbol", "assessment_id"}
             and subject["kind"] == shape["subject"] and isinstance(subject["symbol"], str) and subject["symbol"]
             and ((subject["assessment_id"] is None) if shape["subject"] == r.SYMBOL
                  else (isinstance(subject["assessment_id"], str) and bool(r.CONTENT_ID.fullmatch(subject["assessment_id"])))),
             "alert subject is malformed")

    refs = data["source_refs"]
    _require(isinstance(refs, list) and [x.get("role") if isinstance(x, dict) else None for x in refs]
             == sorted(shape["roles"]), "alert source_refs roles are malformed")
    by_role = {}
    for ref in refs:
        _require(set(ref) == {"role", "object_kind", "id", "format_version", "rules_version", "as_of"}
                 and ref["object_kind"] == shape["roles"][ref["role"]]
                 and isinstance(ref["id"], str) and r.CONTENT_ID.fullmatch(ref["id"])
                 and (ref["format_version"], ref["rules_version"]) == {
                     r.TRADE_SETUP_ASSESSMENT: r.ASSESSMENT_VERSIONS, r.INVALIDATION_CHECK: r.INVALIDATION_VERSIONS,
                     r.MARKET_INTELLIGENCE: r.MI_VERSIONS}[ref["object_kind"]],
                 "alert source_ref is malformed")
        instant(ref["as_of"], "source_ref as_of")
        by_role[ref["role"]] = ref
    _require(data["as_of"] == by_role["current"]["as_of"], "alert as_of must equal the current source as_of")
    if "previous" in by_role:
        _require(by_role["previous"]["id"] != by_role["current"]["id"]
                 and instant(by_role["previous"]["as_of"], "previous as_of") < instant(data["as_of"], "as_of"),
                 "alert previous source must be a distinct, earlier object")
    if shape["subject"] == r.SETUP:
        setup_ref = by_role["setup"] if "setup" in by_role else by_role["current"]
        _require(subject["assessment_id"] == setup_ref["id"], "alert subject does not match the setup source")

    transition = data["transition"]
    if shape["transition"]:
        _require(isinstance(transition, dict) and set(transition) == {"previous", "current"}
                 and transition["previous"] in r.PATTERNS and transition["current"] in r.PATTERNS
                 and transition["previous"] != transition["current"], "alert transition is illegal")
    else:
        _require(transition is None, "alert transition is illegal for this alert code")

    facts = data["facts"]
    _require(isinstance(facts, dict) and tuple(sorted(facts)) == shape["facts"], "alert facts do not match the alert code")
    _require(facts["symbol"] == subject["symbol"], "alert facts symbol does not match the subject")
    _validated_facts(data["alert_code"], facts, by_role)

    expected_trace = [dict(step=i + 1, rule=rule, result="pass", pointers=list(pointers))
                      for i, (rule, pointers) in enumerate(shape["trace"])]
    _require(data["decision_trace"] == expected_trace, "alert decision_trace is inconsistent")
    _require(all(r.POINTER.fullmatch(p) for t in expected_trace for p in t["pointers"]), "alert pointers are malformed")
    _require(data["provenance"] == dict(source_ids=sorted({ref["id"] for ref in refs}), rules_version=r.RULES_VERSION,
                                        pointer_version=r.POINTER_VERSION), "alert provenance is inconsistent")
    return data


def _validated_facts(code, facts, by_role):
    if code == r.SETUP_AVAILABLE:
        _require(isinstance(facts["candidate_count"], int) and not isinstance(facts["candidate_count"], bool)
                 and facts["candidate_count"] > 0 and facts["eligible_side"] in r.SIDES
                 and facts["market_bias"] in r.DIRECTIONAL_BIAS
                 and r.SIDES.index(facts["eligible_side"]) == r.DIRECTIONAL_BIAS.index(facts["market_bias"]),
                 "alert facts are illegal for setup_available")
    elif code == r.SETUP_INVALIDATED:
        _require(facts["side"] in r.SIDES and facts["required_pattern"] == ("all_bullish" if facts["side"] == "call"
                                                                           else "all_bearish")
                 and facts["observed_pattern"] in r.PATTERNS and facts["observed_pattern"] != "incomplete"
                 and facts["observed_pattern"] != facts["required_pattern"]
                 and facts["observed_technical_status"] == "available",
                 "alert facts are illegal for setup_invalidated")
    else:
        elapsed = facts["elapsed_seconds"]
        expected = int((instant(by_role["current"]["as_of"], "current as_of")
                        - instant(by_role["previous"]["as_of"], "previous as_of")).total_seconds())
        _require(isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed == expected,
                 "alert facts are illegal for market_pattern_changed")


def verify_alert(alert, *, assessment=None, check=None, previous=None, current=None):
    """Structural validation, then a rebuild from the explicit sealed inputs; the bytes must be identical."""
    from alert_engine import builder
    data = validated_alert(alert)
    code = data["alert_code"]
    if code == r.SETUP_AVAILABLE:
        rebuilt = builder.setup_available(assessment)
    elif code == r.SETUP_INVALIDATED:
        rebuilt = builder.setup_invalidated(check)
    else:
        rebuilt = builder.market_pattern_changed(previous, current)
    _require(rebuilt is not None, "alert does not re-derive: its rule does not fire on these inputs")
    expected = rebuilt.to_dict()
    body = lambda d: {k: v for k, v in d.items() if k != "alert_id"}  # noqa: E731  (the id is proven structurally)
    if canonical_json(body(expected)) != canonical_json(body(data)):
        differing = sorted(k for k in body(data) if expected.get(k) != data.get(k))
        raise AlertInputError(f"alert does not match its inputs at {', '.join(differing)}")
    return data
