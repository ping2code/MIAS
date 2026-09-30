"""Frozen OptionsIntelligence rules (``phase9-rules-v1``): versions, closed vocabularies and pure functions.

No thresholds, weights, scores or rankings. Every derived number is exact, or quantized to ``RATIO_PLACES`` with
round-half-even, and rendered as a canonical Decimal string.
"""
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from market_data.models import format_decimal

OPTIONS_INTELLIGENCE_FORMAT_VERSION = "phase9-v1"
RULES_VERSION = "phase9-rules-v1"
# Phase 10 compatibility amendment (additive): phase9-v2 contracts also carry numeric current-session volume, open
# interest (value and time basis) and shares per contract. phase9-v1 is unchanged and stays the default.
OPTIONS_INTELLIGENCE_FORMAT_V2 = "phase9-v2"
RULES_VERSION_V2 = "phase9-rules-v2"
FORMATS = {OPTIONS_INTELLIGENCE_FORMAT_VERSION: RULES_VERSION, OPTIONS_INTELLIGENCE_FORMAT_V2: RULES_VERSION_V2}
V2_CONTRACT_FIELDS = ("current_session_volume", "open_interest_value", "open_interest_time_basis",
                      "shares_per_contract")
POINTER_VERSION = "phase9-pointer-v1"
SUPPORTED_SNAPSHOT_FORMATS = frozenset({"phase9-snapshot-v1"})
# MarketIntelligence versions this rule set can reference (frozen local copies; a test pins them to
# market_intelligence.rules, so this package never imports market_intelligence or the evidence layers).
SUPPORTED_MARKET_INTELLIGENCE_FORMATS = frozenset({"phase8-v1"})
SUPPORTED_MARKET_INTELLIGENCE_RULES = frozenset({"phase8-rules-v1"})

RATIO_PLACES = Decimal("1E-10")

STRIKE_RELATIONS = ("below", "equal", "above", "unavailable")
QUOTE_STATES = ("complete", "locked", "crossed", "bid_missing", "ask_missing", "both_missing", "unavailable",
                "excluded_after_as_of")
ACTIVITY_STATES = ("positive", "zero", "missing", "unavailable", "excluded_after_as_of")
SESSION_RELATIONS = ("current_session", "previous_session", "older_session", "unavailable")
GREEK_FIELDS = ("delta", "gamma", "theta", "vega", "rho")
ZERO_MID, ZERO_DENOMINATOR, TOTAL_UNAVAILABLE = "zero_mid", "zero_denominator", "total_unavailable"
QUOTE_NOT_TWO_SIDED = "quote_not_two_sided"
VOLUME_BASIS = "current_session_day_records"

# Attention: closed, unordered categories and codes. No severity, priority or score.
GAP, DATA_QUALITY, PRESENCE = "gap", "data_quality", "presence"
ATTENTION_CATEGORIES = (DATA_QUALITY, GAP, PRESENCE)
ATTENTION_CODES = {
    "quote_incomplete": GAP,
    "crossed_quote": DATA_QUALITY,
    "locked_quote": DATA_QUALITY,
    "iv_unavailable": GAP,
    "greeks_unavailable": GAP,
    "greeks_out_of_bounds": DATA_QUALITY,
    "no_volume": PRESENCE,
    "no_open_interest": PRESENCE,
    "day_not_current_session": GAP,
    "underlying_price_unavailable": GAP,
    "facts_excluded_after_as_of": DATA_QUALITY,
    "records_excluded": DATA_QUALITY,
    "time_basis_unverified": GAP,
}
CHAIN_SCOPE, CONTRACT_SCOPE = "chain", "contracts"


def text(value):
    return None if value is None else format_decimal(value)


def ratio(numerator, denominator):
    """numerator / denominator quantized to RATIO_PLACES (half-even); the caller handles a zero denominator."""
    with localcontext() as context:
        context.prec = 60
        return (numerator / denominator).quantize(RATIO_PLACES, rounding=ROUND_HALF_EVEN)


def median(values):
    """Sorted numeric values: the middle one, or the exact mean of the two middle ones."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    with localcontext() as context:
        context.prec = 60
        return (ordered[middle - 1] + ordered[middle]) / 2


def quote_state(status, bid, ask):
    if status in ("unavailable", "excluded_after_as_of"):
        return status
    if bid is None and ask is None:
        return "both_missing"
    if bid is None:
        return "bid_missing"
    if ask is None:
        return "ask_missing"
    if ask < bid:
        return "crossed"
    return "locked" if ask == bid else "complete"


def activity_state(status, value):
    if status in ("unavailable", "excluded_after_as_of"):
        return status
    if value is None:
        return "missing"
    return "positive" if value > 0 else "zero"


def strike_relation(strike, price):
    if price is None:
        return "unavailable"
    return "below" if strike < price else "above" if strike > price else "equal"


def greeks_out_of_bounds(option_type, values):
    """Greek names violating mathematical sanity predicates (call delta in [0,1], put delta in [-1,0], gamma >= 0,
    vega >= 0). Theta and rho are unconstrained. Descriptive only."""
    out = []
    delta = values.get("delta")
    if delta is not None and not ((0 <= delta <= 1) if option_type == "call" else (-1 <= delta <= 0)):
        out.append("delta")
    for name in ("gamma", "vega"):
        if values.get(name) is not None and values[name] < 0:
            out.append(name)
    return out
