"""Descriptive per-bar states shared by the replay and incremental engines (scalar inputs only).

These helpers turn already-computed indicator values into labels. The two engines
compute the indicator values independently, so the replay-vs-incremental
equivalence tests still cross-check all the numeric work.
"""


def _sign(value):
    return 0 if value is None else (value > 0) - (value < 0)


def _cross(now_a, now_b, before_a, before_b):
    if None in (now_a, now_b, before_a, before_b):
        return None
    now, before = _sign(now_a - now_b), _sign(before_a - before_b)
    if now > 0 >= before:
        return "bullish_cross"
    if now < 0 <= before:
        return "bearish_cross"
    return None


def ema_state(config, current, previous, close):
    """``current``/``previous`` map period -> EMA value (``previous`` is None on the first bar)."""
    fast, mid, slow, _ = config.ema_periods
    e_fast, e_mid, e_slow = current[fast], current[mid], current[slow]
    if None in (e_fast, e_mid, e_slow):
        alignment = None
    elif e_fast > e_mid > e_slow:
        alignment = "bullish_alignment"
    elif e_fast < e_mid < e_slow:
        alignment = "bearish_alignment"
    else:
        alignment = "mixed"
    prev = previous or {}
    return dict(alignment=alignment,
                price_vs_ema20=None if e_mid is None else ("above" if close > e_mid else "below"),
                **{f"ema{fast}_{mid}_cross": _cross(e_fast, e_mid, prev.get(fast), prev.get(mid)),
                   f"ema{mid}_{slow}_cross": _cross(e_mid, e_slow, prev.get(mid), prev.get(slow))})


def vwap_state(value, close, previous_vwap, previous_close):
    """``previous_vwap`` must be None unless the previous bar is in the same regular session."""
    if value is None:
        return dict(position=None, distance_dollars=None, distance_percent=None)
    above = close > value
    if previous_vwap is not None and above and previous_close <= previous_vwap:
        position = "crossing_above_vwap"
    elif previous_vwap is not None and not above and previous_close > previous_vwap:
        position = "crossing_below_vwap"
    else:
        position = "above_vwap" if above else "below_vwap"
    return dict(position=position, distance_dollars=round(close - value, 4),
                distance_percent=round((close - value) / value * 100.0, 4))


def momentum(config, value):
    if value is None:
        return None
    if value >= config.rsi_overbought:
        return "overbought_like"
    if value >= config.rsi_strong:
        return "strong"
    if value <= config.rsi_oversold:
        return "oversold_like"
    if value <= config.rsi_weak:
        return "weak"
    return "neutral"


def classify_gap(gap, threshold_pct):
    """``gap`` is (open, previous_close) or None -> (gap_type, gap_percent, gap_absolute)."""
    if gap is None:
        return (None, None, None)
    open_, previous_close = gap
    absolute = open_ - previous_close
    percent = absolute / previous_close * 100.0
    kind = "gap_up" if percent >= threshold_pct else "gap_down" if percent <= -threshold_pct else "no_gap"
    return (kind, round(percent, 4), round(absolute, 4))
