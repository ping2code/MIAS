"""Pure per-contract screening (Phase 10C): the frozen rule order, rejection reasons and candidate derivation.

``evaluate(source, policy, side, delta_out_of_bounds)`` runs every enabled rule of ``rules.SCREENING_RULES`` in
order against one contract's source facts and returns ``(reasons, checks)``:
- ``reasons``: every rejection reason that applies (never only the first), each once, in rule order;
- ``checks``: the passed ``PolicyCheck`` entries.

A contract is a candidate exactly when ``reasons`` is empty. Then ``checks`` equals ``enabled_rules(policy)``.

Rules are enabled by the policy alone:
- ``side``, ``dte_minimum``, ``dte_maximum``, ``same_day_expiry`` and ``quote_state`` are always enabled;
- the spread rules need ``max_spread_relative``;
- the delta rules and ``greeks_time_basis`` need the delta pair;
- the IV rules need ``require_iv``;
- ``day_session`` needs ``require_current_session_day``;
- ``volume_threshold`` needs ``min_volume``;
- the open-interest rules need ``min_open_interest``;
- ``multiplier_availability`` and ``premium_cap`` need ``max_premium_per_contract``.

Rules that need a fact a failed rule has already shown to be absent are not evaluated for that contract. Spread and
premium need a usable quote. A time basis needs its value present. The premium cap needs a multiplier.

Exact Decimal comparisons, inclusive bounds. There is no score, rank, label or "best" contract. Deltas, IV and
spreads are copied from the input and never recomputed. A multiplier is never assumed.
"""
from decimal import Decimal

from trade_setup import model as m
from trade_setup import rules as r
from trade_setup.canonical import canonical_decimal, format_decimal
from trade_setup.validation import TradeSetupInputError

RULES = {rule: (field, pointers) for rule, field, pointers in r.SCREENING_RULES}


def _number(value, name):
    """The Decimal of a canonical Decimal string, None for None; malformed input fails closed."""
    if value is None:
        return None
    number = canonical_decimal(value)
    if number is None:
        raise TradeSetupInputError(f"contract {name} is not a canonical Decimal string")
    return number


def source_facts(contract, v2):
    """The candidate source facts for one OptionsIntelligence contract (copied, never recomputed)."""
    c = contract
    return dict(
        contract_id=c["contract_id"], provider_symbol=c["provider_symbol"], option_type=c["option_type"],
        expiration=c["expiration"], strike=c["strike"], dte_calendar_days=c["dte_calendar_days"],
        quote_state=c["quote_state"], mid=c["mid"], spread_absolute=c["spread_absolute"],
        spread_relative=c["spread_relative"], delta=c["greeks"]["delta"], greeks_time_basis=c["greeks"]["time_basis"],
        implied_volatility=c["implied_volatility"]["value"], iv_time_basis=c["implied_volatility"]["time_basis"],
        volume_state=c["volume_state"], open_interest_state=c["open_interest_state"],
        day_session_relation=c["day"]["session_relation"],
        current_session_volume=c["current_session_volume"] if v2 else None,
        open_interest_value=c["open_interest_value"] if v2 else None,
        open_interest_time_basis=c["open_interest_time_basis"] if v2 else None,
        shares_per_contract=c["shares_per_contract"] if v2 else None)


def enabled_rules(policy):
    """The screening rules this policy enables, in the frozen order."""
    on = dict(spread_availability=policy.max_spread_relative is not None,
              delta_minimum=policy.abs_delta_min is not None, iv_availability=policy.require_iv,
              day_session=policy.require_current_session_day, volume_threshold=policy.min_volume is not None,
              open_interest_threshold=policy.min_open_interest is not None,
              multiplier_availability=policy.max_premium_per_contract is not None)
    on.update(spread_threshold=on["spread_availability"], delta_maximum=on["delta_minimum"],
              greeks_time_basis=on["delta_minimum"], iv_time_basis=on["iv_availability"],
              open_interest_time_basis=on["open_interest_threshold"], premium_cap=on["multiplier_availability"])
    return tuple(rule for rule in r.SCREENING_RULE_NAMES if on.get(rule, True))


def check(rule):
    field, pointers = RULES[rule]
    return m.PolicyCheck(rule, r.PASS, field, pointers)


