"""Series-level validation: one symbol/interval, strictly increasing, no duplicate timestamps."""
from market_data.models import MarketDataError


def validate_series(bars):
    """Return the bars as a tuple if they form a valid series; raise MarketDataError otherwise.

    Gaps (missing bars, weekends, halts) are allowed and never filled: indicators run
    on the bars that exist, and VWAP/gap logic keys on session dates, not bar counts.
    """
    bars = tuple(bars)
    if not bars:
        return bars
    symbol, interval = bars[0].symbol, bars[0].interval
    for index, bar in enumerate(bars):
        if bar.symbol != symbol or bar.interval != interval:
            raise MarketDataError(f"bar {index} mixes symbol or interval")
        if index and bar.timestamp == bars[index - 1].timestamp:
            raise MarketDataError(f"duplicate timestamp at bar {index}")
        if index and bar.timestamp < bars[index - 1].timestamp:
            raise MarketDataError(f"timestamps out of order at bar {index}")
    return bars
