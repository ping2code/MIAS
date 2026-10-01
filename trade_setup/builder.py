"""Pure Trade Setup builder: global gates (Phase 10B) and contract screening (Phase 10C).

``assess(market_intelligence, options_intelligence, policy)`` is the entry point. It returns the final sealed
phase10-v1 ``TradeSetupAssessment`` (``setup_candidates`` or ``no_setup``): ``prescreen``, then ``screen`` when every
global gate passed.

``prescreen(market_intelligence, options_intelligence, policy)``:

1. validates the policy (required, sealed), then both inputs structurally (no upstream imports);
2. fails closed (``TradeSetupInputError``) on a symbol mismatch, or ``policy_requires_phase9_v2`` when a
   ``phase9-v1`` input meets an enabled numeric rule (``min_volume``, ``min_open_interest``,
   ``max_premium_per_contract``). These are errors, never no_setup;
3. evaluates every global gate in a fixed order and records each in ``decision_trace``:

   ============================ =========================================================================
   input_contemporaneity        |MI as_of - OI as_of| <= policy.max_input_gap_seconds, else
                                ``inputs_not_contemporaneous``
   market_bias                  locked pattern mapping (D3); technical_status != available is insufficient
   side_allowed                 the bias side must be in policy.allowed_sides
   context_opposition_gate      policy.block_on_market_context_opposition and the MI attention code
                                ``market_context_opposition_present``, giving ``context_gate_blocked``
   context_current_gate         policy.block_on_market_context_not_current and the MI attention code
                                ``market_context_not_current``, giving ``context_gate_blocked``
   chain_completeness           policy.require_complete_chain and OI chain_completeness.truncated, giving
                                ``options_chain_truncated``
   execution_data_readiness     no contract anywhere in the chain has a usable two-sided quote (complete,
                                or locked when allowed), giving ``execution_data_unavailable``. This is a
                                chain-level fact, not screening
   ============================ =========================================================================

4. returns one of two results:

   - any global gate failed: a sealed phase10-v1 ``TradeSetupAssessment`` with ``outcome.status = no_setup``,
     the sorted failed-gate reasons, ``candidates = rejections = []`` and a final ``contract_screening`` trace
     step that is ``not_evaluated`` / ``global_gate_failed``;
   - every global gate passed: an internal, unsealed ``PreScreeningEligibility`` carrying the global-gate trace,
     bias, side, execution readiness, input facts, policy and provenance for contract screening. It is not an
     assessment and has no outcome. ``setup_candidates`` is only ever emitted after real contract screening.

``screen(eligibility, options_intelligence)`` screens every contract of the OptionsIntelligence the eligibility
references, against the enabled rules in the frozen order (``trade_setup.screening``). Each contract becomes either
a candidate or a rejection under every reason that applies.
- Candidates are in canonical order (expiration, strike, contract_id). This is an order, not a ranking.
- Rejections are aggregated by reason code.
- Step 8 is ``pass``/``candidates_available`` or ``fail``/``no_candidate_satisfies_policy``.
- A screening ``no_setup`` also carries ``execution_data_unavailable`` when no eligible-side contract has a usable
  quote. It carries ``source_timing_unverified`` when some eligible-side contract failed only on
  ``time_basis_unverified``.

SEC filing presence is never a gate. There is no score, confidence, ranking, sizing, target, stop or reward/risk.
No clock, environment, network, files, database or AI.
"""
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import timedelta, timezone

from trade_setup import model as m
from trade_setup import rules as r
from trade_setup import screening
from trade_setup.canonical import content_id
from trade_setup.policy import validated_policy
from trade_setup.validation import (TradeSetupInputError, instant, validated_market_intelligence,
                                    validated_options_intelligence)


def _counts(values):
    return tuple(m.Count(k, n) for k, n in sorted(Counter(values).items()))


def _step(steps, rule, result, reason, pointers):
    steps.append(m.TraceStep(len(steps) + 1, rule, result, reason, tuple(sorted(pointers))))


