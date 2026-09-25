"""Incremental technical engine: one completed bar in, one snapshot out, bounded state (Phase 4B).

``IncrementalTechnicalEngine.update(bar)`` keeps independent state per
(symbol, interval) and produces exactly the snapshot that
``TechnicalEngine.replay`` produces for the same bar with the same history. The
tests compare every field of every bar with zero tolerance: both engines perform
the same floating-point operations in the same order.

**State per series** (memory is independent of how many bars were processed):

| Component | Retained |
|---|---|
| EMA (each period) | up to N seed values while warming, then the latest value |
| RSI | previous close; up to N seed gains/losses, then the Wilder averages |
| ATR | previous close; up to N seed true ranges, then the latest ATR |
| VWAP | the current session's price x volume and volume sums |
| volume | the last ``volume_lookback`` volumes |
| pivots | the last ``2 x pivot_window + 1`` highs/lows; latest labelled high and low; confirmed counts |
| levels | at most ``max_pivot_history`` confirmed pivots, and the previous bar's levels |
| breakouts | the last ``failure_lookback`` closes and break events |
| gaps | the current session's gap and the last regular close/date |
| snapshot | the latest snapshot only |

**Ordering:** a bar with a timestamp equal to the last one raises ``DuplicateBarError``;
an older bar raises ``OutOfOrderBarError``. A rejected bar never changes state.
History is never rewritten in live mode; use ``TechnicalEngine.replay`` for that.

**Warm-up/restart:** ``warmup(bars)`` validates a historical series and feeds it
through ``update``. A fresh engine warmed with the same bars reaches identical
state, so a restart is just a warm-up from recent history.
"""
from collections import deque
from dataclasses import dataclass, field, replace

from market_data.models import Interval, MarketBar, MarketDataError, session_date
from market_data.validation import validate_calendar_series, validate_series
from technical.indicators import _rsi_value
from technical.levels import breakout_state, cluster_levels, split_levels
from technical.models import TechnicalConfig, TechnicalSnapshot
from technical.signals import classify
from technical.states import classify_gap, ema_state, momentum, vwap_state
from technical.structure import Pivot, _compare, structure_from


class DuplicateBarError(MarketDataError):
    """A bar with the same timestamp as the last processed bar."""


class OutOfOrderBarError(MarketDataError):
    """A bar older than the last processed bar."""


@dataclass
class _Ema:
    """EMA seeded like ``indicators.ema``: ``sum(first N) / N`` (Python's compensated float ``sum``), then recursive."""
    period: int
    seed: list = field(default_factory=list)  # Holds at most ``period`` values, emptied once seeded.
    value: float = None

    def update(self, x):
        if self.value is None:
            self.seed.append(x)
            if len(self.seed) < self.period:
                return None
            self.value, self.seed = sum(self.seed) / self.period, []
            return self.value
        self.value = (x - self.value) * (2.0 / (self.period + 1)) + self.value
        return self.value


@dataclass
class _Wilder:
    """Wilder-smoothed average seeded with ``sum(first N) / N``, then ``(prev x (N - 1) + x) / N``."""
    period: int
    seed: list = field(default_factory=list)
    value: float = None

    def update(self, x):
        if self.value is None:
            self.seed.append(x)
            if len(self.seed) == self.period:
                self.value, self.seed = sum(self.seed) / self.period, []
            return self.value
        self.value = (self.value * (self.period - 1) + x) / self.period
        return self.value


@dataclass
class _Series:
    symbol: str
    interval: object
    config: TechnicalConfig
    count: int = 0
    last_timestamp: object = None
    previous_close: float = None
    previous_timestamp: object = None
    previous_emas: dict = None
    previous_vwap: float = None
    emas: dict = field(default_factory=dict)
    gain: _Wilder = None
    loss: _Wilder = None
    rsi_changes: int = 0
    atr: _Wilder = None
    vwap_session: object = None
    vwap_pv: float = 0.0
    vwap_volume: float = 0.0
    volumes: deque = None
    window: deque = None
    level_pivots: deque = None
    last_high: Pivot = None
    last_low: Pivot = None
    high_count: int = 0
    low_count: int = 0
    closes: deque = None
    events: deque = field(default_factory=deque)
    prior_levels: list = field(default_factory=list)
    gap_session: object = None
    gap_last_close: float = None
    gap_last_date: object = None
    session_gap: tuple = (None, None, None)
    snapshot: TechnicalSnapshot = None

    def __post_init__(self):
        c = self.config
        self.emas = {p: _Ema(p) for p in c.ema_periods}
        self.gain, self.loss, self.atr = _Wilder(c.rsi_period), _Wilder(c.rsi_period), _Wilder(c.atr_period)
        self.volumes = deque(maxlen=c.volume_lookback)
        self.window = deque(maxlen=2 * c.pivot_window + 1)
        self.level_pivots = deque(maxlen=c.max_pivot_history)
        self.closes = deque(maxlen=c.failure_lookback)

    def size(self):
        """Container lengths (for memory-bound tests)."""
        return dict(volumes=len(self.volumes), window=len(self.window), level_pivots=len(self.level_pivots),
                    closes=len(self.closes), events=len(self.events), prior_levels=len(self.prior_levels))


