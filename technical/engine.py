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
It remains the reference implementation; ``technical.incremental`` is the
per-bar O(1)-state engine for live use (Phase 4B), and the tests assert that the
two produce identical snapshots.

Phase 4B additions (no formula changes):

- Levels use at most ``max_pivot_history`` (default 200) of the most recently
  confirmed pivots. This bounds live memory, and the replay applies the same
  rule so both engines agree. Structure counts still cover all pivots.
- With an ``ExchangeCalendar``, a gap is measured only against the **previous
  trading session's** final regular close. If that session is missing from the
  data, the gap is unknown (None), never measured against an older session.
  Daily gaps likewise require the previous bar to be the previous trading day.
  Without a calendar, Phase 4 behaviour is unchanged.
"""
from market_data.models import session_date
from market_data.validation import validate_series
from technical import indicators
from technical.levels import breakout_state, cluster_levels, split_levels
from technical.models import TechnicalConfig, TechnicalSnapshot
from technical.signals import classify
from technical.states import classify_gap, ema_state, momentum, vwap_state
from technical.structure import confirmed, find_pivots, label_pivots, structure_state


def _gaps(bars, threshold_pct, calendar=None):
    """Per bar: (gap_type, gap_percent, gap_absolute) for the bar's session, causal."""
    out, previous_close, last_regular_close, last_regular_date, session, session_gap = [], None, None, None, None, None
    for i, bar in enumerate(bars):
        if not bar.interval.intraday:
            gap = None
            if i and (calendar is None or session_date(bars[i - 1].timestamp) ==
                      calendar.previous_trading_day(session_date(bar.timestamp))):
                gap = (float(bar.open), float(bars[i - 1].close))
            out.append(classify_gap(gap, threshold_pct))
            continue
        if not bar.regular:
            out.append((None, None, None))
            continue
        key = session_date(bar.timestamp)
        if key != session:
            session, previous_close = key, last_regular_close
            if calendar is not None and last_regular_date != calendar.previous_trading_day(key):
                previous_close = None  # The previous trading session is missing: the gap is unknown.
            session_gap = classify_gap(None if previous_close is None else (float(bar.open), previous_close), threshold_pct)
        last_regular_close, last_regular_date = float(bar.close), key
        out.append(session_gap)
    return out


class TechnicalEngine:
    def __init__(self, config=None, calendar=None):
        self.config = config or TechnicalConfig()
        self.calendar = calendar

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
        gaps = _gaps(bars, c.gap_threshold_pct, self.calendar)
        pivots = label_pivots(find_pivots(bars, c.pivot_window), c.equal_tolerance_pct)
        snapshots, events, prior_levels = [], [], []
        for i, bar in enumerate(bars):
            known = confirmed(pivots, i)
            structure = structure_state(known)
            levels = cluster_levels(known[-c.max_pivot_history:], atr=atr[i], cluster_pct=c.cluster_pct, cluster_atr=c.cluster_atr,
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
                breakout_level=level_price,
                ema_state=ema_state(c, {p: emas[p][i] for p in c.ema_periods},
                                    {p: emas[p][i - 1] for p in c.ema_periods} if i else None, closes[i]),
                vwap_state=vwap_state(vwap[i], closes[i], vwap[i - 1] if i and session_date(bars[i - 1].timestamp) ==
                                      session_date(bar.timestamp) else None, closes[i - 1] if i else None),
                momentum=momentum(c, rsi[i]),
                evidence=dict(rsi_period=c.rsi_period, structure=structure, confirmed_pivots=len(known), levels=len(levels), breakout_buffer=round(pad, 4)))
            snapshots.append(_with_signal(snapshot, classify(snapshot, c)))
            prior_levels = levels
        return snapshots


def _with_signal(snapshot, signal):
    from dataclasses import replace
    return replace(snapshot, signal=signal)
