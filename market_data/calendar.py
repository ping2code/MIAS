"""U.S. equity exchange calendar and calendar-aware session classification (Phase 4B).

Source: the maintained ``exchange_calendars`` package, calendar ``XNYS`` (New York
Stock Exchange). It provides trading days, holidays (including observed and
ad-hoc closures) and early closes; no holiday list is maintained in MIAS.

The calendar is loaded with fixed bounds (``FIRST_DATE``..``LAST_DATE``) so results
never depend on the day the process starts. Dates outside the bounds raise
``MarketDataError``. Dates after the last date the library knows about are rule-based
projections and cannot include future ad-hoc closures.

Sessions, all in America/New_York exchange time (daylight saving time via ``zoneinfo``):

| Session | Window on a trading day |
|---|---|
| ``pre`` | 04:00 to the regular open |
| ``regular`` | regular open (09:30) to the regular close (16:00, or 13:00 on an early-close day) |
| ``post`` | regular close to close + 4 hours (20:00, or 17:00 on an early-close day) |
| ``closed`` | anything else, and every moment of a non-trading day |

The extended-hours windows are a MIAS convention, matching common U.S. electronic
venue hours. The Phase 4 weekday-only helper ``market_data.models.classify_session``
is unchanged for compatibility. Calendar-aware code (providers, aggregation,
validation) uses this module instead.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import threading

from market_data.models import EXCHANGE_TZ, MarketDataError, Session

FIRST_DATE, LAST_DATE = date(2005, 1, 3), date(2035, 12, 31)
PRE_OPEN = time(4, 0)
POST_DURATION = timedelta(hours=4)


@dataclass(frozen=True)
class SessionTimes:
    day: date
    pre_open: datetime
    open: datetime
    close: datetime
    post_close: datetime

    @property
    def early_close(self):
        return self.close.timetz().replace(tzinfo=None) < time(16, 0)

    def segment(self, session):
        """[start, end) of one session segment on this trading day."""
        return {Session.PRE: (self.pre_open, self.open), Session.REGULAR: (self.open, self.close),
                Session.POST: (self.close, self.post_close)}[session]


class ExchangeCalendar:
    """Trading-day lookups over a fixed date range, cached per date (thread-safe for reads)."""

    def __init__(self, code="XNYS"):
        import exchange_calendars
        import pandas
        self.code = code
        self._pd = pandas
        self._calendar = exchange_calendars.get_calendar(code, start=FIRST_DATE.isoformat(), end=LAST_DATE.isoformat())
        self._cache, self._lock = {}, threading.Lock()

    def _check(self, day):
        if not isinstance(day, date) or isinstance(day, datetime):
            raise MarketDataError("calendar lookups take a date")
        if not FIRST_DATE <= day <= LAST_DATE:
            raise MarketDataError(f"date {day.isoformat()} is outside the calendar range {FIRST_DATE}..{LAST_DATE}")

    def session_times(self, day):
        """SessionTimes for a trading day, or None for weekends and holidays."""
        self._check(day)
        with self._lock:
            if day in self._cache:
                return self._cache[day]
        stamp = self._pd.Timestamp(day)
        times = None
        if self._calendar.is_session(stamp):
            open_ = self._calendar.session_open(stamp).to_pydatetime().astimezone(EXCHANGE_TZ)
            close = self._calendar.session_close(stamp).to_pydatetime().astimezone(EXCHANGE_TZ)
            times = SessionTimes(day, datetime.combine(day, PRE_OPEN, tzinfo=EXCHANGE_TZ), open_, close,
                                 close + POST_DURATION)
        with self._lock:
            self._cache[day] = times
        return times

    def is_trading_day(self, day):
        return self.session_times(day) is not None

    def previous_trading_day(self, day):
        """The last trading day strictly before ``day`` (``day`` itself need not be a trading day)."""
        candidate = day - timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate -= timedelta(days=1)
        return candidate

    def next_trading_day(self, day):
        candidate = day + timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def trading_days(self, start, end):
        """Trading days with start <= day <= end."""
        days, day = [], start
        while day <= end:
            if self.is_trading_day(day):
                days.append(day)
            day += timedelta(days=1)
        return days

    def classify(self, timestamp):
        """Session of an instant (pre, regular, post or closed)."""
        if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
            raise MarketDataError("timestamp must be timezone-aware")
        local = timestamp.astimezone(EXCHANGE_TZ)
        times = self.session_times(local.date())
        if times is None:
            return Session.CLOSED
        for session in (Session.PRE, Session.REGULAR, Session.POST):
            start, end = times.segment(session)
            if start <= local < end:
                return session
        return Session.CLOSED

    def bar_end(self, bar):
        """End of a bar: start + interval, truncated at its session segment end; daily bars end at the close."""
        if not bar.interval.intraday:
            times = self.session_times(bar.timestamp.astimezone(EXCHANGE_TZ).date())
            if times is None:
                raise MarketDataError(f"daily bar {bar.timestamp.isoformat()} is not on a trading day")
            return times.close
        session = self.classify(bar.timestamp)
        if session is Session.CLOSED:
            raise MarketDataError(f"bar {bar.timestamp.isoformat()} starts while the market is closed")
        times = self.session_times(bar.timestamp.astimezone(EXCHANGE_TZ).date())
        return min(bar.timestamp + bar.interval.delta, times.segment(session)[1])


_DEFAULT, _DEFAULT_LOCK = None, threading.Lock()


def default_calendar():
    """Process-wide XNYS calendar (loading takes a moment, so it is built once)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = ExchangeCalendar()
        return _DEFAULT
