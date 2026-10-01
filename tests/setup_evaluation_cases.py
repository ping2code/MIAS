"""Phase 11 test fixtures (test-only): a sealed Phase 10 setup, sealed later OptionsSnapshots, sealed
SessionSchedules from the real XNYS calendar, a test protocol and Phase 10D InvalidationChecks. All synthetic or
replay. Nothing here is a production protocol or a prospective observation."""
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from options_data.normalization import assemble
from tests.options_snapshot_cases import PROVENANCE, chain_record
from tests import trade_setup_cases as ts

Z = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
ASSESSMENT_AS_OF = Z(2026, 9, 30, 14, 45)          # Wednesday 10:45 ET (the Phase 10 replay setup)
SESSION_1_CLOSE = Z(2026, 10, 1, 20, 0)             # Thursday 16:00 ET
SESSION_5_CLOSE = Z(2026, 10, 7, 20, 0)             # the following Wednesday
WINDOW_START = SESSION_1_CLOSE - timedelta(minutes=30)
INSIDE = SESSION_1_CLOSE - timedelta(minutes=10)

# Candidate records (all calls, bullish setup): strike -> extra chain_record arguments.
CANDIDATES = {
    690: {}, 700: {}, 710: dict(shares=None),                       # 710: no multiplier
    720: dict(quote=ts.quote("0", "0")),                            # locked zero quote: entry reference 0
    730: dict(expiration="2026-10-02"),                             # expires before session_5
}


def record(strike, **kw):
    extra = dict(CANDIDATES[strike], **kw)
    expiration = extra.pop("expiration", ts.EXPIRY)
    return ts.record(strike, expiration=expiration, **extra)


@lru_cache(maxsize=None)
def _setup():
    assessment, _ = ts.screened([record(s) for s in CANDIDATES], allow_locked_quote=True)
    return assessment.to_dict()


def setup():
    from copy import deepcopy
    return deepcopy(_setup())


def schedule(start=date(2026, 9, 28), end=date(2026, 12, 31)):
    from market_data.calendar import default_calendar
    from setup_evaluation.schedule import make_schedule
    cal = default_calendar()
    return make_schedule([(d, cal.session_times(d).open, cal.session_times(d).close)
                          for d in cal.trading_days(start, end)]).to_dict()


def protocol():
    from setup_evaluation.protocol import make_test_protocol
    return make_test_protocol().to_dict()


def quote_at(bid="10.5", ask="10.7", observed_at=INSIDE, **kw):
    fields = dict(observed_at=observed_at, bid=bid, ask=ask, **kw)
    return {k: v for k, v in fields.items() if v is not None}


def snapshot(quotes=None, as_of=INSIDE, truncated=False, calendar_state="regular", underlying="META", extra=(),
             shares=None):
    """A sealed later OptionsSnapshot. ``quotes``: strike -> quote group (False: contract absent; None: no quote).
    Defaults to a fresh two-sided quote for every candidate except the expiring one (gone after expiry)."""
    quotes = {**{s: quote_at() for s in CANDIDATES if s != 730}, **(quotes or {})}
    records = []
    for strike, q in quotes.items():
        if q is False:
            continue
        kw = {} if q is None else dict(quote=q)
        if shares and strike in shares:
            kw["shares"] = shares[strike]
        rec = record(strike, **kw)
        if q is None:
            rec.pop("quote", None)
        records.append(rec)
    records.extend(extra)
    return assemble(underlying, as_of, records, provenance=dict(PROVENANCE, truncated=truncated),
                    calendar_state=calendar_state).to_dict()


def check(name="all_bearish", seconds=86400):
    """A Phase 10D InvalidationCheck of the setup against a later real Phase 8 MI (resealed later)."""
    from trade_setup.invalidation import check_invalidation
    return check_invalidation(setup(), ts.later_mi(name, seconds=seconds)).to_dict()
