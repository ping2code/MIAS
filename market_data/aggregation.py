"""Deterministic bar aggregation that never crosses a session boundary (Phase 4B).

- open = first bar's open; high = max; low = min; close = last bar's close;
  volume = sum.
- **Intraday targets:** buckets are anchored at their session segment's start (04:00
  pre, the regular open, the regular close for post), so hourly regular-session
  buckets are 09:30, 10:30, … 15:30. The last bucket is truncated at the segment end
  (15:30-16:00; 12:30-13:00 on an early-close day). Bars from different segments or
  trading days never share a bucket.
- **Daily target:** one bar per trading day from **regular-session** bars only,
  stamped at midnight exchange time.
- The target must be a whole multiple of the source interval. Missing source bars
  are not invented; a bucket aggregates whatever source bars exist.
- Completeness is not decided here: callers apply ``market_data.completion`` with a
  data-coverage cut-off (see ``MarketDataProvider`` implementations).
"""
from datetime import datetime, timedelta

from market_data.models import EXCHANGE_TZ, Interval, MarketBar, MarketDataError, Session
from market_data.validation import validate_series


def aggregate(bars, target, calendar):
    target = Interval.parse(target)
    bars = validate_series(bars)
    if not bars:
        return []
    source = bars[0].interval
    if not source.intraday:
        raise MarketDataError("only intraday bars can be aggregated")
    if target.seconds <= source.seconds or (target.intraday and target.seconds % source.seconds):
        raise MarketDataError(f"cannot aggregate {source.label} into {target.label}")
    groups, order = {}, []
    for bar in bars:
        local = bar.timestamp.astimezone(EXCHANGE_TZ)
        session = calendar.classify(bar.timestamp)
        if session is Session.CLOSED:
            raise MarketDataError(f"bar {local.isoformat()} is outside every trading session")
        if not target.intraday:
            if session is not Session.REGULAR:
                continue
            key = (local.date(), Session.REGULAR, datetime.combine(local.date(), datetime.min.time(), tzinfo=EXCHANGE_TZ))
        else:
            start, _ = calendar.session_times(local.date()).segment(session)
            offset = int((local - start).total_seconds()) // target.seconds * target.seconds
            key = (local.date(), session, start + timedelta(seconds=offset))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(bar)
    out = []
    for key in order:
        members = groups[key]
        out.append(MarketBar(members[0].symbol, key[2], target, members[0].open, max(b.high for b in members),
                             min(b.low for b in members), members[-1].close, sum(b.volume for b in members),
                             session=key[1] if target.intraday else None))
    return out



def derive_completed(source_bars, target, calendar, as_of):
    """Aggregate **completed** source bars and keep only completed buckets.

    A bucket is complete when its end (``calendar.bar_end``) is at or before both
    ``as_of`` and the source data's coverage (the latest completed source bar's end).
    So a bucket whose last source bars have not arrived yet stays forming. A final
    bucket whose last source minutes had no trades becomes complete once a later
    source bar extends the coverage.
    """
    from market_data.completion import completed_bars
    source = completed_bars(source_bars, as_of, calendar)
    if not source:
        return []
    cutoff = min(as_of, max(calendar.bar_end(b) for b in source))
    return [b for b in aggregate(source, target, calendar) if calendar.bar_end(b) <= cutoff]
