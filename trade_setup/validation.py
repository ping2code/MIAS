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
  exactly ``setup_candidates`` / ``no_setup``. A global-gate ``no_setup`` has no candidates or rejections and a
  ``global_gate_failed`` step 8. A screened assessment has canonical candidates, each re-screened against the
  policy from its own source facts, and canonical aggregated rejections that, together with the candidates,
  partition the chain.
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


def _trace_pair_ok(t, last):
    pair = (t["result"], t["reason"])
    if last:
        return pair in r.SCREENING_STEP_RESULTS
    return ((t["result"] == r.PASS and t["reason"] is None)
            or (t["result"] == r.FAIL and t["reason"] in r.NO_SETUP_REASONS)
            or (t["result"] == r.NOT_EVALUATED and t["reason"] in r.NOT_EVALUATED_REASONS
                and t["reason"] != "global_gate_failed"))


def _validated_screening(data, policy, reasons):
    """Candidates and rejections of a screened (globally eligible) assessment: structure, canonical order, the
    candidate/rejection partition, and a re-screen of every candidate's own source facts against the policy."""
    from trade_setup import builder, screening
    status, candidates, rejections = data["outcome"]["status"], data["candidates"], data["rejections"]
    side, screening_step = data["market_bias"]["side"], data["decision_trace"][-1]
    _require(side in r.OPTION_SIDES, "assessment market_bias is inconsistent")
    expected_step = (r.PASS, r.CANDIDATES_AVAILABLE) if status == r.SETUP_CANDIDATES \
        else (r.FAIL, "no_candidate_satisfies_policy")
    _require((screening_step["result"], screening_step["reason"]) == expected_step,
             "assessment contract_screening step is inconsistent with its outcome")
    _require(screening_step["pointers"] == list(builder.screening_pointers(policy)),
             "assessment contract_screening pointers are inconsistent with the policy")
    _require(isinstance(candidates, list) and isinstance(rejections, list),
             "assessment candidates and rejections must be lists")
    if status == r.SETUP_CANDIDATES:
        _require(candidates != [], "assessment setup_candidates requires candidates")
    else:
        _require(candidates == [], "assessment no_setup carries no candidates")
        _require("no_candidate_satisfies_policy" in reasons and set(reasons) <= set(r.SCREENING_NO_SETUP_REASONS),
                 "assessment screening no_setup reasons are inconsistent")

    expected_checks = [screening.check(rule).to_dict() for rule in screening.enabled_rules(policy)]
    candidate_ids = []
    for c in candidates:
        _require(isinstance(c, dict) and set(c) == {"source", "derived", "policy_checks"}
                 and isinstance(c["source"], dict) and set(c["source"]) == set(m.CandidateSource.__dataclass_fields__)
                 and isinstance(c["derived"], dict) and set(c["derived"]) == set(m.Derived.__dataclass_fields__),
                 "assessment candidate is malformed")
        source = c["source"]
        _require(isinstance(source["contract_id"], str) and source["option_type"] == side
                 and canonical_decimal(source["strike"]) is not None, "assessment candidate identity is inconsistent")
        try:
            # The candidate's own delta is held to the same Phase 9 sign/range bound screening used, so a sign-invalid
            # delta (e.g. call -0.5) re-screens as delta_unavailable whenever the delta rule is enabled.
            out_of_bounds = screening.delta_out_of_bounds(source["option_type"], source["delta"])
            reasons_for, checks = screening.evaluate(source, policy, side, out_of_bounds)
            derived = screening.derived(source).to_dict()
        except TradeSetupInputError:
            raise TradeSetupInputError("assessment candidate source facts are malformed") from None
        _require(reasons_for == () and [k.to_dict() for k in checks] == expected_checks == c["policy_checks"],
                 "assessment candidate does not satisfy the policy")
        _require(c["derived"] == derived, "assessment candidate derived facts are inconsistent")
        candidate_ids.append(source["contract_id"])
    order = [screening.canonical_order(c["source"]) for c in candidates]
    _require(order == sorted(order) and len(set(candidate_ids)) == len(candidate_ids),
             "assessment candidates are not unique and in canonical order")

    rejected = set()
    for x in rejections:
        _require(isinstance(x, dict) and set(x) == {"reason_code", "count", "contract_ids"}
                 and x["reason_code"] in r.REJECTION_REASONS and isinstance(x["contract_ids"], list)
                 and x["contract_ids"] and all(isinstance(i, str) for i in x["contract_ids"])
                 and x["contract_ids"] == sorted(set(x["contract_ids"])) and x["count"] == len(x["contract_ids"]),
                 "assessment rejection is malformed")
        rejected |= set(x["contract_ids"])
    codes = [x["reason_code"] for x in rejections]
    _require(codes == sorted(set(codes)), "assessment rejections are not unique and in reason order")
    _require(not rejected & set(candidate_ids), "assessment candidate also appears in rejections")
    _require(len(rejected) + len(candidate_ids) == data["execution_readiness"]["contract_count"],
             "assessment candidates and rejections do not partition the contracts")
    _require("source_timing_unverified" not in reasons or "time_basis_unverified" in codes,
             "assessment screening no_setup reasons are inconsistent")


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
    bias = data["market_bias"]
    _require(isinstance(bias, dict) and bias.get("state") in r.BIAS_STATES
             and bias.get("side") == r.SIDE_BY_BIAS.get(bias["state"]), "assessment market_bias is inconsistent")
    trace = data["decision_trace"]
    _require(isinstance(trace, list) and [t.get("step") for t in trace] == list(range(1, len(trace) + 1))
             and [t.get("rule") for t in trace] == list(r.TRACE_RULES), "assessment decision_trace is malformed")
    for i, t in enumerate(trace):
        _require(set(t) == {"step", "rule", "result", "reason", "pointers"} and t["result"] in r.TRACE_RESULTS,
                 "assessment decision_trace entry is malformed")
        _require(_trace_pair_ok(t, i == len(trace) - 1), "assessment decision_trace reason is inconsistent with its result")
        _require(isinstance(t["pointers"], list) and t["pointers"] == sorted(set(t["pointers"]))
                 and all(r.POINTER.fullmatch(p) for p in t["pointers"]), "assessment decision_trace pointers are malformed")
    gates, screening_step = trace[:-1], trace[-1]
    gate_failures = sorted({t["reason"] for t in gates if t["result"] == r.FAIL})
    if gate_failures:
        # A global gate failed: contract screening never runs.
        _require(reasons == gate_failures, "assessment no_setup_reasons do not match the failed trace steps")
        _require((screening_step["result"], screening_step["reason"]) == (r.NOT_EVALUATED, "global_gate_failed"),
                 "assessment contract_screening step is inconsistent with the global gates")
        _require(data["candidates"] == [] and data["rejections"] == [],
                 "assessment global-gate no_setup carries no candidates or rejections")
    else:
        _require(screening_step["reason"] != "global_gate_failed",
                 "assessment contract_screening step is inconsistent with the global gates")
        _validated_screening(data, policy, reasons)
    inputs, provenance = data["inputs"], data["provenance"]
    mi_ref, oi_ref = inputs["market_intelligence_ref"], inputs["options_intelligence_ref"]
    _require(provenance == dict(market_intelligence_id=mi_ref["intelligence_id"],
                                options_intelligence_id=oi_ref["options_intelligence_id"],
                                options_intelligence_format_version=oi_ref["options_intelligence_format_version"],
                                policy_id=policy.policy_id, rules_version=r.RULES_VERSION,
                                pointer_version=r.POINTER_VERSION),
             "assessment provenance is inconsistent with its inputs and policy")
    _require(mi_ref["symbol"] == oi_ref["underlying"] == inputs["symbol"], "assessment input symbols are inconsistent")
    _require(oi_ref["options_intelligence_format_version"] == r.OI_V2
             or all(getattr(policy, rule) is None for rule in r.V2_POLICY_RULES), "policy_requires_phase9_v2")
    return data


def verify_assessment(assessment, market_intelligence, options_intelligence):
    """Structural validation, then a rebuild from the inputs and the embedded policy; returns the plain dict."""
    from trade_setup.builder import assess
    data = validated_assessment(assessment)
    expected = assess(market_intelligence, options_intelligence, data["policy"]).to_dict()
    body = lambda d: {k: v for k, v in d.items() if k != "assessment_id"}  # noqa: E731
    if canonical_json(body(expected)) != canonical_json(body(data)):
        differing = sorted(k for k in body(data) if expected.get(k) != data.get(k))
        raise TradeSetupInputError(f"assessment does not match its inputs at {', '.join(differing)}")
    return data
