"""Fail-closed validation for Setup Evaluation (pure; standard library and setup_evaluation only). Nothing is ever
repaired.

Upstream sealed inputs are validated **locally and structurally**: exact keys, versions and a recomputed content id,
plus the fields evaluation reads. No upstream package is imported.
- ``validated_assessment``: a ``setup_candidates`` TradeSetupAssessment, returning its candidate facts in canonical
  order.
- ``validated_snapshot``: an OptionsSnapshot.
- ``validated_check``: a Phase 10D InvalidationCheck.

``classify_quote`` and ``derived_values`` are the shared, pure evaluation rules. The builder uses them to evaluate,
and ``validated_evaluation`` uses them to re-derive every outcome from the object's own copied facts.
"""
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from setup_evaluation import model as m
from setup_evaluation import rules as r
from setup_evaluation.canonical import canonical_decimal, content_id, format_decimal


class SetupEvaluationInputError(ValueError):
    """Invalid input or evaluation (the message names the problem); never an outcome."""


def _require(condition, message):
    if not condition:
        raise SetupEvaluationInputError(message)


def _plain(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


def instant(value, name):
    _require(isinstance(value, str), f"{name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise SetupEvaluationInputError(f"{name} must be an ISO 8601 string") from None
    _require(parsed.utcoffset() is not None, f"{name} must be timezone-aware")
    return parsed


def _sealed(data, keys, id_field, name):
    _require(isinstance(data, dict) and set(data) == set(keys), f"{name} must have exactly its contract keys")
    _require(isinstance(data[id_field], str) and r.CONTENT_ID.fullmatch(data[id_field])
             and content_id({k: v for k, v in data.items() if k != id_field}) == data[id_field],
             f"{name} id does not match its body (tampered or corrupt)")


def _decimal(value, name, *, nullable=False, positive=False):
    if value is None and nullable:
        return None
    number = canonical_decimal(value)
    _require(number is not None and (number > 0 if positive else number >= 0), f"{name} is malformed")
    return number


def canonical_order(expiration, strike, contract_id):
    return (expiration, Decimal(strike), contract_id)


# --- upstream inputs ----------------------------------------------------------------------------------------------

def validated_assessment(assessment):
    """(assessment dict, candidate facts in canonical order) for a sealed setup_candidates assessment."""
    data = _plain(assessment)
    _sealed(data, r.ASSESSMENT_TOP_LEVEL, "assessment_id", "assessment")
    _require(data["assessment_format_version"] == r.ASSESSMENT_FORMAT_VERSION
             and data["rules_version"] == r.ASSESSMENT_RULES_VERSION, "unsupported assessment version")
    _require(isinstance(data["outcome"], dict) and data["outcome"].get("status") == r.SETUP_CANDIDATES,
             "evaluation requires a setup_candidates assessment")
    _require(isinstance(data["market_bias"], dict) and data["market_bias"].get("side") in r.OPTION_SIDES,
             "assessment market_bias is malformed")
    inputs = data["inputs"]
    _require(isinstance(inputs, dict) and isinstance(inputs.get("symbol"), str) and r.SYMBOL.fullmatch(inputs["symbol"]),
             "assessment inputs are malformed")
    instant(inputs.get("assessment_as_of"), "assessment_as_of")
    _require(isinstance(data["policy"], dict) and isinstance(data["policy"].get("policy_id"), str)
             and r.CONTENT_ID.fullmatch(data["policy"]["policy_id"]), "assessment policy is malformed")
    candidates = data["candidates"]
    _require(isinstance(candidates, list) and candidates, "assessment candidates are malformed")
    facts = []
    for c in candidates:
        source, derived = c.get("source") if isinstance(c, dict) else None, c.get("derived") if isinstance(c, dict) else None
        _require(isinstance(source, dict) and isinstance(derived, dict)
                 and all(isinstance(source.get(k), str) for k in ("contract_id", "provider_symbol", "expiration", "strike"))
                 and source.get("option_type") == data["market_bias"]["side"] and r.DATE.fullmatch(source["expiration"])
                 and canonical_decimal(source["strike"]) is not None, "assessment candidate is malformed")
        _decimal(derived.get("entry_reference_ask"), "assessment candidate entry_reference_ask")
        _decimal(source.get("shares_per_contract"), "assessment candidate shares_per_contract", nullable=True, positive=True)
        facts.append(dict(contract_id=source["contract_id"], provider_symbol=source["provider_symbol"],
                          option_type=source["option_type"], expiration=source["expiration"], strike=source["strike"],
                          entry_reference_ask=derived["entry_reference_ask"],
                          shares_per_contract=source.get("shares_per_contract")))
    order = [canonical_order(f["expiration"], f["strike"], f["contract_id"]) for f in facts]
    _require(order == sorted(order) and len({f["contract_id"] for f in facts}) == len(facts),
             "assessment candidates are not unique and in canonical order")
    return data, facts


def validated_snapshot(snapshot):
    """The OptionsSnapshot dict, structurally validated (fields evaluation reads), with its id recomputed."""
    data = _plain(snapshot)
    _sealed(data, r.SNAPSHOT_TOP_LEVEL, "snapshot_id", "snapshot")
    _require(data["snapshot_format_version"] == r.SNAPSHOT_FORMAT_VERSION, "unsupported snapshot format version")
    _require(isinstance(data["underlying"], str) and r.SYMBOL.fullmatch(data["underlying"]), "snapshot underlying is malformed")
    instant(data["as_of"], "snapshot as_of")
    session = data["session"]
    _require(isinstance(session, dict) and set(session) == {"session_date", "calendar_state"}
             and isinstance(session["session_date"], str) and r.DATE.fullmatch(session["session_date"])
             and session["calendar_state"] in r.CALENDAR_STATES + (None,), "snapshot session is malformed")
    _require(isinstance(data["provenance"], dict) and isinstance(data["provenance"].get("truncated"), bool),
             "snapshot provenance is malformed")
    _require(isinstance(data["contracts"], list), "snapshot contracts must be a list")
    ids = set()
    for c in data["contracts"]:
        _require(isinstance(c, dict) and isinstance(c.get("identity"), dict) and isinstance(c.get("terms"), dict)
                 and isinstance(c.get("quote"), dict), "snapshot contract is malformed")
        identity, quote = c["identity"], c["quote"]
        _require(all(isinstance(identity.get(k), str) for k in ("contract_id", "provider_symbol", "expiration", "strike"))
                 and identity.get("option_type") in r.OPTION_SIDES, "snapshot contract identity is malformed")
        _decimal(c["terms"].get("shares_per_contract"), "snapshot shares_per_contract", nullable=True, positive=True)
        _require(set(r.QUOTE_FIELDS) <= set(quote) and quote["status"] in r.FACT_STATUSES
                 and quote["time_basis"] in r.TIME_BASES, "snapshot quote is malformed")
        for side in ("bid", "ask"):
            _decimal(quote[side], f"snapshot quote {side}", nullable=True)
        if quote["observed_at"] is not None:
            instant(quote["observed_at"], "snapshot quote observed_at")
        ids.add(identity["contract_id"])
    _require(len(ids) == len(data["contracts"]), "snapshot contract ids are not unique")
    return data


def validated_check(check):
    """A Phase 10D InvalidationCheck dict, structurally validated, with its id recomputed."""
    data = _plain(check)
    _sealed(data, r.INVALIDATION_TOP_LEVEL, "invalidation_id", "invalidation check")
    _require(data["invalidation_format_version"] == r.INVALIDATION_FORMAT_VERSION
             and data["rules_version"] == r.INVALIDATION_RULES_VERSION, "unsupported invalidation check version")
    _require(isinstance(data["setup_ref"], dict) and isinstance(data["setup_ref"].get("assessment_id"), str)
             and data["result"] in r.INVALIDATION_RESULTS and isinstance(data["market_intelligence_ref"], dict),
             "invalidation check is malformed")
    instant(data["market_intelligence_ref"].get("as_of"), "invalidation check market intelligence as_of")
    return data


# --- shared evaluation rules --------------------------------------------------------------------------------------

def classify_quote(quote, window_start, target_close):
    """The outcome status for a present contract's copied quote, by frozen precedence:
    unavailable/missing, after-cutoff status, timing, two-sidedness, crossing, then staleness."""
    if quote["status"] in ("unavailable", "missing"):
        return "quote_unavailable"
    if quote["status"] == "excluded_after_as_of":
        return "quote_after_cutoff"
    if quote["time_basis"] != "observed_at" or quote["observed_at"] is None:
        return "quote_timing_unverified"
    if quote["bid"] is None or quote["ask"] is None:
        return "quote_not_two_sided"
    if Decimal(quote["bid"]) > Decimal(quote["ask"]):
        return "quote_crossed"
    observed = instant(quote["observed_at"], "quote observed_at")
    if observed < window_start:
        return "quote_stale"
    if observed > target_close:
        return "quote_after_cutoff"
    return r.OBSERVED


def premium_return(change, entry):
    with localcontext() as context:
        context.prec = 60
        value = (change / entry).quantize(Decimal(1).scaleb(-r.RETURN_PLACES), rounding=ROUND_HALF_EVEN)
    text = format(value, "f")
    return text[1:] if text.startswith("-") and Decimal(text) == 0 else text


def derived_values(entry_text, shares_text, bid_text, terms_changed):
    """(liquidation_reference_bid, premium_change, premium_return, return_status, dollar_change, dollar_status) for an
    observed candidate. Exact Decimals; the return is quantized to 8 places with ROUND_HALF_EVEN."""
    entry, bid = Decimal(entry_text), Decimal(bid_text)
    change = bid - entry
    if entry == 0:
        ret, return_status = None, "entry_reference_zero"
    else:
        ret, return_status = premium_return(change, entry), "computed"
    if shares_text is None:
        dollar, dollar_status = None, "multiplier_unavailable"
    elif terms_changed:
        dollar, dollar_status = None, "contract_terms_changed"
    else:
        dollar, dollar_status = format_decimal(change * Decimal(shares_text)), "computed"
    return bid_text, format_decimal(change), ret, return_status, dollar, dollar_status


def window(target_close):
    return target_close - timedelta(seconds=r.WINDOW_SECONDS)


def sign(change_text):
    change = Decimal(change_text)
    return "positive" if change > 0 else "negative" if change < 0 else "zero"


# --- the evaluation itself ----------------------------------------------------------------------------------------

def _validated_outcome(o, truncated, target_date, window_start, target_close):
    _require(isinstance(o, dict) and set(o) == set(m.OUTCOME_FIELDS) and isinstance(o["contract_id"], str),
             "evaluation candidate outcome is malformed")
    _decimal(o["entry_reference_ask"], "evaluation entry_reference_ask")
    _decimal(o["shares_per_contract"], "evaluation shares_per_contract", nullable=True, positive=True)
    _require(o["outcome_status"] in r.OUTCOME_STATUSES and o["return_status"] in r.RETURN_STATUSES
             and o["dollar_status"] in r.DOLLAR_STATUSES, "evaluation outcome status is not supported")
    quote = o["observed_quote"]
    if quote is None:
        _require(o["outcome_status"] in ("expired_before_target", "contract_absent", "observation_incomplete"),
                 "evaluation outcome status is inconsistent with its observed quote")
        _require(o["outcome_status"] == "expired_before_target"
                 or o["outcome_status"] == ("observation_incomplete" if truncated else "contract_absent"),
                 "evaluation missing-contract status is inconsistent with the snapshot completeness")
    else:
        _require(isinstance(quote, dict) and set(quote) == set(r.QUOTE_FIELDS) and quote["status"] in r.FACT_STATUSES
                 and quote["time_basis"] in r.TIME_BASES, "evaluation observed quote is malformed")
        for side in ("bid", "ask"):
            _decimal(quote[side], f"evaluation quote {side}", nullable=True)
        _require(classify_quote(quote, window_start, target_close) == o["outcome_status"],
                 "evaluation outcome status is inconsistent with its observed quote")
    if o["outcome_status"] != r.OBSERVED:
        _require((o["liquidation_reference_bid"], o["premium_change"], o["premium_return"], o["return_status"],
                  o["dollar_change_per_contract"], o["dollar_status"]) == (None, None, None, "not_observed", None,
                                                                          "not_observed"),
                 "evaluation carries outcome values for a candidate that was not observed")
        return
    expected = derived_values(o["entry_reference_ask"], o["shares_per_contract"], quote["bid"],
                              o["dollar_status"] == "contract_terms_changed")
    actual = (o["liquidation_reference_bid"], o["premium_change"], o["premium_return"], o["return_status"],
              o["dollar_change_per_contract"], o["dollar_status"])
    _require(o["dollar_status"] != "contract_terms_changed" or o["shares_per_contract"] is not None,
             "evaluation dollar status is inconsistent")
    _require(actual == expected, "evaluation outcome values do not re-derive from the copied facts")


def validated_evaluation(evaluation):
    """Structural, standalone validation of a sealed SetupEvaluation; returns the plain dict. Never repairs.

    Within the object: versions, ids, refs, symbol, time ordering, the window, the target-session match, canonical
    unique candidates, every outcome's status and values re-derived from its own copied facts, the summary, the
    invalidation relation shape, the trace and provenance. Horizon resolution against the schedule, the candidate
    set against the assessment and the copied facts against the snapshot need the inputs (``verify_evaluation``)."""
    data = _plain(evaluation)
    _sealed(data, m.EVALUATION_FIELDS, "evaluation_id", "evaluation")
    _require(data["evaluation_format_version"] == r.EVALUATION_FORMAT_VERSION and data["rules_version"] == r.RULES_VERSION,
             "unsupported evaluation version")
    protocol, setup, obs, sched, horizon = (data[k] for k in ("protocol_ref", "setup_ref", "observation_ref",
                                                               "schedule_ref", "horizon"))
    _require(isinstance(protocol, dict) and set(protocol) == {"protocol_format_version", "protocol_id", "purpose"}
             and protocol["protocol_format_version"] == r.PROTOCOL_FORMAT_VERSION
             and protocol["purpose"] in r.PROTOCOL_PURPOSES and isinstance(protocol["protocol_id"], str)
             and r.CONTENT_ID.fullmatch(protocol["protocol_id"]), "evaluation protocol_ref is malformed")
    _require(protocol["purpose"] == "test" or (r.PRODUCTION_PROTOCOL_ID is not None
                                              and protocol["protocol_id"] == r.PRODUCTION_PROTOCOL_ID),
             "evaluation protocol is not an activated production protocol")
    _require(isinstance(setup, dict) and set(setup) == set(m.SetupRef.__dataclass_fields__)
             and setup["assessment_format_version"] == r.ASSESSMENT_FORMAT_VERSION
             and setup["assessment_rules_version"] == r.ASSESSMENT_RULES_VERSION and setup["side"] in r.OPTION_SIDES
             and isinstance(setup["candidate_count"], int) and setup["candidate_count"] > 0,
             "evaluation setup_ref is malformed")
    _require(isinstance(obs, dict) and set(obs) == set(m.ObservationRef.__dataclass_fields__)
             and obs["snapshot_format_version"] == r.SNAPSHOT_FORMAT_VERSION and isinstance(obs["truncated"], bool),
             "evaluation observation_ref is malformed")
    _require(isinstance(sched, dict) and set(sched) == {"schedule_id", "schedule_format_version"}
             and sched["schedule_format_version"] == r.SCHEDULE_FORMAT_VERSION, "evaluation schedule_ref is malformed")
    _require(obs["underlying"] == setup["symbol"], "evaluation symbols are inconsistent")
    _require(isinstance(horizon, dict) and set(horizon) == set(m.Horizon.__dataclass_fields__)
             and horizon["name"] in r.HORIZONS and horizon["target_session_date"] == obs["session_date"],
             "evaluation horizon is malformed")
    anchor = instant(setup["assessment_as_of"], "assessment_as_of")
    _require(protocol["purpose"] != "production"
             or anchor >= instant(r.PRODUCTION_PROSPECTIVE_START, "production prospective_start"),
             "evaluation assessment is before the production prospective_start")
    target_open, target_close = instant(horizon["target_open"], "target_open"), instant(horizon["target_close"], "target_close")
    window_start, as_of = instant(horizon["window_start"], "window_start"), instant(obs["as_of"], "observation as_of")
    _require(anchor < target_open < target_close and window_start == window(target_close),
             "evaluation horizon is inconsistent")
    _require(anchor < as_of and window_start <= as_of <= target_close,
             "evaluation observation is outside the horizon window")
    outcomes = data["candidate_outcomes"]
    _require(isinstance(outcomes, list) and len(outcomes) == setup["candidate_count"],
             "evaluation candidate_outcomes do not match the setup candidate count")
    for o in outcomes:
        _validated_outcome(o, obs["truncated"], horizon["target_session_date"], window_start, target_close)
    _require(len({o["contract_id"] for o in outcomes}) == len(outcomes), "evaluation candidates are not unique")
    counts = {}
    for o in outcomes:
        counts[o["outcome_status"]] = counts.get(o["outcome_status"], 0) + 1
    signs = dict(positive=0, negative=0, zero=0)
    for o in outcomes:
        if o["outcome_status"] == r.OBSERVED:
            signs[sign(o["premium_change"])] += 1
    _require(data["summary"] == dict(candidate_count=len(outcomes),
                                     outcome_status_counts=[dict(key=k, count=n) for k, n in sorted(counts.items())],
                                     premium_change_sign_counts=signs), "evaluation summary does not re-derive")
    rel = data["invalidation_relation"]
    _require(isinstance(rel, dict) and set(rel) == {"status", "check_ids", "invalidating_check_ids"}
             and rel["status"] in r.RELATION_STATUSES and rel["check_ids"] == sorted(set(rel["check_ids"]))
             and rel["invalidating_check_ids"] == sorted(set(rel["invalidating_check_ids"]))
             and set(rel["invalidating_check_ids"]) <= set(rel["check_ids"])
             and rel["status"] == ("not_evaluated" if not rel["check_ids"] else "invalidated_by_target"
                                   if rel["invalidating_check_ids"] else "no_invalidation_observed"),
             "evaluation invalidation_relation is inconsistent")
    _require(data["decision_trace"] == [dict(step=i + 1, rule=rule, result="pass", pointers=list(pointers))
                                        for i, (rule, pointers) in enumerate(r.TRACE_STEPS)],
             "evaluation decision_trace is inconsistent")
    _require(data["provenance"] == dict(assessment_id=setup["assessment_id"], snapshot_id=obs["snapshot_id"],
                                        schedule_id=sched["schedule_id"], protocol_id=protocol["protocol_id"],
                                        invalidation_check_ids=rel["check_ids"], rules_version=r.RULES_VERSION,
                                        pointer_version=r.POINTER_VERSION), "evaluation provenance is inconsistent")
    return data
