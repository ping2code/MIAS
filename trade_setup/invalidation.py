"""Pure Trade Setup InvalidationCheck (Phase 10D): a point-in-time market-state check of an existing setup.

``check_invalidation(assessment, market_intelligence)`` checks whether a newer sealed MarketIntelligence still shows
the market state that established a ``setup_candidates`` TradeSetupAssessment (``pattern_must_remain``). It returns
a separate, content-addressed ``InvalidationCheck`` (``phase10-invalidation-v1``) and never mutates the assessment.

Decision (``decide``), from the new MI's copied technical status and timeframe pattern:

1. ``technical_status != available``: ``not_evaluable`` / ``technical_evidence_not_available``;
2. ``pattern == incomplete``: ``not_evaluable`` / ``timeframe_evidence_incomplete``;
3. ``pattern == required_pattern``: ``holds`` / ``required_pattern_present``;
4. any other pattern: ``invalidated`` / ``required_pattern_absent``.

Only ``holds`` confirms the requirement. Missing or incomplete evidence is not proof of invalidation.

Inputs that cannot give a meaningful result raise ``TradeSetupInputError``; nothing is sealed for them:
- an invalid or tampered assessment (including its invalidation descriptor) or MarketIntelligence;
- an outcome other than ``setup_candidates``;
- a symbol mismatch;
- an MI whose ``as_of`` is not strictly later than the establishing MI's (same or older).

The check is stateless. A later ``holds`` never reactivates a setup an earlier check invalidated: the earliest
``invalidated`` check is terminal downstream, and a new setup needs a new TradeSetupAssessment.

No prices, P&L, premium, targets or stops. No clock, environment, network, files, database or AI. Imports only the
standard library and trade_setup.
"""
from dataclasses import replace

from trade_setup import model as m
from trade_setup import rules as r
from trade_setup.canonical import canonical_json, content_id
from trade_setup.validation import (TradeSetupInputError, _plain, _require, _sealed_id, instant, validated_assessment,
                                    validated_market_intelligence)

STEP_POINTERS = dict(r.INVALIDATION_STEPS)
REF_FIELDS = {"setup_ref": tuple(m.SetupRef.__dataclass_fields__),
              "market_intelligence_ref": tuple(m.MarketIntelligenceRef.__dataclass_fields__)}


def decide(required_pattern, pattern, technical_status):
    """(result, reason, trace steps) for an observed state; a pure function of these three values."""
    def step(rule, result, reason=None):
        return m.TraceStep(len(steps) + 1, rule, result, reason, STEP_POINTERS[rule])

    steps = []
    steps.append(step("symbol_match", r.PASS))
    steps.append(step("as_of_order", r.PASS))
    skipped = r.INVALIDATION_NOT_EVALUATED_REASON
    if technical_status != r.REQUIRED_TECHNICAL_STATUS:
        reason = "technical_evidence_not_available"
        steps.append(step("technical_evidence", r.FAIL, reason))
        steps.append(step("timeframe_completeness", r.NOT_EVALUATED, skipped))
        steps.append(step("pattern_match", r.NOT_EVALUATED, skipped))
    elif pattern == "incomplete":
        reason = "timeframe_evidence_incomplete"
        steps.append(step("technical_evidence", r.PASS))
        steps.append(step("timeframe_completeness", r.FAIL, reason))
        steps.append(step("pattern_match", r.NOT_EVALUATED, skipped))
    else:
        reason = "required_pattern_present" if pattern == required_pattern else "required_pattern_absent"
        steps.append(step("technical_evidence", r.PASS))
        steps.append(step("timeframe_completeness", r.PASS))
        steps.append(step("pattern_match", r.PASS if pattern == required_pattern else r.FAIL, reason))
    return r.INVALIDATION_REASONS[reason], reason, tuple(steps)


def _setup(assessment):
    """The validated setup_candidates assessment (descriptor checks included in validated_assessment)."""
    data = validated_assessment(assessment)
    _require(data["outcome"]["status"] == r.SETUP_CANDIDATES and data["candidates"] != [],
             "invalidation requires a setup_candidates assessment")
    _require(data["market_bias"]["side"] in r.OPTION_SIDES and isinstance(data["market_bias"]["invalidation"], dict),
             "setup has no market-state invalidation descriptor")
    return data


def check_invalidation(assessment, market_intelligence):
    """The sealed InvalidationCheck of a setup against a newer MarketIntelligence; raises on invalid input."""
    setup = _setup(assessment)
    mi = validated_market_intelligence(market_intelligence)
    inputs, bias = setup["inputs"], setup["market_bias"]
    descriptor, established = bias["invalidation"], inputs["market_intelligence_ref"]
    ref = mi["synthesis_ref"]
    _require(ref["symbol"] == inputs["symbol"], "market intelligence symbol does not match the setup")
    _require(instant(ref["as_of"], "market intelligence as_of") > instant(established["as_of"], "established as_of"),
             "market intelligence is not newer than the market intelligence that established the setup")
    pattern, technical = mi["timeframe_structure"]["pattern"], mi["evidence_coverage"]["technical_status"]
    result, reason, trace = decide(descriptor["required_pattern"], pattern, technical)
    draft = m.InvalidationCheck(
        invalidation_format_version=r.INVALIDATION_FORMAT_VERSION, invalidation_id="",
        rules_version=r.INVALIDATION_RULES_VERSION,
        setup_ref=m.SetupRef(setup["assessment_id"], setup["assessment_format_version"], setup["rules_version"],
                             setup["policy"]["policy_id"], bias["side"], inputs["assessment_as_of"],
                             descriptor["established_by"], established["as_of"]),
        market_intelligence_ref=m.MarketIntelligenceRef(mi["intelligence_id"], mi["intelligence_format_version"],
                                                        mi["rules_version"], ref["symbol"], ref["as_of"]),
        symbol=inputs["symbol"],
        required_market_state=m.RequiredMarketState(descriptor["rule"], descriptor["required_pattern"],
                                                    r.REQUIRED_TECHNICAL_STATUS),
        observed_market_state=m.ObservedMarketState(pattern, technical),
        result=result, reason=reason, decision_trace=trace,
        provenance=m.InvalidationProvenance(setup["assessment_id"], mi["intelligence_id"],
                                            descriptor["established_by"], r.INVALIDATION_RULES_VERSION,
                                            r.INVALIDATION_POINTER_VERSION))
    return replace(draft, invalidation_id=content_id(draft.body()))


