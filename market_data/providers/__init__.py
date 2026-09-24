"""Concrete market data providers and the settings-driven factory (Phase 4B)."""
from market_data.config import MarketDataConfigError


def build_provider(settings, *, calendar=None, session=None, sleep=None, clock=None):
    """The provider named by ``settings.provider``; ``none`` is a configuration error for callers that need data."""
    if settings.provider == "polygon":
        from market_data.providers.polygon import PolygonProvider
        return PolygonProvider(settings, calendar=calendar, session=session, sleep=sleep, clock=clock)
    raise MarketDataConfigError("MARKET_DATA_PROVIDER is not configured (set it to polygon)")
