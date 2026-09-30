"""Fail-closed validation for Trade Setup (pure; standard library and trade_setup only). Nothing is ever repaired.

- ``validated_market_intelligence``: a sealed Phase 8 object (typed with ``to_dict()`` or its dict), checked
  **structurally**: exactly 13 keys, ``phase8-v1`` / ``phase8-rules-v1``, ``intelligence_id`` recomputes, the
  ``synthesis_ref`` symbol and ``as_of``, and the closed pattern, technical-status, conflict and attention
  vocabularies. No EvidenceSynthesis re-derivation, and no import of the evidence layers or market_intelligence.
- ``validated_options_intelligence``: a sealed Phase 9 object, ``phase9-v1`` or ``phase9-v2``, checked
  structurally: exactly 13 keys, the version pair, the id, the snapshot reference, the closed quote, activity,
  session and time-basis vocabularies, canonical contract order, and exactly the contract fields of its format.
  No snapshot re-derivation, and no import of options_intelligence or options_data.
- ``validated_assessment``: a sealed TradeSetupAssessment on its own (keys, versions, id, policy, closed
  vocabularies, outcome and trace consistency, reference and provenance agreement). The outcome vocabulary is
  exactly ``setup_candidates`` / ``no_setup``; in Phase 10B only a global-gate ``no_setup`` can validate.
- ``verify_assessment``: the structural check, then a rebuild from (MarketIntelligence, OptionsIntelligence,
  embedded policy) that must match byte for byte.

Errors are ``TradeSetupInputError`` (inputs and assessments) and ``PolicyError`` (the policy).
"""
from datetime import datetime
from decimal import Decimal

from trade_setup import model as m
from trade_setup import rules as r
from trade_setup.canonical import canonical_decimal, canonical_json, content_id
from trade_setup.policy import PolicyError, validated_policy


class TradeSetupInputError(ValueError):
    """Invalid input or assessment (the message names the problem; never a no_setup outcome)."""


def _require(condition, message):
    if not condition:
        raise TradeSetupInputError(message)


def _plain(value):
    return value.to_dict() if callable(getattr(value, "to_dict", None)) else value