def usable_quote(quote_state, policy):
    return quote_state == "complete" or (quote_state == "locked" and policy.allow_locked_quote)


def entry_reference_ask(source):
    mid, spread = _number(source["mid"], "mid"), _number(source["spread_absolute"], "spread_absolute")
    if mid is None or spread is None:
        raise TradeSetupInputError("contract with a usable quote has no mid or spread_absolute")
    return mid + spread / 2


def evaluate(source, policy, side, delta_out_of_bounds):
    """(reasons, checks) for one contract; see the module docstring."""
    enabled = set(enabled_rules(policy))
    reasons, checks = [], []

    def outcome(rule, failed, reason):
        if failed:
            if reason not in reasons:
                reasons.append(reason)
        else:
            checks.append(check(rule))

    def verified(basis):
        return policy.allow_unverified_time_basis or basis in r.VERIFIED_TIME_BASES

    dte = source["dte_calendar_days"]
    if not isinstance(dte, int) or isinstance(dte, bool):
        raise TradeSetupInputError("contract dte_calendar_days is not an integer")
    outcome("side", source["option_type"] != side, "side_mismatch")
    outcome("dte_minimum", dte < policy.min_dte, "expiration_outside_policy")
    outcome("dte_maximum", dte > policy.max_dte, "expiration_outside_policy")
    outcome("same_day_expiry", dte == 0 and not policy.allow_same_day_expiry, "same_day_expiry_excluded")
    usable = usable_quote(source["quote_state"], policy)
    outcome("quote_state", not usable, r.QUOTE_STATE_REASONS.get(source["quote_state"]))

    if usable and "spread_availability" in enabled:
        relative = _number(source["spread_relative"], "spread_relative")
        outcome("spread_availability", relative is None, "spread_relative_unavailable")
        if relative is not None:
            outcome("spread_threshold", relative > Decimal(policy.max_spread_relative), "spread_above_policy")

    if "delta_minimum" in enabled:
        delta = _number(source["delta"], "delta")
        if delta is None or delta_out_of_bounds:
            outcome("delta_minimum", True, "delta_unavailable")
        else:
            outcome("delta_minimum", abs(delta) < Decimal(policy.abs_delta_min), "delta_outside_policy")
            outcome("delta_maximum", abs(delta) > Decimal(policy.abs_delta_max), "delta_outside_policy")
            outcome("greeks_time_basis", not verified(source["greeks_time_basis"]), "time_basis_unverified")

    if "iv_availability" in enabled:
        iv = _number(source["implied_volatility"], "implied_volatility")
        outcome("iv_availability", iv is None, "iv_unavailable")
        if iv is not None:
            outcome("iv_time_basis", not verified(source["iv_time_basis"]), "time_basis_unverified")

    if "day_session" in enabled:
        outcome("day_session", source["day_session_relation"] != "current_session", "day_not_current_session")

    if "volume_threshold" in enabled:
        volume = _number(source["current_session_volume"], "current_session_volume")
        outcome("volume_threshold", volume is None or volume < policy.min_volume, "volume_below_policy")

    if "open_interest_threshold" in enabled:
        value = _number(source["open_interest_value"], "open_interest_value")
        outcome("open_interest_threshold", value is None or value < policy.min_open_interest,
                "open_interest_below_policy")
        if value is not None:
            outcome("open_interest_time_basis", not verified(source["open_interest_time_basis"]),
                    "time_basis_unverified")

    if usable and "multiplier_availability" in enabled:
        shares = _number(source["shares_per_contract"], "shares_per_contract")
        outcome("multiplier_availability", shares is None, "multiplier_unavailable")
        if shares is not None:
            outcome("premium_cap", entry_reference_ask(source) * shares > Decimal(policy.max_premium_per_contract),
                    "premium_above_policy")
    return tuple(reasons), tuple(checks)


def derived(source):
    """The candidate derived facts (the quote is usable for every candidate)."""
    ask = entry_reference_ask(source)
    shares = _number(source["shares_per_contract"], "shares_per_contract")
    return m.Derived(format_decimal(ask), format_decimal(ask * shares) if shares is not None else None,
                     "computed" if shares is not None else "multiplier_unavailable")


def canonical_order(source):
    return (source["expiration"], Decimal(source["strike"]), source["contract_id"])
