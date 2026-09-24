"""Deterministic technical-state classification with reasons and explainable confidence (never BUY/SELL).

Evidence items, each with a direction (+1 bullish, -1 bearish, 0 neutral):

| Evidence | +1 | -1 |
|---|---|---|
| structure | trend ``bullish`` (HH/HL) | trend ``bearish`` (LH/LL) |
| EMA alignment | EMA9 > EMA20 > EMA50 | EMA9 < EMA20 < EMA50 |
| price vs EMA20 | above | below |
| VWAP (intraday, when defined) | above or crossing above | below or crossing below |
| momentum (RSI zone) | ``strong`` / ``overbought_like`` | ``weak`` / ``oversold_like`` |
| session gap | ``gap_up`` | ``gap_down`` |

Elevated relative volume (at least ``elevated_relative_volume``) is not a direction
by itself; it counts as one extra agreeing item for a directional state.

States, first match wins:

1. ``insufficient_data``: the medium EMA (EMA20 by default), RSI or ATR is still
   warming up.
2. ``breakout_watch`` / ``breakdown_watch``: this bar broke resistance or support by
   more than the buffer.
3. ``bullish_setup``: bullish structure, bullish EMA alignment, and price above
   VWAP (or above EMA20 when VWAP is undefined).
4. ``bearish_setup``: the mirror image.
5. ``range``: structure ``range`` (contracting or equal highs/lows). A confirmed
   range outranks short-term momentum inside it; it is left through a
   breakout/breakdown or a new confirmed structure.
6. ``bullish_momentum``: structure not bearish, bullish EMA alignment, price above
   EMA20, RSI ``strong`` or higher, and not below VWAP.
7. ``bearish_momentum``: the mirror image.
8. ``mixed``: everything else, including a failed breakout or breakdown (named in
   the reasons).

Confidence (directional states only; the others are always LOW) counts agreeing
(A) and conflicting (C) evidence items:

- **HIGH:** A >= 5 and C = 0;
- **MEDIUM:** A >= 3 and C <= 1;
- **LOW:** otherwise.
"""
from technical.models import TechnicalSignal

DIRECTION = dict(bullish_setup=1, bullish_momentum=1, breakout_watch=1, bearish_setup=-1, bearish_momentum=-1,
                 breakdown_watch=-1)


def evidence_items(snapshot, config):
    items = []
    trend = snapshot.trend
    if trend in ("bullish", "bearish"):
        items.append(("structure", 1 if trend == "bullish" else -1,
                      f"confirmed {snapshot.last_high_type}/{snapshot.last_low_type} structure"))
    alignment = snapshot.ema_state.get("alignment")
    if alignment in ("bullish_alignment", "bearish_alignment"):
        fast, mid, slow = config.ema_periods[:3]
        sign = 1 if alignment == "bullish_alignment" else -1
        items.append(("ema_alignment", sign, f"EMA{fast} {'>' if sign > 0 else '<'} EMA{mid} {'>' if sign > 0 else '<'} EMA{slow}"))
    side = snapshot.ema_state.get("price_vs_ema20")
    if side in ("above", "below"):
        items.append(("price_vs_ema20", 1 if side == "above" else -1, f"price {side} EMA{config.ema_periods[1]}"))
    vwap_position = snapshot.vwap_state.get("position")
    if vwap_position:
        sign = 1 if vwap_position in ("above_vwap", "crossing_above_vwap") else -1
        items.append(("vwap", sign, f"{vwap_position.replace('_', ' ')} ({snapshot.vwap_state['distance_percent']:+.2f}%)"))
    if snapshot.momentum in ("strong", "overbought_like", "weak", "oversold_like"):
        sign = 1 if snapshot.momentum in ("strong", "overbought_like") else -1
        items.append(("momentum", sign, f"RSI {snapshot.rsi:.1f} ({snapshot.momentum.replace('_', '-')})"))
    if snapshot.gap_type in ("gap_up", "gap_down"):
        items.append(("gap", 1 if snapshot.gap_type == "gap_up" else -1,
                      f"{snapshot.gap_type.replace('_', ' ')} {snapshot.gap_percent:+.2f}%"))
    return items


def _confidence(state, items, snapshot, config):
    direction = DIRECTION.get(state)
    if direction is None:
        return "LOW", 0, 0
    agreeing = sum(1 for _, sign, _ in items if sign == direction)
    conflicting = sum(1 for _, sign, _ in items if sign == -direction)
    if snapshot.relative_volume is not None and snapshot.relative_volume >= config.elevated_relative_volume:
        agreeing += 1
    if agreeing >= 5 and conflicting == 0:
        return "HIGH", agreeing, conflicting
    if agreeing >= 3 and conflicting <= 1:
        return "MEDIUM", agreeing, conflicting
    return "LOW", agreeing, conflicting


def classify(snapshot, config):
    mid = config.ema_periods[1]
    missing = [name for name, value in ((f"EMA{mid}", snapshot.ema.get(f"ema{mid}")), ("RSI", snapshot.rsi),
                                        ("ATR", snapshot.atr)) if value is None]
    if missing:
        return TechnicalSignal("insufficient_data", "LOW", (f"warming up: {', '.join(missing)} not yet available",))
    items = evidence_items(snapshot, config)
    signs = {name: sign for name, sign, _ in items}
    structure, alignment = signs.get("structure", 0), signs.get("ema_alignment", 0)
    above_ema20 = signs.get("price_vs_ema20") == 1
    vwap = signs.get("vwap")
    price_side = vwap if vwap is not None else signs.get("price_vs_ema20", 0)
    momentum = signs.get("momentum", 0)
    if snapshot.breakout_state == "breakout":
        state = "breakout_watch"
    elif snapshot.breakout_state == "breakdown":
        state = "breakdown_watch"
    elif structure == 1 and alignment == 1 and price_side == 1:
        state = "bullish_setup"
    elif structure == -1 and alignment == -1 and price_side == -1:
        state = "bearish_setup"
    elif snapshot.trend == "range":
        state = "range"
    elif structure != -1 and alignment == 1 and above_ema20 and momentum == 1 and vwap != -1:
        state = "bullish_momentum"
    elif structure != 1 and alignment == -1 and signs.get("price_vs_ema20") == -1 and momentum == -1 and vwap != 1:
        state = "bearish_momentum"
    else:
        state = "mixed"
    reasons = [text for _, _, text in items]
    if snapshot.breakout_state in ("breakout", "breakdown", "failed_breakout", "failed_breakdown"):
        verb = snapshot.breakout_state.replace("_", " ")
        reasons.insert(0, f"{verb} of level {snapshot.breakout_level:.2f}")
    if snapshot.relative_volume is not None and snapshot.relative_volume >= config.elevated_relative_volume:
        reasons.append(f"relative volume {snapshot.relative_volume:.1f}x (elevated)")
    if state == "range":
        reasons.insert(0, f"range structure ({snapshot.last_high_type}/{snapshot.last_low_type})")
    if state == "mixed" and not reasons:
        reasons.append("no agreeing technical evidence")
    confidence, agreeing, conflicting = _confidence(state, items, snapshot, config)
    return TechnicalSignal(state, confidence, tuple(reasons), agreeing, conflicting)
