"""Frozen, versioned synthesis rules (``phase7g-rules-v1``): pure functions and constant tables only.

Everything here is descriptive. There is no weighting, no scoring, no higher-timeframe "winner" and no confidence
calculation. Nothing is tuned on outcome data.

- ``STATE_DIRECTION`` is a frozen local copy of the technical engine's direction contract. A test proves it equals
  ``technical.signals.DIRECTION``, so this module never imports ``technical``.
- The vocabularies mirror the persisted technical schema. Tests prove they equal ``persistence.models``.
"""
from decimal import Decimal, InvalidOperation

SYNTHESIS_FORMAT_VERSION = "phase7g-v1"
RULES_VERSION = "phase7g-rules-v1"
PACKET_FORMAT_VERSION = "phase7c-v1"
MARKET_CONTEXT_FORMAT_VERSION = "phase7b-v1"
INTERVALS = ("1d", "1h", "5m")
PAIRS = (("1d", "1h"), ("1h", "5m"), ("1d", "5m"))
BASES = ("prev_close", "open")
SELF_REFERENCE = "self"

TECHNICAL_STATES = ("bullish_setup", "bearish_setup", "bullish_momentum", "bearish_momentum", "breakout_watch",
                    "breakdown_watch", "range", "mixed", "insufficient_data")
TECHNICAL_TRENDS = ("bullish", "bearish", "range", "mixed", "insufficient_data")
BREAKOUT_STATES = ("breakout", "breakdown", "failed_breakout", "failed_breakdown", "none")
TECHNICAL_CONFIDENCES = ("LOW", "MEDIUM", "HIGH")

# Directions, exactly as the engine's DIRECTION contract: +1 bullish, -1 bearish; every other state has none.
STATE_DIRECTION = dict(bullish_setup=1, bullish_momentum=1, breakout_watch=1, bearish_setup=-1, bearish_momentum=-1,
                       breakdown_watch=-1)

BULLISH, BEARISH, NON_DIRECTIONAL, UNAVAILABLE = "bullish", "bearish", "non_directional", "unavailable"
AGREE, OPPOSE = "agree", "oppose"
POSITIVE, NEGATIVE, ZERO = "positive", "negative", "zero"
PATTERNS = ("all_bullish", "all_bearish", "all_non_directional", "opposed", "partially_directional", "incomplete")

# Contradiction codes. A "mixed" technical state alone is never a contradiction: it's a non-directional source fact.
TIMEFRAME_OPPOSITION = "timeframe_opposition"
MARKET_CONTEXT_OPPOSES_TIMEFRAME = "market_context_opposes_timeframe"
TIMEFRAME_UNAVAILABLE = "timeframe_unavailable"
MARKET_CONTEXT_UNAVAILABLE = "market_context_unavailable"
NEWS_UNAVAILABLE = "news_unavailable"
CONTEXT_SESSION_MISMATCH = "context_session_mismatch"
COMPARISON_MISALIGNED = "comparison_misaligned"
CONTRADICTION_CODES = (TIMEFRAME_OPPOSITION, MARKET_CONTEXT_OPPOSES_TIMEFRAME, TIMEFRAME_UNAVAILABLE,
                       MARKET_CONTEXT_UNAVAILABLE, NEWS_UNAVAILABLE, CONTEXT_SESSION_MISMATCH, COMPARISON_MISALIGNED)


def state_direction(state):
    """bullish | bearish | non_directional | unavailable for a technical state (None = missing row)."""
    if state is None or state == "insufficient_data":
        return UNAVAILABLE
    value = STATE_DIRECTION.get(state)
    if value == 1:
        return BULLISH
    if value == -1:
        return BEARISH
    return NON_DIRECTIONAL


def pair_relation(first, second):
    """agree | oppose | non_directional | unavailable between two state directions (order-independent)."""
    if UNAVAILABLE in (first, second):
        return UNAVAILABLE
    if NON_DIRECTIONAL in (first, second):
        return NON_DIRECTIONAL
    return AGREE if first == second else OPPOSE


def timeframe_pattern(directions):
    """Compact pattern over the three directions, by fixed precedence (no weighting)."""
    directions = tuple(directions)
    if UNAVAILABLE in directions:
        return "incomplete"
    if BULLISH in directions and BEARISH in directions:
        return "opposed"
    if all(d == BULLISH for d in directions):
        return "all_bullish"
    if all(d == BEARISH for d in directions):
        return "all_bearish"
    if all(d == NON_DIRECTIONAL for d in directions):
        return "all_non_directional"
    return "partially_directional"


def sign(value):
    """Strict sign of a canonical decimal string: positive (> 0), negative (< 0), zero (== 0); no deadband."""
    if value is None:
        return UNAVAILABLE
    if not isinstance(value, str):
        raise ValueError("market-context value must be a canonical decimal string")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("market-context value must be a canonical decimal string") from None
    if not number.is_finite():
        raise ValueError("market-context value must be finite")
    return POSITIVE if number > 0 else NEGATIVE if number < 0 else ZERO


def context_relation(direction, value_sign):
    """agree | oppose | non_directional | unavailable between a timeframe direction and a market-context sign."""
    if direction == UNAVAILABLE or value_sign == UNAVAILABLE:
        return UNAVAILABLE
    if direction == NON_DIRECTIONAL or value_sign == ZERO:
        return NON_DIRECTIONAL
    return AGREE if (direction == BULLISH) == (value_sign == POSITIVE) else OPPOSE
