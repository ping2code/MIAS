"""The required, explicit, immutable, content-addressed Trade Setup policy (``phase10-policy-v1``).

Every key is required and there are no engine defaults. ``null`` (``None``) explicitly switches off an optional
numeric rule. Decimal thresholds are canonical Decimal strings (never floats).

``make_policy(**fields)`` validates the fields and seals them (adds ``policy_format_version`` and ``policy_id``);
``validated_policy(policy)`` checks a sealed policy, including that ``policy_id`` recomputes.
``policy_id = "sha256:" + SHA-256(canonical policy without policy_id)``.
"""
from trade_setup import model as m
from trade_setup import rules as r
from trade_setup.canonical import canonical_decimal, content_id


class PolicyError(ValueError):
    """The policy is not a valid phase10-policy-v1 policy (the message names the problem)."""


def _require(condition, message):
    if not condition:
        raise PolicyError(message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_fields(data):
    sides = data["allowed_sides"]
    _require(isinstance(sides, list) and sides and sides == sorted(set(sides)) and set(sides) <= set(r.OPTION_SIDES),
             "allowed_sides must be a non-empty sorted subset of call, put")
    _require(_is_int(data["min_dte"]) and _is_int(data["max_dte"]) and 0 <= data["min_dte"] <= data["max_dte"],
             "min_dte and max_dte must be integers with 0 <= min_dte <= max_dte")
    for key in ("allow_same_day_expiry", "allow_locked_quote", "require_iv", "allow_unverified_time_basis",
                "require_current_session_day", "require_complete_chain", "block_on_market_context_opposition",
                "block_on_market_context_not_current"):
        _require(isinstance(data[key], bool), f"{key} must be a boolean")
    if data["max_spread_relative"] is not None:
        value = canonical_decimal(data["max_spread_relative"])
        _require(value is not None and value >= 0, "max_spread_relative must be null or a non-negative Decimal string")
    low, high = data["abs_delta_min"], data["abs_delta_max"]
    _require((low is None) == (high is None), "abs_delta_min and abs_delta_max must both be null or both be set")
    if low is not None:
        low_d, high_d = canonical_decimal(low), canonical_decimal(high)
        _require(low_d is not None and high_d is not None and 0 <= low_d <= high_d <= 1,
                 "abs_delta_min/abs_delta_max must be Decimal strings with 0 <= min <= max <= 1")
    for key in ("min_volume", "min_open_interest"):
        _require(data[key] is None or (_is_int(data[key]) and data[key] >= 0),
                 f"{key} must be null or a non-negative integer")
    if data["max_premium_per_contract"] is not None:
        value = canonical_decimal(data["max_premium_per_contract"])
        _require(value is not None and value > 0, "max_premium_per_contract must be null or a positive Decimal string")
    _require(_is_int(data["max_input_gap_seconds"]) and data["max_input_gap_seconds"] >= 0,
             "max_input_gap_seconds must be a non-negative integer")


def make_policy(**fields):
    """Seal an explicit policy. Every rule field is required; nothing is defaulted."""
    expected = set(m.POLICY_FIELDS) - {"policy_format_version", "policy_id"}
    _require(set(fields) == expected, "policy must specify exactly every phase10-policy-v1 rule field")
    data = dict(fields, allowed_sides=list(fields["allowed_sides"]) if isinstance(fields["allowed_sides"], (list, tuple))
                else fields["allowed_sides"], policy_format_version=r.POLICY_FORMAT_VERSION)
    _check_fields(data)
    data["policy_id"] = content_id(data)
    return validated_policy(data)


def validated_policy(policy):
    """A sealed policy (dict or Policy) as a Policy model, after every check."""
    data = policy.to_dict() if isinstance(policy, m.Policy) else policy
    _require(isinstance(data, dict) and set(data) == set(m.POLICY_FIELDS),
             "policy must have exactly the phase10-policy-v1 keys")
    _require(data["policy_format_version"] == r.POLICY_FORMAT_VERSION, "unsupported policy format version")
    _require(isinstance(data["policy_id"], str) and r.CONTENT_ID.fullmatch(data["policy_id"]), "policy_id is malformed")
    _check_fields(data)
    _require(content_id({k: v for k, v in data.items() if k != "policy_id"}) == data["policy_id"],
             "policy_id does not match the policy (tampered or corrupt)")
    return m.Policy(**{k: tuple(v) if k == "allowed_sides" else v for k, v in data.items()})
