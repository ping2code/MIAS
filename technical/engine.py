"""Technical engine and deterministic replay (no look-ahead).

``TechnicalEngine.replay(bars)`` walks the validated bars in order and emits one
snapshot per bar, using only information available at that bar:

- **Indicators** (EMA, RSI, ATR, VWAP, relative volume) are causal: element ``i``
  depends only on bars ``0..i``.
- **Pivots** enter only at their confirmation bar (``pivot + window``).
- **Levels** are rebuilt at each bar from the pivots confirmed so far. A breakout
  is judged against the previous bar's levels.
- **Gaps** compare a session's first regular open with the previous session's
  last regular close. Before that open, the gap is unknown (None).

``analyze(bars)`` is simply the last replay snapshot. The tests assert that
``replay(bars)[i] == analyze(bars[:i + 1])`` for every bar of every fixture; any
future-data leak would break that equality.

Per-bar level rebuilding is O(pivots), so a full replay is O(bars x pivots).
That is fine for sessions and fixtures, and is documented as a Phase 4B
optimization point.
"""
from market_data.models import session_date
from market_data.validation import validate_series
from technical import indicators
from technical.levels import breakout_state, cluster_levels, split_levels
from technical.models import TechnicalConfig, TechnicalSnapshot
from technical.signals import classify
from technical.structure import confirmed, find_pivots, label_pivots, structure_state


def _gaps(bars, threshold_pct):
    """Per bar: (gap_type, gap_percent, gap_absolute) for the bar's session, causal."""
    out, previous_close, last_regular_close, session, session_gap = [], None, None, None, None
    for i, bar in enumerate(bars):
        if not bar.interval.intraday:
            gap = None if i == 0 else (float(bar.open), float(bars[i - 1].close))
            out.append(_classify_gap(gap, threshold_pct))
            continue
        if not bar.regular:
            out.append((None, None, None))
            continue
        key = session_date(bar.timestamp)
        if key != session:
            session, previous_close = key, last_regular_close
            session_gap = _classify_gap(None if previous_close is None else (float(bar.open), previous_close), threshold_pct)
        last_regular_close = float(bar.close)
        out.append(session_gap)
    return out


def _classify_gap(gap, threshold_pct):
    if gap is None:
        return (None, None, None)
    open_, previous_close = gap
    absolute = open_ - previous_close
    percent = absolute / previous_close * 100.0
    kind = "gap_up" if percent >= threshold_pct else "gap_down" if percent <= -threshold_pct else "no_gap"
    return (kind, round(percent, 4), round(absolute, 4))


def _sign(value):
    return 0 if value is None else (value > 0) - (value < 0)


