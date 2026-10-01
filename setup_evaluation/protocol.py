"""The evaluation protocol (``phase11-evaluation-protocol-v1``): a sealed, content-addressed declaration of the
frozen v1 evaluation semantics.

v1 implements exactly one semantics, so every semantic field must equal the frozen rules. A protocol cannot change
behaviour; it records and pins it. ``purpose`` is ``test`` (fixtures and replay, ``prospective_start`` null) or
``production``.

**The production protocol is not activated.** ``rules.PRODUCTION_PROTOCOL_ID`` and
``rules.PRODUCTION_PROSPECTIVE_START`` are None, so every production protocol is rejected. A separate activation
step, after Phase 10 live validation closes, will write the production protocol, pin its id and prospective start,
and record the activation commit.
"""
from dataclasses import replace
from datetime import datetime, timedelta

from setup_evaluation import model as m
from setup_evaluation import rules as r
from setup_evaluation.canonical import content_id


class ProtocolError(ValueError):
    """The protocol is invalid or not usable (the message names the problem)."""


def _require(condition, message):
    if not condition:
        raise ProtocolError(message)


def canonical_utc(value):
    """True for a canonical UTC ISO 8601 instant (offset +00:00, round-trips exactly): no timezone ambiguity."""
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.utcoffset() == timedelta(0) and parsed.isoformat() == value


def frozen_fields():
    """The v1 semantic fields, exactly as implemented."""
    return dict(evaluation_format_version=r.EVALUATION_FORMAT_VERSION, rules_version=r.RULES_VERSION,
                schedule_format_version=r.SCHEDULE_FORMAT_VERSION,
                horizons=sorted(r.HORIZONS), horizon_rule=r.HORIZON_RULE, window_seconds=r.WINDOW_SECONDS,
                window_rule=r.WINDOW_RULE, mark=r.MARK, entry=r.ENTRY, quote_requirements=list(r.QUOTE_REQUIREMENTS),
                return_places=r.RETURN_PLACES, return_rounding=r.RETURN_ROUNDING,
                outcome_statuses=list(r.OUTCOME_STATUSES), return_statuses=list(r.RETURN_STATUSES),
                dollar_statuses=list(r.DOLLAR_STATUSES), relation_statuses=list(r.RELATION_STATUSES),
                candidate_inclusion=r.CANDIDATE_INCLUSION, invalidation_rule=r.INVALIDATION_RULE,
                missing_contract_rule=r.MISSING_CONTRACT_RULE, multiplier_rule=r.MULTIPLIER_RULE,
                exclusions=list(r.EXCLUSIONS))


def make_test_protocol():
    """A sealed test/replay protocol (purpose ``test``, no prospective start). Never a production protocol."""
    draft = m.Protocol(protocol_format_version=r.PROTOCOL_FORMAT_VERSION, protocol_id="", purpose="test",
                       prospective_start=None, **{k: tuple(v) if isinstance(v, list) else v
                                                  for k, v in frozen_fields().items()})
    return replace(draft, protocol_id=content_id(draft.body()))


def validated_protocol(protocol):
    """The plain protocol dict, validated: exact keys, version, id, frozen semantics, and activation rules."""
    data = protocol.to_dict() if isinstance(protocol, m.Protocol) else protocol
    _require(isinstance(data, dict) and set(data) == set(m.PROTOCOL_FIELDS),
             "protocol must have exactly the phase11-evaluation-protocol-v1 keys")
    _require(data["protocol_format_version"] == r.PROTOCOL_FORMAT_VERSION, "unsupported protocol format version")
    _require(isinstance(data["protocol_id"], str)
             and content_id({k: v for k, v in data.items() if k != "protocol_id"}) == data["protocol_id"],
             "protocol_id does not match the protocol (tampered or corrupt)")
    _require(all(data[k] == v for k, v in frozen_fields().items()),
             "protocol semantics do not match the frozen phase11-rules-v1 semantics")
    _require(data["purpose"] in r.PROTOCOL_PURPOSES, "protocol purpose is not supported")
    if data["purpose"] == "test":
        _require(data["prospective_start"] is None, "a test protocol has no prospective_start")
    else:
        _require(r.PRODUCTION_PROTOCOL_ID is not None and r.PRODUCTION_PROSPECTIVE_START is not None,
                 "production evaluation protocol is not activated")
        _require(canonical_utc(data["prospective_start"]), "production prospective_start must be canonical UTC")
        _require(data["protocol_id"] == r.PRODUCTION_PROTOCOL_ID
                 and data["prospective_start"] == r.PRODUCTION_PROSPECTIVE_START,
                 "protocol is not the pinned production protocol")
    return data
