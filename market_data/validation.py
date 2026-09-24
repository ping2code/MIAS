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


def validate_calendar_series(bars, calendar):
    """``validate_series`` plus calendar/session checks for externally sourced bars (Phase 4B).

    Each problem raises ``MarketDataError`` naming the bar; nothing is repaired:

    - an intraday bar must start inside a pre, regular or post segment of a trading day
      (never on a holiday, weekend or overnight);
    - a bar that carries a session label must agree with the calendar;
    - an intraday bar must sit on its segment's grid: ``(start - segment_start)`` must
      be a whole number of intervals. This rejects bars that cannot map to one session,
      such as a clock-aligned 10:00 hourly bar whose 09:00 sibling would span the 09:30
      open;
    - a daily bar must be stamped at midnight exchange time on a trading day.
    """
    from market_data.models import EXCHANGE_TZ, Session
    bars = validate_series(bars)
    for index, bar in enumerate(bars):
        local = bar.timestamp.astimezone(EXCHANGE_TZ)
        times = calendar.session_times(local.date())
        if not bar.interval.intraday:
            if times is None:
                raise MarketDataError(f"bar {index}: daily bar on non-trading day {local.date().isoformat()}")
            if local.time() != local.time().min:
                raise MarketDataError(f"bar {index}: daily bar must be stamped at midnight exchange time")
            continue
        session = calendar.classify(bar.timestamp)
        if session is Session.CLOSED:
            raise MarketDataError(f"bar {index}: {local.isoformat()} is outside every trading session")
        if bar.session is not None and bar.session is not session:
            raise MarketDataError(f"bar {index}: session label {bar.session.value} disagrees with calendar {session.value}")
        start, _ = times.segment(session)
        if (local - start).total_seconds() % bar.interval.seconds:
            raise MarketDataError(f"bar {index}: {local.isoformat()} is not aligned to the {session.value} session grid")
    return bars
