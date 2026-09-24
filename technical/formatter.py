"""Human-readable snapshot text for terminals and tests (never sent to Telegram in Phase 4)."""


def _fmt(value, digits=2):
    return "n/a" if value is None else f"{value:.{digits}f}"


def format_snapshot(snapshot):
    s = snapshot
    emas = s.ema
    keys = list(emas)
    fast, mid, slow = (emas[k] for k in keys[:3])
    if None in (fast, mid, slow):
        ema_line = "warming up"
    else:
        rel = lambda a, b: ">" if a > b else "<" if a < b else "="
        ema_line = f"{keys[0][3:]} {rel(fast, mid)} {keys[1][3:]} {rel(mid, slow)} {keys[2][3:]}"
    vwap = s.vwap_state
    vwap_line = ("n/a" if vwap["position"] is None else
                 f"Price {vwap['distance_percent']:+.1f}% {'above' if vwap['distance_percent'] >= 0 else 'below'} VWAP")
    lines = [f"{s.symbol} — {s.interval}", f"Price: {s.price:.2f}", "", "Structure:",
             f"{s.last_high_type or '—'} / {s.last_low_type or '—'}", f"Trend: {s.trend.replace('_', ' ').title()}", "",
             "EMA:", ema_line, "", "VWAP:", vwap_line, "", f"RSI({s.evidence.get('rsi_period', 14)}):", _fmt(s.rsi, 1), "",
             "Volume:", "n/a" if s.relative_volume is None else f"{s.relative_volume:.1f}x average", ""]
    if s.gap_type:
        lines += ["Gap:", f"{s.gap_type.replace('_', ' ')} ({s.gap_percent:+.2f}%)", ""]
    levels = [f"Support {s.support_levels[0]['price']:.2f} ({s.support_levels[0]['touches']} touches)"] if s.support_levels else []
    if s.resistance_levels:
        levels.append(f"Resistance {s.resistance_levels[0]['price']:.2f} ({s.resistance_levels[0]['touches']} touches)")
    if s.breakout_state != "none":
        levels.append(f"{s.breakout_state.replace('_', ' ').capitalize()} of {s.breakout_level:.2f}")
    if levels:
        lines += ["Levels:", *levels, ""]
    lines += ["Signal:", f"{s.signal.state.upper()} (confidence {s.signal.confidence})", "", "Reasons:"]
    lines += [f"- {reason}" for reason in s.signal.reasons]
    return "\n".join(lines)


def _timeframe_title(label):
    return "Daily" if label == "1d" else label


def format_multi_timeframe(multi):
    """Each timeframe reported independently; there is no combined verdict."""
    lines = [multi.symbol, ""]
    for label, s in multi.timeframes.items():
        lines.append(f"{_timeframe_title(label)}:")
        if s is None:
            lines += [f"  unavailable ({multi.missing.get(label, 'missing')})", ""]
            continue
        vwap = s.vwap_state["position"]
        lines += [f"  State: {s.signal.state} (confidence {s.signal.confidence})",
                  f"  Structure: {s.last_high_type or '—'} / {s.last_low_type or '—'} ({s.trend.replace('_', ' ')})",
                  f"  RSI: {_fmt(s.rsi, 1)}",
                  f"  Price: {s.price:.2f}" + ("" if vwap is None else f", {vwap.replace('_', ' ')}"),
                  f"  Bar: {s.timestamp.isoformat()}", ""]
    lines.append("Timeframes are independent; no combined signal is produced.")
    return "\n".join(lines)
