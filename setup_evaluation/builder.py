"""Pure SetupEvaluation builder (Phase 11): one evaluation per (assessment, horizon), every candidate included.

``evaluate(assessment, snapshot, schedule, protocol, horizon, invalidation_checks=())``:

1. Validate every input: protocol (test only; production is not activated), assessment (``setup_candidates``),
   snapshot, schedule, and each check (each must belong to the assessment; no duplicates).
2. Check that the snapshot underlying equals the setup symbol.
3. Resolve the horizon: ``session_n`` is the n-th schedule session whose ``regular_open`` is strictly later than
   ``assessment_as_of``. The window is ``[target_close - 30 min, target_close]``.
4. The snapshot must be later than ``assessment_as_of``, inside the window (inclusive), in a regular session on the
   target date. Otherwise it is an input error, and nothing is sealed.
5. For every original candidate, in canonical order:
   - ``expired_before_target`` if the target date is after the expiration;
   - otherwise, if the contract is not in the snapshot: ``contract_absent`` for a complete chain, or
     ``observation_incomplete`` for a truncated one;
   - otherwise, the copied quote is classified (``validation.classify_quote``). Only ``observed`` carries the
     liquidation reference bid, premium change, return and dollar change. A present contract whose identity differs
     from the candidate's is an input error.
6. Count outcomes, and premium-change signs among observed candidates.
7. Relate the optional checks: ``invalidated_by_target``, ``no_invalidation_observed`` (supplied checks only) or
   ``not_evaluated``.

There is no ranking, label, average, selection or "best" contract. No clock, environment, network, files, database
or AI.
"""
from collections import Counter
from dataclasses import replace
from datetime import datetime

from setup_evaluation import model as m
from setup_evaluation import rules as r
from setup_evaluation.canonical import canonical_json, content_id
from setup_evaluation.protocol import ProtocolError, validated_protocol
from setup_evaluation.schedule import ScheduleError, resolve, validated_schedule
from setup_evaluation.validation import (SetupEvaluationInputError, _require, classify_quote, derived_values, instant,
                                         sign, validated_assessment, validated_check, validated_snapshot, window)


def _outcome(fact, contract, truncated, target_date, window_start, target_close):
    if fact["expiration"] < target_date:
        return m.CandidateOutcome(fact["contract_id"], fact["entry_reference_ask"], fact["shares_per_contract"], None,
                                  "expired_before_target", None, None, None, "not_observed", None, "not_observed")
    if contract is None:
        status = "observation_incomplete" if truncated else "contract_absent"
        return m.CandidateOutcome(fact["contract_id"], fact["entry_reference_ask"], fact["shares_per_contract"], None,
                                  status, None, None, None, "not_observed", None, "not_observed")
    identity = contract["identity"]
    _require(all(identity[k] == fact[k] for k in ("provider_symbol", "option_type", "expiration", "strike")),
             "snapshot contract identity does not match the assessment candidate")
    quote = m.ObservedQuote(*(contract["quote"][k] for k in r.QUOTE_FIELDS))
    status = classify_quote(quote.to_dict(), window_start, target_close)
    if status != r.OBSERVED:
        return m.CandidateOutcome(fact["contract_id"], fact["entry_reference_ask"], fact["shares_per_contract"], quote,
                                  status, None, None, None, "not_observed", None, "not_observed")
    later_shares = contract["terms"]["shares_per_contract"]
    terms_changed = (fact["shares_per_contract"] is not None and later_shares is not None
                     and later_shares != fact["shares_per_contract"])
    return m.CandidateOutcome(fact["contract_id"], fact["entry_reference_ask"], fact["shares_per_contract"], quote,
                              r.OBSERVED, *derived_values(fact["entry_reference_ask"], fact["shares_per_contract"],
                                                          quote.bid, terms_changed))


def summarize(outcomes):
    counts = Counter(o.outcome_status for o in outcomes)
    signs = Counter(sign(o.premium_change) for o in outcomes if o.outcome_status == r.OBSERVED)
    return m.Summary(len(outcomes), tuple(m.Count(k, n) for k, n in sorted(counts.items())),
                     m.SignCounts(signs["positive"], signs["negative"], signs["zero"]))


def relation(checks, target_close):
    """The descriptive invalidation relation for the supplied checks (supplied checks only; not monitoring)."""
    if not checks:
        return m.InvalidationRelation("not_evaluated", (), ())
    invalidating = sorted(c["invalidation_id"] for c in checks if c["result"] == "invalidated"
                          and instant(c["market_intelligence_ref"]["as_of"], "check as_of") <= target_close)
    status = "invalidated_by_target" if invalidating else "no_invalidation_observed"
    return m.InvalidationRelation(status, tuple(sorted(c["invalidation_id"] for c in checks)), tuple(invalidating))


