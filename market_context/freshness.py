"""Explicit data-freshness metadata for a 5m context series (Phase 7B.1). It never changes any metric.

**Inputs:** ``now``; ``as_of`` (the data cut-off, i.e. now minus the configured
provider delay); the configured delay; the interval; the XNYS calendar; the
series' latest completed regular bar end.

**Expected latest bar end:** the latest regular-session grid bar end at or before
``as_of``.

- Grid bars start at the regular open and step by the interval.
- The last bar is truncated at the close (``ExchangeCalendar.bar_end``
  semantics), so early closes come from the calendar.
- Before today's first bar completes, it is the previous session's close.

**Lag:** ``lag_bars`` is the number of grid bar ends in ``(latest_bar_end,
expected_latest_bar_end]``, counted across sessions.

**Status:**

| Status | Rule |
|---|---|
| ``no_data`` | no completed regular bar |
| ``current`` | ``lag_bars == 0`` and ``now`` is inside the regular session |
| ``market_closed`` | ``lag_bars == 0`` and ``now`` is pre-market, post-market or closed (the series ends at its session's last expected bar) |
| ``lagging`` | ``lag_bars >= 1`` |
| ``unknown`` | the latest bar is off the regular grid or after the expected end, or the lag spans more than ``MAX_SCAN_DAYS`` |

There is deliberately no ``stale`` threshold, and no vendor-specific delay is
assumed: the delay is the configured value only.
"""
from market_data.models import EXCHANGE_TZ, Interval, Session

MAX_SCAN_DAYS = 40


def grid_ends(day, interval, calendar):
    """Regular-session bar ends for one trading day (the last one truncated at the close); [] on non-trading days."""
    times = calendar.session_times(day)
    if times is None:
        return []
    step, ends, start = Interval.parse(interval).delta, [], times.open
    while start < times.close:
        ends.append(min(start + step, times.close))
        start += step
    return ends


def expected_latest_bar_end(as_of, interval, calendar):
    day = as_of.astimezone(EXCHANGE_TZ).date()
    for _ in range(MAX_SCAN_DAYS):
        ends = [e for e in grid_ends(day, interval, calendar) if e <= as_of]
        if ends:
            return ends[-1]
        day = calendar.previous_trading_day(day)
    return None


def count_lag(latest_end, expected_end, interval, calendar):
    """Grid bar ends in (latest_end, expected_end]; None when the span exceeds ``MAX_SCAN_DAYS`` trading days."""
    day, last_day = latest_end.astimezone(EXCHANGE_TZ).date(), expected_end.astimezone(EXCHANGE_TZ).date()
    count = 0
    for _ in range(MAX_SCAN_DAYS):
        count += sum(1 for e in grid_ends(day, interval, calendar) if latest_end < e <= expected_end)
        if day >= last_day:
            return count
        day = calendar.next_trading_day(day)
    return None


def freshness(*, latest_bar_end, now, as_of, delay_seconds, interval, calendar):
    from market_context.models import Freshness
    expected = expected_latest_bar_end(as_of, interval, calendar)
    base = dict(now=now, as_of=as_of, configured_delay_seconds=int(delay_seconds), interval=interval,
                expected_latest_bar_end=expected)
    if latest_bar_end is None:
        return Freshness(status="no_data", **base)
    base.update(latest_bar_end=latest_bar_end, age_seconds=int((now - latest_bar_end).total_seconds()))
    on_grid = latest_bar_end in grid_ends(latest_bar_end.astimezone(EXCHANGE_TZ).date(), interval, calendar)
    lag = count_lag(latest_bar_end, expected, interval, calendar) if expected and on_grid and \
        latest_bar_end <= expected else None
    if lag is None:
        return Freshness(status="unknown", **base)
    if lag:
        return Freshness(status="lagging", lag_bars=lag, **base)
    status = "current" if calendar.classify(now) is Session.REGULAR else "market_closed"
    return Freshness(status=status, lag_bars=0, **base)
