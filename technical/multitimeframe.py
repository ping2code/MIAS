"""Side-by-side multi-timeframe analysis (Phase 4B).

Each timeframe runs through its own engine state and keeps its own state and
confidence. There is no fusion: 1d does not override 5m, there is no weighted
score, and nothing is BUY or SELL. A timeframe without completed bars is reported
explicitly as missing, with a reason; it is never silently dropped.
"""
from market_data.models import Interval, MarketDataError
from technical.incremental import IncrementalTechnicalEngine
from technical.models import MultiTimeframeSnapshot

DEFAULT_TIMEFRAMES = ("1d", "1h", "5m")


def from_engine(engine, symbol, timeframes=DEFAULT_TIMEFRAMES):
    """Latest snapshots already held by an incremental engine."""
    labels = [Interval.parse(t).label for t in timeframes]
    snaps = {label: engine.latest(symbol, label) for label in labels}
    missing = {label: "no completed bars processed" for label, snap in snaps.items() if snap is None}
    stamps = [snap.timestamp for snap in snaps.values() if snap is not None]
    return MultiTimeframeSnapshot(symbol, max(stamps) if stamps else None, snaps, missing)


def analyze_timeframes(symbol, bars_by_interval, *, timeframes=DEFAULT_TIMEFRAMES, config=None, calendar=None):
    """Warm a fresh engine per timeframe from completed bars; ``bars_by_interval`` maps interval -> bars."""
    engine = IncrementalTechnicalEngine(config, calendar)
    wanted = {Interval.parse(t) for t in timeframes}
    for interval, bars in bars_by_interval.items():
        interval = Interval.parse(interval)
        if interval not in wanted:
            continue
        for bar in bars:
            if bar.symbol != symbol or bar.interval != interval:
                raise MarketDataError(f"{interval.label} series contains a bar for {bar.symbol} {bar.interval.label}")
        engine.warmup(bars)
    return from_engine(engine, symbol, timeframes)
