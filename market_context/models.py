"""Immutable Market Context models (``context_format_version = "phase7b-v1"``): facts and numbers only.

There are deliberately no recommendation, state, score, confidence or
interpretive-label fields.

``to_dict()`` is deterministic:

- ``Decimal`` values use the MIAS canonical text (``format_decimal``: no exponent,
  no trailing zeros);
- datetimes and dates use ISO 8601;
- the float VWAP fields are plain JSON numbers;
- ``None`` stays ``None``.
"""
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from decimal import Decimal

from market_data.models import format_decimal

CONTEXT_FORMAT_VERSION = "phase7b-v1"
CONTEXT_INTERVAL = "5m"
BASES = ("prev_close", "open")

# Reason codes (explicit unavailability; values are never invented).
NO_SYMBOL_DATA = "no_symbol_data"
NO_BENCHMARK_DATA = "no_benchmark_data"
NO_PREVIOUS_SESSION = "no_previous_session"
ZERO_RANGE = "zero_range"
SELF = "self"
SESSION_MISMATCH = "session_mismatch"
MISALIGNED = "misaligned"
NO_DATA = "no_data"
NO_VWAP = "no_vwap"


def _plain(value):
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in sorted(value.items())}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


class _Plain:
    def to_dict(self):
        return {f.name: _plain(getattr(self, f.name)) for f in fields(self)}


@dataclass(frozen=True)
class SeriesInput:
    """One normalized input series: completed 5m ``MarketBar``s plus the provenance that freshness needs."""
    symbol: str
    interval: str
    bars: tuple
    provider_id: str
    delay_seconds: int


@dataclass(frozen=True)
class Freshness(_Plain):
    status: str                      # current | lagging | market_closed | no_data | unknown (no "stale" threshold)
    now: datetime
    as_of: datetime
    configured_delay_seconds: int
    interval: str
    latest_bar_end: datetime = None
    expected_latest_bar_end: datetime = None
    age_seconds: int = None
    lag_bars: int = None


@dataclass(frozen=True)
class SeriesContext(_Plain):
    symbol: str
    interval: str
    session_date: date = None
    bars_completed: int = 0
    bars_expected_so_far: int = None
    session_open: Decimal = None
    session_high: Decimal = None
    session_low: Decimal = None
    latest_close: Decimal = None
    latest_bar_start: datetime = None
    latest_bar_end: datetime = None
    previous_close: Decimal = None
    previous_close_session: date = None
    return_since_prev_close: Decimal = None
    return_since_open: Decimal = None
    session_range: Decimal = None
    position_in_range: Decimal = None
    distance_from_high: Decimal = None
    distance_from_low: Decimal = None
    session_volume: Decimal = None
    vwap: float = None
    close_vs_vwap: float = None
    freshness: Freshness = None
    unavailable: tuple = ()


@dataclass(frozen=True)
class BenchmarkComparison(_Plain):
    benchmark: str
    basis: str                        # prev_close | open
    cutoff: datetime = None
    symbol_return: Decimal = None
    benchmark_return: Decimal = None
    relative_return: Decimal = None   # symbol_return - benchmark_return (fractional; a fact, not a signal)
    aligned: bool = None
    symbol_bar_end: datetime = None
    benchmark_bar_end: datetime = None
    alignment_gap_seconds: int = None
    unavailable: tuple = ()


@dataclass(frozen=True)
class MarketContext(_Plain):
    context_format_version: str
    symbol: str
    now: datetime
    as_of: datetime
    calendar_state: str
    session_date: date
    symbol_context: SeriesContext
    benchmark_contexts: tuple
    comparisons: tuple
    provenance: dict = field(default_factory=dict)