def evaluate(assessment, snapshot, schedule, protocol, horizon, invalidation_checks=()):
    """The sealed SetupEvaluation; raises SetupEvaluationInputError on invalid input (never an outcome)."""
    try:
        protocol = validated_protocol(protocol)
    except ProtocolError as error:
        raise SetupEvaluationInputError(f"protocol is invalid: {error}") from None
    _require(horizon in r.HORIZONS, "horizon must be session_1 or session_5")
    setup, facts = validated_assessment(assessment)
    snap = validated_snapshot(snapshot)
    try:
        schedule = validated_schedule(schedule)
    except ScheduleError as error:
        raise SetupEvaluationInputError(f"schedule is invalid: {error}") from None
    checks = [validated_check(c) for c in invalidation_checks]
    _require(len({c["invalidation_id"] for c in checks}) == len(checks), "invalidation checks are duplicated")
    _require(all(c["setup_ref"]["assessment_id"] == setup["assessment_id"] for c in checks),
             "invalidation check does not belong to the assessment")

    symbol, anchor = setup["inputs"]["symbol"], instant(setup["inputs"]["assessment_as_of"], "assessment_as_of")
    _require(snap["underlying"] == symbol, "snapshot underlying does not match the setup symbol")
    try:
        session = resolve(schedule, anchor, r.HORIZONS[horizon])
    except ScheduleError as error:
        raise SetupEvaluationInputError(str(error)) from None
    target_open, target_close = datetime.fromisoformat(session["regular_open"]), datetime.fromisoformat(session["regular_close"])
    window_start = window(target_close)
    as_of = instant(snap["as_of"], "snapshot as_of")
    _require(as_of > anchor, "snapshot is not later than assessment_as_of")
    _require(window_start <= as_of <= target_close, "snapshot is outside the horizon observation window")
    _require(snap["session"]["calendar_state"] == "regular" and snap["session"]["session_date"] == session["session_date"],
             "snapshot session is not the regular target session")

    by_id = {c["identity"]["contract_id"]: c for c in snap["contracts"]}
    truncated = snap["provenance"]["truncated"]
    outcomes = tuple(_outcome(f, by_id.get(f["contract_id"]), truncated, session["session_date"], window_start,
                              target_close) for f in facts)
    rel = relation(checks, target_close)
    draft = m.SetupEvaluation(
        evaluation_format_version=r.EVALUATION_FORMAT_VERSION, evaluation_id="", rules_version=r.RULES_VERSION,
        protocol_ref=m.ProtocolRef(protocol["protocol_format_version"], protocol["protocol_id"], protocol["purpose"]),
        setup_ref=m.SetupRef(setup["assessment_id"], setup["assessment_format_version"], setup["rules_version"],
                             setup["policy"]["policy_id"], symbol, setup["market_bias"]["side"],
                             setup["inputs"]["assessment_as_of"], len(facts)),
        observation_ref=m.ObservationRef(snap["snapshot_id"], snap["snapshot_format_version"], snap["underlying"],
                                         snap["as_of"], snap["session"]["session_date"], truncated),
        schedule_ref=m.ScheduleRef(schedule["schedule_id"], schedule["schedule_format_version"]),
        horizon=m.Horizon(horizon, session["session_date"], target_open.isoformat(), target_close.isoformat(),
                          window_start.isoformat()),
        candidate_outcomes=outcomes, summary=summarize(outcomes), invalidation_relation=rel,
        decision_trace=tuple(m.TraceStep(i + 1, rule, "pass", pointers) for i, (rule, pointers) in enumerate(r.TRACE_STEPS)),
        provenance=m.Provenance(setup["assessment_id"], snap["snapshot_id"], schedule["schedule_id"],
                                protocol["protocol_id"], rel.check_ids, r.RULES_VERSION, r.POINTER_VERSION))
    return replace(draft, evaluation_id=content_id(draft.body()))


def verify_evaluation(evaluation, assessment, snapshot, schedule, protocol, invalidation_checks=()):
    """Structural validation, then a full rebuild from the sealed inputs that must match byte for byte."""
    from setup_evaluation.validation import validated_evaluation
    data = validated_evaluation(evaluation)
    expected = evaluate(assessment, snapshot, schedule, protocol, data["horizon"]["name"], invalidation_checks).to_dict()
    body = lambda d: {k: v for k, v in d.items() if k != "evaluation_id"}  # noqa: E731
    if canonical_json(body(expected)) != canonical_json(body(data)):
        differing = sorted(k for k in body(data) if expected.get(k) != data.get(k))
        raise SetupEvaluationInputError(f"evaluation does not match its inputs at {', '.join(differing)}")
    return data
