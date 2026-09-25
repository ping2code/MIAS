"""Deterministic technical indicators (pure, causal: output[i] uses only inputs[0..i]).

Every function returns a list aligned with its input. ``None`` means "insufficient
history", and warm-up values are never fabricated. Math is float (see
market_data.models for why that is safe).

- **SMA** (Simple Moving Average): the mean of the last N values.
- **EMA** (Exponential Moving Average): seeded with the SMA of the first N values
  at index N-1 (the StockCharts convention), then ``ema = (value - prev) * k + prev``
  with ``k = 2 / (N + 1)``.
- **RSI** (Relative Strength Index), Wilder: first average gain/loss are simple
  means of the first N changes (at index N); then ``avg = (prev * (N - 1) + current) / N``.
  ``RSI = 100 - 100 / (1 + avg_gain / avg_loss)``; losses only is 0, gains only is
  100, flat is 50.
- **TR** (True Range): ``max(high - low, |high - prev_close|, |low - prev_close|)``;
  the first bar has no previous close, so its TR is ``high - low``.
- **ATR** (Average True Range), Wilder: the first ATR is the mean of the first N
  TRs (at index N-1); then ``atr = (prev * (N - 1) + tr) / N``. It measures
  volatility, not direction.
- **VWAP** (Volume Weighted Average Price): the cumulative sum of
  ``typical_price * volume`` divided by cumulative volume, where
  ``typical_price = (high + low + close) / 3``. It resets at every new regular
  session (exchange-time date). Only regular-session bars count; extended-hours
  bars get None. VWAP stays None until the session has traded volume;
  zero-volume bars add nothing. Daily bars get None (VWAP is an intraday
  measure).
- **Volume** is converted to float like prices. It may be fractional; whole-number
  volumes stay exact, since float sums of integers below 2^53 are exact.
- **Relative volume:** current volume divided by the mean volume of the previous
  N bars (the current bar is excluded). It is None until N prior bars exist or
  when that mean is 0.
"""
from market_data.models import session_date


def _floats(values):
    return [None if v is None else float(v) for v in values]


def sma(values, period):
    _check(period)
    values = _floats(values)
    out, window_sum = [], 0.0
    for i, value in enumerate(values):
        window_sum += value
        if i >= period:
            window_sum -= values[i - period]
        out.append(window_sum / period if i >= period - 1 else None)
    return out


def ema(values, period):
    _check(period)
    values, k = _floats(values), 2.0 / (period + 1)
    out, prev = [], None
    for i, value in enumerate(values):
        if i < period - 1:
            out.append(None)
            continue
        prev = sum(values[:period]) / period if i == period - 1 else (value - prev) * k + prev
        out.append(prev)
    return out


def rsi(closes, period=14):
    _check(period)
    closes = _floats(closes)
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(max(c, 0.0) for c in changes[:period]) / period
    avg_loss = sum(max(-c, 0.0) for c in changes[:period]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        change = changes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain, avg_loss):
    if avg_loss == 0.0:
        return 50.0 if avg_gain == 0.0 else 100.0  # Flat is neutral; gains only is 100 (no division by zero).
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def true_range(bars):
    out = []
    for i, bar in enumerate(bars):
        high, low = float(bar.high), float(bar.low)
        if i == 0:
            out.append(high - low)
        else:
            prev_close = float(bars[i - 1].close)
            out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr(bars, period=14):
    _check(period)
    ranges = true_range(bars)
    out, prev = [], None
    for i, value in enumerate(ranges):
        if i < period - 1:
            out.append(None)
            continue
        prev = sum(ranges[:period]) / period if i == period - 1 else (prev * (period - 1) + value) / period
        out.append(prev)
    return out


def vwap(bars):
    out, current, pv, volume = [], None, 0.0, 0.0
    for bar in bars:
        if not bar.interval.intraday or not bar.regular:
            out.append(None)
            continue
        key = session_date(bar.timestamp)
        if key != current:  # New regular session: reset.
            current, pv, volume = key, 0.0, 0.0
        shares = float(bar.volume)
        pv += bar.typical_price * shares
        volume += shares
        out.append(pv / volume if volume else None)
    return out


def relative_volume(bars, lookback=20):
    """(average_volume, relative_volume) per bar over the previous ``lookback`` bars."""
    _check(lookback)
    averages, relatives = [], []
    for i, bar in enumerate(bars):
        if i < lookback:
            averages.append(None)
            relatives.append(None)
            continue
        average = sum([float(b.volume) for b in bars[i - lookback:i]]) / lookback
        averages.append(average)
        relatives.append(float(bar.volume) / average if average > 0 else None)
    return averages, relatives


def _check(period):
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError("period must be a positive integer")