def validated_invalidation(check):
    """Structural, standalone validation of a sealed InvalidationCheck; returns the plain dict. Never repairs."""
    data = _plain(check)
    _require(isinstance(data, dict) and set(data) == set(m.INVALIDATION_FIELDS),
             "invalidation check must have exactly the 12 phase10-invalidation-v1 keys")
    _require(data["invalidation_format_version"] == r.INVALIDATION_FORMAT_VERSION,
             "unsupported invalidation format version")
    _require(data["rules_version"] == r.INVALIDATION_RULES_VERSION, "unsupported invalidation rules version")
    _sealed_id(data, "invalidation_id", "invalidation check")
    for key, fields in REF_FIELDS.items():
        _require(isinstance(data[key], dict) and set(data[key]) == set(fields), f"invalidation {key} is malformed")
    setup, mi_ref = data["setup_ref"], data["market_intelligence_ref"]
    _require(all(isinstance(setup[k], str) and r.CONTENT_ID.fullmatch(setup[k])
                 for k in ("assessment_id", "policy_id", "established_by"))
             and setup["assessment_format_version"] == r.ASSESSMENT_FORMAT_VERSION
             and setup["assessment_rules_version"] == r.RULES_VERSION and setup["side"] in r.OPTION_SIDES,
             "invalidation setup_ref is malformed")
    _require(isinstance(mi_ref["intelligence_id"], str) and r.CONTENT_ID.fullmatch(mi_ref["intelligence_id"])
             and mi_ref["intelligence_format_version"] == r.MI_FORMAT_VERSION
             and mi_ref["rules_version"] == r.MI_RULES_VERSION, "invalidation market_intelligence_ref is malformed")
    _require(isinstance(data["symbol"], str) and r.SYMBOL.fullmatch(data["symbol"]) and mi_ref["symbol"] == data["symbol"],
             "invalidation symbols are inconsistent")
    established = instant(setup["established_as_of"], "established_as_of")
    _require(instant(mi_ref["as_of"], "market intelligence as_of") > established
             and instant(setup["assessment_as_of"], "assessment_as_of") >= established,
             "invalidation market intelligence is not newer than the establishing market intelligence")
    required_pattern = r.PATTERN_BY_BIAS[{"call": "bullish", "put": "bearish"}[setup["side"]]]
    _require(data["required_market_state"] == dict(rule=r.INVALIDATION_RULE, required_pattern=required_pattern,
                                                   required_technical_status=r.REQUIRED_TECHNICAL_STATUS),
             "invalidation required_market_state is inconsistent with the setup side")
    observed = data["observed_market_state"]
    _require(isinstance(observed, dict) and set(observed) == {"pattern", "technical_status"}
             and observed["pattern"] in r.MI_PATTERNS and observed["technical_status"] in r.MI_TECHNICAL_STATUSES,
             "invalidation observed_market_state is malformed")
    _require(data["result"] in r.INVALIDATION_RESULTS and data["reason"] in r.INVALIDATION_REASONS,
             "invalidation result or reason is not supported")
    result, reason, trace = decide(required_pattern, observed["pattern"], observed["technical_status"])
    _require((data["result"], data["reason"]) == (result, reason),
             "invalidation result and reason are inconsistent with the observed market state")
    _require(isinstance(data["decision_trace"], list)
             and all(isinstance(t, dict) and isinstance(t.get("pointers"), list)
                     and all(isinstance(p, str) and r.INVALIDATION_POINTER.fullmatch(p) for p in t["pointers"])
                     for t in data["decision_trace"])
             and data["decision_trace"] == [t.to_dict() for t in trace],
             "invalidation decision_trace is inconsistent")
    _require(data["provenance"] == dict(assessment_id=setup["assessment_id"],
                                        market_intelligence_id=mi_ref["intelligence_id"],
                                        established_by=setup["established_by"],
                                        rules_version=r.INVALIDATION_RULES_VERSION,
                                        pointer_version=r.INVALIDATION_POINTER_VERSION),
             "invalidation provenance is inconsistent")
    return data


def verify_invalidation(check, assessment, market_intelligence):
    """Structural validation, then a rebuild from the setup and MarketIntelligence that must match byte for byte."""
    data = validated_invalidation(check)  # the id is already proven to match the body
    expected = check_invalidation(assessment, market_intelligence).to_dict()
    body = lambda d: {k: v for k, v in d.items() if k != "invalidation_id"}  # noqa: E731
    if canonical_json(body(expected)) != canonical_json(body(data)):
        differing = sorted(k for k in body(data) if expected.get(k) != data.get(k))
        raise TradeSetupInputError(f"invalidation check does not match its inputs at {', '.join(differing)}")
    return data
