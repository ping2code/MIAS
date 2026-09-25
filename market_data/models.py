"""Normalized OHLCV (Open, High, Low, Close, Volume) bars and generic intervals.

Prices and volume are ``Decimal`` so bar data keeps exact vendor precision. Volume
may be fractional: vendor aggregates include fractional-share trades (observed in the
Phase 4C live check), so a non-negative fractional volume is valid data, never
rounded. Indicator math
converts to ``float``: IEEE-754 double precision has ~15-16 significant digits,
so rounding error for prices in the tens to thousands of dollars is around
1e-12. That is ten orders of magnitude below a $0.01 tick, and indicator outputs
are descriptive, never order prices.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import Enum
import re
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("America/New_York")
REGULAR_OPEN, REGULAR_CLOSE = time(9, 30), time(16, 0)  # U.S. equities regular trading session.
SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


class MarketDataError(ValueError):
    """Invalid market data (the message names the problem, never a vendor payload)."""


class Interval(Enum):
    """Generic bar intervals: name -> length in seconds (no symbol-specific handling)."""
    M1 = ("1m", 60)
    M5 = ("5m", 300)
    M15 = ("15m", 900)
    M30 = ("30m", 1800)
    H1 = ("1h", 3600)
    D1 = ("1d", 86400)

    def __init__(self, label, seconds):
        self.label, self.seconds = label, seconds

    @property
    def intraday(self):
        return self.seconds < 86400

    @property
    def delta(self):
        return timedelta(seconds=self.seconds)

    @classmethod
    def parse(cls, value):
        for interval in cls:
            if value in (interval, interval.label, interval.name):
                return interval
        raise MarketDataError(f"Unsupported interval: {value!r}")


class Session(str, Enum):
    REGULAR = "regular"
    PRE = "pre"
    POST = "post"
    CLOSED = "closed"  # Phase 4B: set only by calendar-aware classification (market_data.calendar).


def classify_session(timestamp):
    """Session of an intraday bar from its start time in exchange time (weekday rules only; no holiday calendar)."""
    local = timestamp.astimezone(EXCHANGE_TZ).time()
    if REGULAR_OPEN <= local < REGULAR_CLOSE:
        return Session.REGULAR
    return Session.PRE if local < REGULAR_OPEN else Session.POST


def session_date(timestamp):
    """Trading date of a bar in exchange time; the session key for VWAP resets and gaps."""
    return timestamp.astimezone(EXCHANGE_TZ).date()


def format_decimal(value):
    """Canonical fixed-point text for a Decimal: no exponent, no trailing zeros ("1E+3" -> "1000", "0.2500" -> "0.25").

    Used wherever a Decimal is serialized or hashed, so the text never depends on the
    value's internal scale and never uses scientific notation.
    """
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "0") else text


def _decimal(name, value):
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise MarketDataError(f"{name} must be a Decimal, int or numeric string (not float)")
    try:
        result = Decimal(value)
    except Exception:
        raise MarketDataError(f"{name} is not a number") from None
    if not result.is_finite():
        raise MarketDataError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class MarketBar:
    """One OHLCV bar. ``timestamp`` is the bar start (timezone-aware). ``session`` is derived when not given."""
    symbol: str
    timestamp: datetime
    interval: Interval
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal  # Non-negative; int, Decimal or numeric string accepted (never float), stored as Decimal.
    session: Session = None

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not SYMBOL.fullmatch(self.symbol):
            raise MarketDataError("symbol must be an upper-case ticker")
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise MarketDataError("timestamp must be timezone-aware")
        object.__setattr__(self, "interval", Interval.parse(self.interval))
        for name in ("open", "high", "low", "close"):
            value = _decimal(name, getattr(self, name))
            if value <= 0:
                raise MarketDataError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        volume = _decimal("volume", self.volume)
        if volume < 0:
            raise MarketDataError("volume must be non-negative")
        object.__setattr__(self, "volume", abs(volume) if volume == 0 else volume)  # "-0" becomes 0.
        if self.high < max(self.open, self.close, self.low):
            raise MarketDataError("high must be >= open, close and low")
        if self.low > min(self.open, self.close, self.high):
            raise MarketDataError("low must be <= open, close and high")
        session = self.session
        if session is None and self.interval.intraday:
            session = classify_session(self.timestamp)
        elif session is not None:
            session = Session(session)
        object.__setattr__(self, "session", session)

    @property
    def typical_price(self):
        return (float(self.high) + float(self.low) + float(self.close)) / 3.0

    @property
    def regular(self):
        """True for regular-session intraday bars and for all daily bars."""
        return not self.interval.intraday or self.session is Session.REGULAR

    def to_dict(self):
        return dict(symbol=self.symbol, timestamp=self.timestamp.isoformat(), interval=self.interval.label,
                    open=format_decimal(self.open), high=format_decimal(self.high), low=format_decimal(self.low),
                    close=format_decimal(self.close), volume=format_decimal(self.volume),
                    session=self.session.value if self.session else None)