def prescreen(market_intelligence, options_intelligence, policy):
    """A no_setup TradeSetupAssessment or a PreScreeningEligibility for sealed inputs and an explicit policy; raises
    TradeSetupInputError on invalid input (never no_setup)."""
    policy = validated_policy(policy)
    mi = validated_market_intelligence(market_intelligence)
    oi = validated_options_intelligence(options_intelligence)
    symbol = mi["synthesis_ref"]["symbol"]
    if symbol != oi["snapshot_ref"]["underlying"]:
        raise TradeSetupInputError("market intelligence and options intelligence symbols do not match")
    oi_format = oi["options_intelligence_format_version"]
    if oi_format != r.OI_V2 and any(getattr(policy, rule) is not None for rule in r.V2_POLICY_RULES):
        raise TradeSetupInputError("policy_requires_phase9_v2")

    mi_as_of, oi_as_of = instant(mi["synthesis_ref"]["as_of"], "mi as_of"), instant(oi["snapshot_ref"]["as_of"], "oi as_of")
    gap = abs(mi_as_of - oi_as_of)
    steps = []
    _step(steps, "input_contemporaneity",
          *((r.PASS, None) if gap <= timedelta(seconds=policy.max_input_gap_seconds)
            else (r.FAIL, "inputs_not_contemporaneous")),
          ("mi:synthesis_ref.as_of", "oi:snapshot_ref.as_of", "policy:max_input_gap_seconds"))

    pattern, technical = mi["timeframe_structure"]["pattern"], mi["evidence_coverage"]["technical_status"]
    state = r.BIAS_BY_PATTERN[pattern] if technical == "available" else "insufficient"
    side = r.SIDE_BY_BIAS.get(state)
    _step(steps, "market_bias", *((r.PASS, None) if side else (r.FAIL, r.REASON_BY_BIAS[state])),
          ("mi:timeframe_structure.pattern", "mi:evidence_coverage.technical_status"))
    if side is None:
        _step(steps, "side_allowed", r.NOT_EVALUATED, "market_bias_not_directional", ("policy:allowed_sides",))
    else:
        _step(steps, "side_allowed", *((r.PASS, None) if side in policy.allowed_sides
                                       else (r.FAIL, "side_not_allowed_by_policy")), ("policy:allowed_sides",))

    attention = {a["code"] for a in mi["attention"]}
    for rule, flag, code in (("context_opposition_gate", "block_on_market_context_opposition",
                              "market_context_opposition_present"),
                             ("context_current_gate", "block_on_market_context_not_current",
                              "market_context_not_current")):
        pointers = (f"mi:attention[{code}]", f"policy:{flag}")
        if not getattr(policy, flag):
            _step(steps, rule, r.NOT_EVALUATED, "policy_disabled", pointers)
        else:
            _step(steps, rule, *((r.FAIL, "context_gate_blocked") if code in attention else (r.PASS, None)), pointers)

    truncated = oi["chain_completeness"]["truncated"]
    pointers = ("oi:chain_completeness.truncated", "policy:require_complete_chain")
    if not policy.require_complete_chain:
        _step(steps, "chain_completeness", r.NOT_EVALUATED, "policy_disabled", pointers)
    else:
        _step(steps, "chain_completeness", *((r.FAIL, "options_chain_truncated") if truncated else (r.PASS, None)),
              pointers)

    contracts = oi["contracts"]
    usable = ("complete", "locked") if policy.allow_locked_quote else ("complete",)
    usable_count = sum(c["quote_state"] in usable for c in contracts)
    _step(steps, "execution_data_readiness",
          *((r.PASS, None) if usable_count else (r.FAIL, "execution_data_unavailable")),
          ("oi:contracts[*].quote_state", "policy:allow_locked_quote"))

    reasons = tuple(sorted({s.reason for s in steps if s.result == r.FAIL}))
    v2 = oi_format == r.OI_V2
    shared = dict(
        policy=policy,
        inputs=m.Inputs(
            market_intelligence_ref=m.MarketIntelligenceRef(mi["intelligence_id"], mi["intelligence_format_version"],
                                                            mi["rules_version"], symbol, mi["synthesis_ref"]["as_of"]),
            options_intelligence_ref=m.OptionsIntelligenceRef(
                oi["options_intelligence_id"], oi_format, oi["rules_version"], oi["snapshot_ref"]["snapshot_id"],
                oi["snapshot_ref"]["underlying"], oi["snapshot_ref"]["as_of"]),
            symbol=symbol, assessment_as_of=max(mi_as_of, oi_as_of).astimezone(timezone.utc).isoformat(),
            input_gap_seconds=int(gap.total_seconds())),
        market_bias=m.MarketBias(state, side, pattern, technical,
                                 m.Invalidation("pattern_must_remain", r.PATTERN_BY_BIAS[state], mi["intelligence_id"])
                                 if side else None),
        execution_readiness=m.ExecutionReadiness(
            options_intelligence_format_version=oi_format, contract_count=len(contracts), truncated=truncated,
            quote_state_counts=_counts(c["quote_state"] for c in contracts), usable_two_sided_quote_count=usable_count,
            greeks_time_basis_counts=_counts(c["greeks"]["time_basis"] for c in contracts),
            iv_time_basis_counts=_counts(c["implied_volatility"]["time_basis"] for c in contracts),
            day_session_relation_counts=_counts(c["day"]["session_relation"] for c in contracts),
            numeric_activity_facts_available=v2,
            shares_per_contract_present_count=sum(c["shares_per_contract"] is not None for c in contracts) if v2
            else None),
        provenance=m.Provenance(mi["intelligence_id"], oi["options_intelligence_id"], oi_format, policy.policy_id,
                                r.RULES_VERSION, r.POINTER_VERSION))
    if not reasons:
        return m.PreScreeningEligibility(eligible_for_contract_screening=True, eligible_side=side,
                                         decision_trace=tuple(steps), **shared)
    _step(steps, "contract_screening", r.NOT_EVALUATED, "global_gate_failed", ("oi:contracts[*]",))
    draft = m.TradeSetupAssessment(
        assessment_format_version=r.ASSESSMENT_FORMAT_VERSION, assessment_id="", rules_version=r.RULES_VERSION,
        outcome=m.Outcome(r.NO_SETUP, reasons), candidates=(), rejections=(), decision_trace=tuple(steps), **shared)
    return replace(draft, assessment_id=content_id(draft.body()))


