"""Pure Phase 12 alert eligibility (``phase12-rules-v1``): explicit sealed inputs in, at most one AlertEvent out.

Each function returns the sealed ``AlertEvent`` when its closed rule fires, ``None`` when the valid input doesn't
qualify, and raises ``AlertInputError`` for invalid input:

- ``setup_available(assessment)``: a TradeSetupAssessment whose outcome is ``setup_candidates``. One
  assessment-level alert, whatever the candidate count; no candidate is selected, ordered or promoted. ``no_setup``
  does not fire.
- ``setup_invalidated(check)``: a Phase 10D InvalidationCheck whose result is ``invalidated``. ``holds`` and
  ``not_evaluable`` do not fire. Suppressing repeats or terminal state is Phase 12C.
- ``market_pattern_changed(previous, current)``: an explicit MarketIntelligence pair (Phase 8 pair rules: distinct,
  same symbol, previous ``as_of`` strictly earlier). It fires only for the Phase 8 ``pattern_changed`` semantics:
  comparable syntheses (equal format and rules versions) whose ``timeframe_structure.pattern`` differs. Other Phase 8
  transitions never fire, and a non-comparable pair has no transitions (as in Phase 8).

All state comes from the explicit inputs. No clock, environment, network, files, database, Redis or AI. The event's
``as_of`` is the current source's ``as_of``.
"""
from dataclasses import replace
from datetime import datetime

from alert_engine import model as m
from alert_engine import rules as r
from alert_engine.canonical import content_id
from alert_engine.validation import validated_assessment, validated_check, validated_pair


def _seal(code, subject, transition, refs, facts):
    shape = r.SHAPES[code]
    refs = tuple(sorted(refs, key=lambda ref: ref.role))
    draft = m.AlertEvent(
        alert_format_version=r.ALERT_FORMAT_VERSION, alert_id="", rules_version=r.RULES_VERSION, alert_code=code,
        subject=subject, transition=transition, source_refs=refs,
        as_of=next(ref.as_of for ref in refs if ref.role == "current"), facts=dict(sorted(facts.items())),
        decision_trace=tuple(m.TraceStep(i + 1, rule, "pass", pointers) for i, (rule, pointers) in enumerate(shape["trace"])),
        provenance=m.Provenance(tuple(sorted({ref.id for ref in refs})), r.RULES_VERSION, r.POINTER_VERSION))
    return replace(draft, alert_id=content_id(draft.body()))


def setup_available(assessment):
    data = validated_assessment(assessment)
    if data["outcome"]["status"] != r.SETUP_CANDIDATES:
        return None
    inputs, bias = data["inputs"], data["market_bias"]
    ref = m.SourceRef("current", r.TRADE_SETUP_ASSESSMENT, data["assessment_id"], data["assessment_format_version"],
                      data["rules_version"], inputs["assessment_as_of"])
    facts = dict(symbol=inputs["symbol"], candidate_count=len(data["candidates"]), eligible_side=bias["side"],
                 market_bias=bias["state"])
    return _seal(r.SETUP_AVAILABLE, m.Subject(r.SETUP, inputs["symbol"], data["assessment_id"]), None, (ref,), facts)


def setup_invalidated(check):
    data = validated_check(check)
    if data["result"] != r.INVALIDATED:
        return None
    setup, mi_ref, observed = data["setup_ref"], data["market_intelligence_ref"], data["observed_market_state"]
    refs = (m.SourceRef("current", r.INVALIDATION_CHECK, data["invalidation_id"], data["invalidation_format_version"],
                        data["rules_version"], mi_ref["as_of"]),
            m.SourceRef("setup", r.TRADE_SETUP_ASSESSMENT, setup["assessment_id"], setup["assessment_format_version"],
                        setup["assessment_rules_version"], setup["assessment_as_of"]))
    facts = dict(symbol=data["symbol"], side=setup["side"], required_pattern=data["required_market_state"]["required_pattern"],
                 observed_pattern=observed["pattern"], observed_technical_status=observed["technical_status"])
    return _seal(r.SETUP_INVALIDATED, m.Subject(r.SETUP, data["symbol"], setup["assessment_id"]), None, refs, facts)


def market_pattern_changed(previous, current):
    previous, current = validated_pair(previous, current)
    p, c = previous["synthesis_ref"], current["synthesis_ref"]
    comparable = (p["synthesis_format_version"], p["synthesis_rules_version"]) == (
        c["synthesis_format_version"], c["synthesis_rules_version"])
    earlier, later = previous["timeframe_structure"]["pattern"], current["timeframe_structure"]["pattern"]
    if not comparable or earlier == later:
        return None
    refs = tuple(m.SourceRef(role, r.MARKET_INTELLIGENCE, x["intelligence_id"], x["intelligence_format_version"],
                             x["rules_version"], x["synthesis_ref"]["as_of"])
                 for role, x in (("previous", previous), ("current", current)))
    elapsed = int((datetime.fromisoformat(c["as_of"]) - datetime.fromisoformat(p["as_of"])).total_seconds())
    return _seal(r.MARKET_PATTERN_CHANGED, m.Subject(r.SYMBOL, c["symbol"], None), m.Transition(earlier, later), refs,
                 dict(symbol=c["symbol"], elapsed_seconds=elapsed))
