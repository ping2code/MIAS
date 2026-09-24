"""Market data provider contract and a deterministic fixture provider.

Phase 4 shipped no live vendor adapter: the free Yahoo chart endpoints are
unofficial, with unclear licensing terms. Phase 4B adds the documented
Polygon.io/Massive aggregates adapter in ``market_data.providers.polygon``,
configured through ``market_data.config`` (see docs/phase4b-real-market-data.md).
"""
from abc import ABC, abstractmethod

from market_data.models import Interval, MarketDataError
from market_data.validation import validate_series


class MarketDataProvider(ABC):
    @abstractmethod
    def get_bars(self, symbol, interval, start, end):
        """Validated bars with ``start <= timestamp < end``, oldest first."""


class FixtureProvider(MarketDataProvider):
    """Serves pre-built bar series from memory (deterministic; for tests, replay and examples)."""

    def __init__(self, series):
        self._series = {}
        for bars in series:
            bars = validate_series(bars)
            if bars:
                key = (bars[0].symbol, bars[0].interval)
                if key in self._series:
                    raise MarketDataError(f"duplicate fixture series for {key[0]} {key[1].label}")
                self._series[key] = bars

    def get_bars(self, symbol, interval, start, end):
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise MarketDataError("start/end must be timezone-aware with start < end")
        bars = self._series.get((symbol, Interval.parse(interval)), ())
        return [bar for bar in bars if start <= bar.timestamp < end]
