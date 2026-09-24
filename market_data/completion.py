"""Completed vs forming bars (Phase 4B completed-bar policy).

A bar is **completed** once its end (``ExchangeCalendar.bar_end``: start + interval,
truncated at the session segment end; the close for daily bars) is at or before
``as_of``. Anything later is still **forming**. The technical engine consumes
completed bars only, so no pivot, breakout or state is ever derived from a
forming bar.
"""


def split_completed(bars, as_of, calendar):
    """(completed, forming) with order preserved."""
    if as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    completed, forming = [], []
    for bar in bars:
        (completed if calendar.bar_end(bar) <= as_of else forming).append(bar)
    return completed, forming


def completed_bars(bars, as_of, calendar):
    return split_completed(bars, as_of, calendar)[0]
