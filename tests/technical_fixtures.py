"""Deterministic SYNTHETIC META/NVDA 5-minute bar scenarios for the technical engine.

These are generated shapes patterned after realistic intraday behavior. They are
NOT historical prices. The price scales (META ~740, NVDA ~182) are arbitrary.

Generation:

- Each regular session has 78 five-minute bars from 09:30 America/New_York.
- A path is a list of (bar, percent) waypoints relative to the session base, with
  linear interpolation between them.
- ``open`` is the previous close (gapless intraday); a session's first open can
  carry a gap.
- ``high``/``low`` add a 0.05% multiplicative wick above/below max/min(open, close).
- Prices are rounded to cents. Volume is U-shaped with optional per-bar multipliers.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
import math

from market_data.models import EXCHANGE_TZ, MarketBar

BASES = dict(META=740.0, NVDA=182.0)
VOLUME = dict(META=150_000, NVDA=1_200_000)
BARS_PER_SESSION = 78
WICK = 0.0005
DAY1, DAY2 = date(2026, 9, 21), date(2026, 9, 22)  # A Monday and Tuesday (synthetic dates).


def _cents(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _path(waypoints, n=BARS_PER_SESSION):
    points = sorted(waypoints)
    values = []
    for i in range(n):
        for (i0, p0), (i1, p1) in zip(points, points[1:]):
            if i0 <= i <= i1:
                values.append(p0 + (p1 - p0) * (i - i0) / (i1 - i0))
                break
        else:
            values.append(points[-1][1])
    return values


def session(symbol, day, waypoints, *, base, open_price=None, spikes=None):
    """One synthetic regular session; returns (bars, last_close)."""
    start = datetime.combine(day, time(9, 30), tzinfo=EXCHANGE_TZ)
    percents = _path(waypoints)
    closes = [base * (1 + p / 100.0) for p in percents]
    bars, previous = [], open_price if open_price is not None else closes[0]
    for i, close in enumerate(closes):
        o = previous
        high, low = max(o, close) * (1 + WICK), min(o, close) * (1 - WICK)
        shape = 1.6 - 1.1 * math.sin(math.pi * i / (BARS_PER_SESSION - 1))  # U-shaped intraday volume.
        volume = int(VOLUME[symbol] * shape * (spikes or {}).get(i, 1.0))
        bars.append(MarketBar(symbol, start + timedelta(minutes=5 * i), "5m", _cents(o), _cents(high), _cents(low),
                              _cents(close), volume))
        previous = close
    return bars, closes[-1]


def zigzag(start, step_up, step_down, legs, first=0, spacing=6):
    """Alternating waypoints: up by step_up, down by step_down, ``legs`` times (percent units)."""
    points, level, i = [(first, start)], start, first
    for leg in range(legs):
        i += spacing
        level += step_up if leg % 2 == 0 else -step_down
        points.append((i, level))
    return points


def bullish_trend(symbol):
    base = BASES[symbol]
    return session(symbol, DAY1, zigzag(0.0, 1.0, 0.45, 12) + [(77, 3.9)], base=base)[0]


def bearish_trend(symbol):
    base = BASES[symbol]
    return session(symbol, DAY1, zigzag(0.0, 0.45, 1.0, 12, spacing=6)[:1] +
                   [(i, -p) for i, p in zigzag(0.0, 1.0, 0.45, 12)[1:]] + [(77, -3.9)], base=base)[0]


def range_bound(symbol):
    points = [(0, 0.6)] + [(i, 1.2 if k % 2 == 0 else 0.0) for k, i in enumerate(range(6, 78, 6))]
    return session(symbol, DAY1, points, base=BASES[symbol])[0]


def gap_up_continuation(symbol):
    base = BASES[symbol]
    first, close1 = session(symbol, DAY1, zigzag(0.0, 0.6, 0.3, 12) + [(77, 1.9)], base=base)
    second, _ = session(symbol, DAY2, [(0, 0.2)] + zigzag(0.2, 0.9, 0.4, 12)[1:] + [(77, 3.6)], base=close1 * 1.02,
                        open_price=close1 * 1.02)
    return first + second


def gap_up_failure(symbol):
    base = BASES[symbol]
    first, close1 = session(symbol, DAY1, zigzag(0.0, 0.6, 0.3, 12) + [(77, 1.9)], base=base)
    fade = [(0, 0.3), (5, 0.6)] + [(i, -p) for i, p in zigzag(0.0, 0.9, 0.4, 11, first=5)[1:]] + [(77, -3.9)]
    second, _ = session(symbol, DAY2, fade, base=close1 * 1.02, open_price=close1 * 1.02)
    return first + second


def vwap_reclaim(symbol):
    points = [(0, 0.0), (6, -0.5), (10, -0.3), (16, -1.0), (20, -0.8), (28, -1.6), (34, -1.1), (40, -0.9), (46, -0.2),
              (50, -0.4), (58, 0.5), (62, 0.3), (70, 1.1), (77, 1.3)]
    return session(symbol, DAY1, points, base=BASES[symbol])[0]


def vwap_rejection(symbol):
    points = [(0, 0.0), (6, -0.5), (10, -0.3), (16, -1.0), (20, -0.8), (28, -1.6), (38, -0.4), (40, -0.6),
              (44, -0.5), (50, -1.4), (56, -1.1), (64, -2.2), (70, -1.9), (77, -2.8)]
    return session(symbol, DAY1, points, base=BASES[symbol])[0]


def ema_crossover(symbol):
    base = BASES[symbol]
    down = [(i, -p) for i, p in zigzag(0.0, 0.8, 0.35, 12)] + [(77, -3.2)]
    first, close1 = session(symbol, DAY1, down, base=base)
    second, _ = session(symbol, DAY2, zigzag(0.0, 0.9, 0.35, 12) + [(77, 3.8)], base=close1, open_price=close1)
    return first + second


def _range_then(symbol, tail, spikes):
    points = [(0, 0.5)] + [(i, 1.0 if k % 2 == 0 else 0.0) for k, i in enumerate(range(6, 55, 6))] + tail
    return session(symbol, DAY1, points, base=BASES[symbol], spikes=spikes)[0]


def breakout_high_volume(symbol):
    # Equal highs at +1.0% form resistance; bar 57 closes decisively above it on ~3x volume.
    return _range_then(symbol, [(56, 0.9), (57, 1.8), (63, 2.3), (67, 2.0), (77, 2.7)], {57: 3.2, 58: 2.0})


def false_breakout(symbol):
    # The same resistance; bar 57 breaks out, then closes back below the level two bars later.
    return _range_then(symbol, [(56, 0.9), (57, 1.8), (59, 0.6), (65, 0.2), (71, 0.8), (77, 0.3)], {57: 2.5})


SCENARIOS = dict(bullish_trend=bullish_trend, bearish_trend=bearish_trend, range=range_bound,
                 gap_up_continuation=gap_up_continuation, gap_up_failure=gap_up_failure, vwap_reclaim=vwap_reclaim,
                 vwap_rejection=vwap_rejection, ema_crossover=ema_crossover, breakout_high_volume=breakout_high_volume,
                 false_breakout=false_breakout)