def instant(value, name):
    _require(isinstance(value, str), f"{name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise TradeSetupInputError(f"{name} is not ISO 8601") from None
    _require(parsed.utcoffset() is not None, f"{name} must be timezone-aware")
    return parsed


def _sealed_id(data, key, name):
    _require(isinstance(data[key], str) and r.CONTENT_ID.fullmatch(data[key]), f"{name} id is malformed")
    try:
        recomputed = content_id({k: v for k, v in data.items() if k != key})
    except (TypeError, ValueError):
        raise TradeSetupInputError(f"{name} body is not canonical JSON") from None
    _require(recomputed == data[key], f"{name} id does not match its body (tampered or corrupt)")


def validated_market_intelligence(market_intelligence):
    data = _plain(market_intelligence)
    _require(isinstance(data, dict) and set(data) == set(r.MI_TOP_LEVEL),
             "market intelligence must have exactly the 13 phase8-v1 keys")
    _require(data["intelligence_format_version"] == r.MI_FORMAT_VERSION, "unsupported market intelligence format version")
    _require(data["rules_version"] == r.MI_RULES_VERSION, "unsupported market intelligence rules version")
    _sealed_id(data, "intelligence_id", "market intelligence")
    ref = data["synthesis_ref"]
    _require(isinstance(ref, dict) and set(ref) == set(r.MI_SYNTHESIS_REF)
             and isinstance(ref["symbol"], str) and r.SYMBOL.fullmatch(ref["symbol"]),
             "market intelligence synthesis_ref is malformed")
    instant(ref["as_of"], "market intelligence as_of")
    structure, coverage = data["timeframe_structure"], data["evidence_coverage"]
    _require(isinstance(structure, dict) and structure.get("pattern") in r.MI_PATTERNS,
             "market intelligence timeframe pattern is not supported")
    _require(isinstance(coverage, dict) and coverage.get("technical_status") in r.MI_TECHNICAL_STATUSES,
             "market intelligence technical_status is not supported")
    _require(isinstance(data["conflicts"], list) and all(isinstance(c, dict) and c.get("code") in r.MI_CONFLICT_CODES
                                                         for c in data["conflicts"]),
             "market intelligence conflicts are malformed")
    _require(isinstance(data["attention"], list)
             and all(isinstance(a, dict) and r.MI_ATTENTION_CODES.get(a.get("code")) == a.get("category")
                     for a in data["attention"]), "market intelligence attention is malformed")
    _require(isinstance(data["market_context_alignment"], dict)
             and isinstance(data["market_context_alignment"].get("available"), bool),
             "market intelligence market_context_alignment is malformed")
    return data


def _contract_key(c):
    return (c["expiration"], c["option_type"], Decimal(c["strike"]), c["contract_id"])


def _optional_decimal(value, positive=False):
    if value is None:
        return True
    number = canonical_decimal(value)
    return number is not None and (number > 0 if positive else number >= 0)


def validated_options_intelligence(options_intelligence):
    data = _plain(options_intelligence)
    _require(isinstance(data, dict) and set(data) == set(r.OI_TOP_LEVEL),
             "options intelligence must have exactly the 13 phase9 keys")
    fmt = data["options_intelligence_format_version"]
    _require(fmt in r.OI_FORMATS, "unsupported options intelligence format version")
    _require(data["rules_version"] == r.OI_FORMATS[fmt], "unsupported options intelligence rules version")
    _sealed_id(data, "options_intelligence_id", "options intelligence")
    ref = data["snapshot_ref"]
    _require(isinstance(ref, dict) and set(ref) == set(r.OI_SNAPSHOT_REF)
             and isinstance(ref["underlying"], str) and r.SYMBOL.fullmatch(ref["underlying"])
             and isinstance(ref["snapshot_id"], str) and r.CONTENT_ID.fullmatch(ref["snapshot_id"]),
             "options intelligence snapshot_ref is malformed")
    instant(ref["as_of"], "options intelligence as_of")
    _require(isinstance(data["provenance"], dict) and data["provenance"].get("rules_version") == r.OI_FORMATS[fmt],
             "options intelligence provenance is inconsistent")
    contracts = data["contracts"]
    _require(isinstance(contracts, list), "options intelligence contracts must be a list")
    keys = set(r.OI_CONTRACT_V1) | (set(r.OI_CONTRACT_V2_EXTRA) if fmt == r.OI_V2 else set())
    for c in contracts:
        _require(isinstance(c, dict) and set(c) == keys,
                 "options intelligence contract fields do not match its format version")
        _require(c["option_type"] in r.OPTION_SIDES and canonical_decimal(c["strike"]) is not None,
                 "options intelligence contract identity is malformed")
        _require(c["quote_state"] in r.OI_QUOTE_STATES, "options intelligence quote_state is not supported")
        _require(c["volume_state"] in r.OI_ACTIVITY_STATES and c["open_interest_state"] in r.OI_ACTIVITY_STATES,
                 "options intelligence activity state is not supported")
        _require(isinstance(c["day"], dict) and c["day"].get("session_relation") in r.OI_SESSION_RELATIONS,
                 "options intelligence session_relation is not supported")
        _require(all(isinstance(c[g], dict) and c[g].get("time_basis") in r.OI_TIME_BASES
                     for g in ("greeks", "implied_volatility")), "options intelligence time basis is not supported")
        if fmt == r.OI_V2:
            _require(_optional_decimal(c["current_session_volume"]) and _optional_decimal(c["open_interest_value"])
                     and c["open_interest_time_basis"] in r.OI_TIME_BASES
                     and _optional_decimal(c["shares_per_contract"], positive=True),
                     "options intelligence phase9-v2 contract facts are malformed")
    order = [_contract_key(c) for c in contracts]
    _require(order == sorted(order) and len({k[3] for k in order}) == len(order),
             "options intelligence contracts are not unique and in canonical order")
    completeness = data["chain_completeness"]
    _require(isinstance(completeness, dict) and completeness.get("contract_count") == len(contracts)
             and isinstance(completeness.get("truncated"), bool), "options intelligence chain_completeness is malformed")
    return data


def validated_assessment(assessment):
    """Structural, standalone validation of a sealed TradeSetupAssessment; returns the plain dict."""
    data = _plain(assessment)
    _require(isinstance(data, dict) and set(data) == set(m.TOP_LEVEL_FIELDS),
             "assessment must have exactly the 12 phase10-v1 keys")
    _require(data["assessment_format_version"] == r.ASSESSMENT_FORMAT_VERSION, "unsupported assessment format version")
    _require(data["rules_version"] == r.RULES_VERSION, "unsupported assessment rules version")
    _sealed_id(data, "assessment_id", "assessment")
    try:
        policy = validated_policy(data["policy"])
    except PolicyError as error:
        raise TradeSetupInputError(f"assessment policy is invalid: {error}") from None
    outcome = data["outcome"]
    _require(isinstance(outcome, dict) and outcome.get("status") in r.OUTCOME_STATUSES,
             "assessment outcome status is not supported")
    reasons = outcome.get("no_setup_reasons")
    _require(isinstance(reasons, list) and reasons == sorted(set(reasons)) and set(reasons) <= set(r.NO_SETUP_REASONS),
             "assessment no_setup_reasons must be sorted, unique and from the closed set")
    _require((outcome["status"] == r.NO_SETUP) == bool(reasons), "assessment outcome is inconsistent with its reasons")
    _require(outcome["status"] != r.SETUP_CANDIDATES, "setup_candidates requires contract screening (Phase 10C)")
    _require(data["candidates"] == [] and data["rejections"] == [],
             "phase 10B assessments carry no candidates or rejections")
    bias = data["market_bias"]
    _require(isinstance(bias, dict) and bias.get("state") in r.BIAS_STATES
             and bias.get("side") == r.SIDE_BY_BIAS.get(bias["state"]), "assessment market_bias is inconsistent")
    trace = data["decision_trace"]
    _require(isinstance(trace, list) and [t.get("step") for t in trace] == list(range(1, len(trace) + 1))
             and [t.get("rule") for t in trace] == list(r.TRACE_RULES), "assessment decision_trace is malformed")
    for t in trace:
        _require(set(t) == {"step", "rule", "result", "reason", "pointers"} and t["result"] in r.TRACE_RESULTS,
                 "assessment decision_trace entry is malformed")
        _require((t["result"] == r.PASS and t["reason"] is None)
                 or (t["result"] == r.FAIL and t["reason"] in r.NO_SETUP_REASONS)
                 or (t["result"] == r.NOT_EVALUATED and t["reason"] in r.NOT_EVALUATED_REASONS),
                 "assessment decision_trace reason is inconsistent with its result")
        _require(isinstance(t["pointers"], list) and t["pointers"] == sorted(set(t["pointers"]))
                 and all(r.POINTER.fullmatch(p) for p in t["pointers"]), "assessment decision_trace pointers are malformed")
    _require(sorted({t["reason"] for t in trace if t["result"] == r.FAIL}) == reasons,
             "assessment no_setup_reasons do not match the failed trace steps")
    # Phase 10B seals only global-gate no_setup: some global gate failed, so screening was not evaluated.
    gates, screening = trace[:-1], trace[-1]
    _require(any(t["result"] == r.FAIL for t in gates) and all(t["reason"] != "global_gate_failed" for t in gates)
             and (screening["result"], screening["reason"]) == (r.NOT_EVALUATED, "global_gate_failed"),
             "assessment contract_screening step requires a failed global gate (screening is Phase 10C)")
    inputs, provenance = data["inputs"], data["provenance"]
    mi_ref, oi_ref = inputs["market_intelligence_ref"], inputs["options_intelligence_ref"]
    _require(provenance == dict(market_intelligence_id=mi_ref["intelligence_id"],
                                options_intelligence_id=oi_ref["options_intelligence_id"],
                                options_intelligence_format_version=oi_ref["options_intelligence_format_version"],
                                policy_id=policy.policy_id, rules_version=r.RULES_VERSION,
                                pointer_version=r.POINTER_VERSION),
             "assessment provenance is inconsistent with its inputs and policy")
    _require(mi_ref["symbol"] == oi_ref["underlying"] == inputs["symbol"], "assessment input symbols are inconsistent")
    return data


def verify_assessment(assessment, market_intelligence, options_intelligence):
    """Structural validation, then a rebuild from the inputs and the embedded policy; returns the plain dict."""
    from trade_setup.builder import prescreen
    data = validated_assessment(assessment)
    rebuilt = prescreen(market_intelligence, options_intelligence, data["policy"])
    _require(isinstance(rebuilt, m.TradeSetupAssessment),
             "assessment does not match its inputs: every global gate passes (contract screening required)")
    expected = rebuilt.to_dict()
    body = lambda d: {k: v for k, v in d.items() if k != "assessment_id"}  # noqa: E731
    if canonical_json(body(expected)) != canonical_json(body(data)):
        differing = sorted(k for k in body(data) if expected.get(k) != data.get(k))
        raise TradeSetupInputError(f"assessment does not match its inputs at {', '.join(differing)}")
    return data