def screening_pointers(policy):
    """Step-8 pointers: the contract collection and the policy fields of every enabled screening rule."""
    return tuple(sorted({"oi:contracts[*]"} | {f"policy:{screening.RULES[rule][0]}"
                                                for rule in screening.enabled_rules(policy)}))


def screen(eligibility, options_intelligence):
    """The final TradeSetupAssessment for a globally eligible input; raises TradeSetupInputError on a mismatch."""
    if not isinstance(eligibility, m.PreScreeningEligibility) or eligibility.eligible_for_contract_screening is not True:
        raise TradeSetupInputError("contract screening needs a PreScreeningEligibility")
    oi = validated_options_intelligence(options_intelligence)
    if oi["options_intelligence_id"] != eligibility.provenance.options_intelligence_id:
        raise TradeSetupInputError("options intelligence does not match the pre-screening eligibility")
    policy, side, fmt = eligibility.policy, eligibility.eligible_side, oi["options_intelligence_format_version"]
    if fmt != r.OI_V2 and any(getattr(policy, rule) is not None for rule in r.V2_POLICY_RULES):
        raise TradeSetupInputError("policy_requires_phase9_v2")
    if side not in policy.allowed_sides or side != eligibility.market_bias.side:
        raise TradeSetupInputError("pre-screening eligibility side is inconsistent")

    candidates, rejected = [], defaultdict(list)
    side_usable_quote = timing_only = False
    for contract in oi["contracts"]:
        source = screening.source_facts(contract, fmt == r.OI_V2)
        reasons, checks = screening.evaluate(source, policy, side, "delta" in contract["greeks"]["out_of_bounds_fields"])
        if source["option_type"] == side:
            side_usable_quote |= screening.usable_quote(source["quote_state"], policy)
            timing_only |= reasons == ("time_basis_unverified",)
        for reason in reasons:
            rejected[reason].append(source["contract_id"])
        if not reasons:
            candidates.append(m.Candidate(m.CandidateSource(**source), screening.derived(source), checks))
    candidates.sort(key=lambda c: screening.canonical_order(c.source.to_dict()))
    rejections = tuple(m.Rejection(code, len(ids), tuple(sorted(ids))) for code, ids in sorted(rejected.items()))

    if candidates:
        outcome, step = m.Outcome(r.SETUP_CANDIDATES, ()), (r.PASS, r.CANDIDATES_AVAILABLE)
    else:
        reasons = {"no_candidate_satisfies_policy"} | ({"execution_data_unavailable"} if not side_usable_quote
                                                       else set()) | ({"source_timing_unverified"} if timing_only
                                                                      else set())
        outcome, step = m.Outcome(r.NO_SETUP, tuple(sorted(reasons))), (r.FAIL, "no_candidate_satisfies_policy")
    trace = eligibility.decision_trace + (m.TraceStep(len(eligibility.decision_trace) + 1, "contract_screening",
                                                      *step, screening_pointers(policy)),)
    draft = m.TradeSetupAssessment(
        assessment_format_version=r.ASSESSMENT_FORMAT_VERSION, assessment_id="", rules_version=r.RULES_VERSION,
        policy=policy, inputs=eligibility.inputs, outcome=outcome, market_bias=eligibility.market_bias,
        execution_readiness=eligibility.execution_readiness, candidates=tuple(candidates), rejections=rejections,
        decision_trace=trace, provenance=eligibility.provenance)
    return replace(draft, assessment_id=content_id(draft.body()))


def assess(market_intelligence, options_intelligence, policy):
    """The final sealed phase10-v1 TradeSetupAssessment; raises TradeSetupInputError on invalid input."""
    result = prescreen(market_intelligence, options_intelligence, policy)
    if isinstance(result, m.TradeSetupAssessment):
        return result
    return screen(result, options_intelligence)