class IncrementalTechnicalEngine:
    def __init__(self, config=None, calendar=None):
        self.config = config or TechnicalConfig()
        self.calendar = calendar
        self._series = {}

    def series_keys(self):
        return sorted((symbol, interval.label) for symbol, interval in self._series)

    def latest(self, symbol, interval):
        state = self._series.get((symbol, Interval.parse(interval)))
        return state.snapshot if state else None

    def state_size(self, symbol, interval):
        return self._series[(symbol, Interval.parse(interval))].size()

    def reset(self, symbol, interval):
        self._series.pop((symbol, Interval.parse(interval)), None)

    def warmup(self, bars):
        """Feed a validated historical series; returns the last snapshot (None for no bars)."""
        bars = validate_series(bars)
        snapshot = None
        for bar in bars:
            snapshot = self.update(bar)
        return snapshot

    def update(self, bar):
        if not isinstance(bar, MarketBar):
            raise MarketDataError("update() takes a MarketBar")
        key = (bar.symbol, bar.interval)
        state = self._series.get(key)
        if state is not None and state.last_timestamp is not None:
            if bar.timestamp == state.last_timestamp:
                raise DuplicateBarError(f"duplicate bar {bar.timestamp.isoformat()} for {bar.symbol} {bar.interval.label}")
            if bar.timestamp < state.last_timestamp:
                raise OutOfOrderBarError(f"out-of-order bar {bar.timestamp.isoformat()} for {bar.symbol} "
                                         f"{bar.interval.label} (last {state.last_timestamp.isoformat()})")
        if self.calendar is not None:
            validate_calendar_series([bar], self.calendar)
        if state is None:
            state = self._series[key] = _Series(bar.symbol, bar.interval, self.config)
        state.snapshot = self._step(state, bar)
        return state.snapshot

    def _step(self, s, bar):
        c, i = self.config, s.count
        close, high, low, shares = float(bar.close), float(bar.high), float(bar.low), float(bar.volume)
        # Indicators (same operations, in the same order, as technical.indicators).
        emas = {p: s.emas[p].update(close) for p in c.ema_periods}
        rsi = None
        if s.previous_close is not None:
            change = close - s.previous_close
            s.rsi_changes += 1
            avg_gain, avg_loss = s.gain.update(max(change, 0.0)), s.loss.update(max(-change, 0.0))
            if s.rsi_changes >= c.rsi_period:
                rsi = _rsi_value(avg_gain, avg_loss)
        tr = high - low if s.previous_close is None else max(high - low, abs(high - s.previous_close),
                                                             abs(low - s.previous_close))
        atr = s.atr.update(tr)
        vwap = None
        if bar.interval.intraday and bar.regular:
            day = session_date(bar.timestamp)
            if day != s.vwap_session:
                s.vwap_session, s.vwap_pv, s.vwap_volume = day, 0.0, 0.0
            s.vwap_pv += bar.typical_price * shares
            s.vwap_volume += shares
            vwap = s.vwap_pv / s.vwap_volume if s.vwap_volume else None
        average = relative = None
        if len(s.volumes) == c.volume_lookback:
            average = sum(s.volumes) / c.volume_lookback
            relative = shares / average if average > 0 else None
        gap = self._gap(s, bar)
        # Pivots: the candidate pivot_window bars back is decided now that its right side exists.
        s.window.append((i, high, low, bar.timestamp))
        w = c.pivot_window
        if len(s.window) == 2 * w + 1 and i >= 2 * w:
            j, h, l, stamp = s.window[w]
            left, right = list(s.window)[:w], list(s.window)[w + 1:]
            if all(h > x[1] for x in left) and all(h >= x[1] for x in right):
                s.last_high = self._confirm(s, "high", j, i, h, stamp, s.last_high)
                s.high_count += 1
            if all(l < x[2] for x in left) and all(l <= x[2] for x in right):
                s.last_low = self._confirm(s, "low", j, i, l, stamp, s.last_low)
                s.low_count += 1
        structure = structure_from(s.last_high, s.last_low, s.high_count, s.low_count)
        levels = cluster_levels(list(s.level_pivots), atr=atr, cluster_pct=c.cluster_pct, cluster_atr=c.cluster_atr,
                                min_touches=c.min_touches)
        while s.events and i - s.events[0][0] > c.failure_lookback:
            s.events.popleft()
        recent = [(kind, price) for _, kind, price in s.events]
        state, level_price, pad = breakout_state(close, list(s.closes), s.prior_levels, atr, recent,
                                                 buffer_pct=c.buffer_pct, buffer_atr=c.buffer_atr)
        if state in ("breakout", "breakdown"):
            s.events.append((i, state, level_price))
        supports, resistances = split_levels(levels, close)
        same_session = s.previous_timestamp is not None and \
            session_date(s.previous_timestamp) == session_date(bar.timestamp)
        snapshot = TechnicalSnapshot(
            symbol=bar.symbol, timestamp=bar.timestamp, interval=bar.interval.label, index=i, price=close,
            ema={f"ema{p}": emas[p] for p in c.ema_periods}, vwap=vwap, rsi=rsi, atr=atr, volume=shares,
            average_volume=average, relative_volume=relative, trend=structure["trend"],
            last_high_type=structure["last_high_type"], last_low_type=structure["last_low_type"],
            significant_high=structure["last_significant_high"], significant_low=structure["last_significant_low"],
            support_levels=tuple(l.as_dict(close) for l in supports),
            resistance_levels=tuple(l.as_dict(close) for l in resistances),
            gap_type=gap[0], gap_percent=gap[1], gap_absolute=gap[2], breakout_state=state, breakout_level=level_price,
            ema_state=ema_state(c, emas, s.previous_emas, close),
            vwap_state=vwap_state(vwap, close, s.previous_vwap if same_session else None, s.previous_close),
            momentum=momentum(c, rsi),
            evidence=dict(rsi_period=c.rsi_period, structure=structure, confirmed_pivots=s.high_count + s.low_count,
                          levels=len(levels), breakout_buffer=round(pad, 4)))
        snapshot = replace(snapshot, signal=classify(snapshot, c))
        # Advance per-bar history.
        s.volumes.append(shares)
        s.closes.append(close)
        s.prior_levels = levels
        s.previous_close, s.previous_timestamp, s.previous_emas, s.previous_vwap = close, bar.timestamp, emas, vwap
        s.last_timestamp, s.count = bar.timestamp, i + 1
        return snapshot

    def _confirm(self, s, kind, index, confirmed_at, price, stamp, previous):
        label = None
        if previous is not None:
            label = _compare(price, previous.price, self.config.equal_tolerance_pct) + ("H" if kind == "high" else "L")
        pivot = Pivot(kind, index, confirmed_at, price, stamp, label)
        s.level_pivots.append(pivot)
        return pivot

    def _gap(self, s, bar):
        threshold = self.config.gap_threshold_pct
        if not bar.interval.intraday:
            gap = None
            if s.previous_close is not None and (self.calendar is None or session_date(s.previous_timestamp) ==
                                                 self.calendar.previous_trading_day(session_date(bar.timestamp))):
                gap = (float(bar.open), s.previous_close)
            return classify_gap(gap, threshold)
        if not bar.regular:
            return (None, None, None)
        day = session_date(bar.timestamp)
        if day != s.gap_session:
            previous_close = s.gap_last_close
            if self.calendar is not None and s.gap_last_date != self.calendar.previous_trading_day(day):
                previous_close = None
            s.gap_session = day
            s.session_gap = classify_gap(None if previous_close is None else (float(bar.open), previous_close), threshold)
        s.gap_last_close, s.gap_last_date = float(bar.close), day
        return s.session_gap