class TechnicalEngine:
    def __init__(self, config=None):
        self.config = config or TechnicalConfig()

    def analyze(self, bars):
        snapshots = self.replay(bars)
        return snapshots[-1] if snapshots else None

    def replay(self, bars):
        c = self.config
        bars = validate_series(bars)
        if not bars:
            return []
        closes = [float(b.close) for b in bars]
        emas = {period: indicators.ema(closes, period) for period in c.ema_periods}
        rsi = indicators.rsi(closes, c.rsi_period)
        atr = indicators.atr(bars, c.atr_period)
        vwap = indicators.vwap(bars)
        averages, relatives = indicators.relative_volume(bars, c.volume_lookback)
        gaps = _gaps(bars, c.gap_threshold_pct)
        pivots = label_pivots(find_pivots(bars, c.pivot_window), c.equal_tolerance_pct)
        fast, mid, slow, long_ = c.ema_periods
        snapshots, events, prior_levels = [], [], []
        for i, bar in enumerate(bars):
            known = confirmed(pivots, i)
            structure = structure_state(known)
            levels = cluster_levels(known, atr=atr[i], cluster_pct=c.cluster_pct, cluster_atr=c.cluster_atr,
                                    min_touches=c.min_touches)
            recent = [(kind, price) for index, kind, price in events if i - index <= c.failure_lookback]
            state, level_price, pad = breakout_state(closes[i], closes[max(0, i - c.failure_lookback):i], prior_levels, atr[i], recent,
                                                     buffer_pct=c.buffer_pct, buffer_atr=c.buffer_atr)
            if state in ("breakout", "breakdown"):
                events.append((i, state, level_price))
            supports, resistances = split_levels(levels, closes[i])
            ema_values = {f"ema{p}": emas[p][i] for p in c.ema_periods}
            snapshot = TechnicalSnapshot(
                symbol=bar.symbol, timestamp=bar.timestamp, interval=bar.interval.label, index=i, price=closes[i],
                ema=ema_values, vwap=vwap[i], rsi=rsi[i], atr=atr[i], volume=bar.volume, average_volume=averages[i],
                relative_volume=relatives[i], trend=structure["trend"], last_high_type=structure["last_high_type"],
                last_low_type=structure["last_low_type"], significant_high=structure["last_significant_high"],
                significant_low=structure["last_significant_low"],
                support_levels=tuple(l.as_dict(closes[i]) for l in supports),
                resistance_levels=tuple(l.as_dict(closes[i]) for l in resistances),
                gap_type=gaps[i][0], gap_percent=gaps[i][1], gap_absolute=gaps[i][2], breakout_state=state,
                breakout_level=level_price, ema_state=self._ema_state(emas, closes, i), vwap_state=self._vwap_state(vwap, closes, bars, i),
                momentum=self._momentum(rsi[i]),
                evidence=dict(rsi_period=c.rsi_period, structure=structure, confirmed_pivots=len(known), levels=len(levels), breakout_buffer=round(pad, 4)))
            snapshots.append(_with_signal(snapshot, classify(snapshot, c)))
            prior_levels = levels
        return snapshots

    def _ema_state(self, emas, closes, i):
        fast, mid, slow, _ = self.config.ema_periods
        e_fast, e_mid, e_slow = emas[fast][i], emas[mid][i], emas[slow][i]
        if None in (e_fast, e_mid, e_slow):
            alignment = None
        elif e_fast > e_mid > e_slow:
            alignment = "bullish_alignment"
        elif e_fast < e_mid < e_slow:
            alignment = "bearish_alignment"
        else:
            alignment = "mixed"
        def cross(a, b):
            if i == 0 or None in (a[i], b[i], a[i - 1], b[i - 1]):
                return None
            now, before = _sign(a[i] - b[i]), _sign(a[i - 1] - b[i - 1])
            if now > 0 >= before:
                return "bullish_cross"
            if now < 0 <= before:
                return "bearish_cross"
            return None
        return dict(alignment=alignment,
                    price_vs_ema20=None if e_mid is None else ("above" if closes[i] > e_mid else "below"),
                    **{f"ema{fast}_{mid}_cross": cross(emas[fast], emas[mid]),
                       f"ema{mid}_{slow}_cross": cross(emas[mid], emas[slow])})

    def _vwap_state(self, vwap, closes, bars, i):
        value = vwap[i]
        if value is None:
            return dict(position=None, distance_dollars=None, distance_percent=None)
        above = closes[i] > value
        previous = vwap[i - 1] if i and session_date(bars[i - 1].timestamp) == session_date(bars[i].timestamp) else None
        if previous is not None and above and closes[i - 1] <= previous:
            position = "crossing_above_vwap"
        elif previous is not None and not above and closes[i - 1] > previous:
            position = "crossing_below_vwap"
        else:
            position = "above_vwap" if above else "below_vwap"
        return dict(position=position, distance_dollars=round(closes[i] - value, 4),
                    distance_percent=round((closes[i] - value) / value * 100.0, 4))

    def _momentum(self, value):
        c = self.config
        if value is None:
            return None
        if value >= c.rsi_overbought:
            return "overbought_like"
        if value >= c.rsi_strong:
            return "strong"
        if value <= c.rsi_oversold:
            return "oversold_like"
        if value <= c.rsi_weak:
            return "weak"
        return "neutral"


def _with_signal(snapshot, signal):
    from dataclasses import replace
    return replace(snapshot, signal=signal)
