"""Phase 9D test cases (test-only): synthetic OptionsSnapshots and their expected OptionsIntelligence goldens.

Snapshots are built by the Phase 9B assembler from synthetic records (tests.options_snapshot_cases). The calendar
is a small stub (weekdays are trading days), so goldens do not depend on the exchange-calendar library; the real
XNYS calendar is exercised by the live replay tests.

Regenerate only on a deliberate rules/format change, and review the diff:

    python -m tests.options_intelligence_cases --regenerate
"""
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from options_data.canonical import canonical_json
from tests.options_snapshot_cases import AS_OF, BEFORE, chain_record, full_snapshot, live_snapshot, meta_chain, nvda_chain

FIXTURES = Path(__file__).parent / "fixtures" / "options_intelligence"
V2_FIXTURES = Path(__file__).parent / "fixtures" / "options_intelligence_v2"   # phase9-v2 (Phase 10 amendment)
PREVIOUS_SESSION = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
OLDER_SESSION = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
PRICE = dict(value="700", observed_at=BEFORE, source="synthetic_stocks")


class WeekdayCalendar:
    """previous_trading_day over weekdays only (2026-09-30 -> 2026-09-29)."""

    def previous_trading_day(self, day):
        candidate = day - timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate


CALENDAR = WeekdayCalendar()


def day(observed=None, volume=1200):
    values = dict(open="10", high="11.2", low="9.5", close="10.25", previous_close="9.8", change="0.45",
                  change_percent="4.59", volume=volume, vwap="10.4")
    if observed is not None:
        values["observed_at"] = observed
    return values


def quoted_chain():
    """Every quote state, strike relation, session relation and activity combination, with an underlying price."""
    q = lambda **kw: {"observed_at": BEFORE, **kw}
    return [
        chain_record(strike=690, quote=q(bid="10.1", ask="10.4", bid_size=5, ask_size=7), day=day(BEFORE, 500),
                     open_interest=dict(value=400)),                                       # complete, below, vol>oi
        chain_record(strike=700, quote=q(bid="10.3", ask="10.3"), day=day(PREVIOUS_SESSION)),  # locked, equal
        chain_record(strike=710, quote=q(bid="10.5", ask="10.2"), day=day(OLDER_SESSION)),     # crossed, above
        chain_record(strike=720, quote=q(ask="0.05"), day=day(BEFORE, 0)),                      # bid_missing, zero vol
        chain_record(strike=730, quote=q(bid="0.01"), day=False, open_interest=dict(value=0)),  # ask_missing
        chain_record(strike=740, quote=q(bid="0", ask="0"), greeks=dict(delta="1.3", gamma="-0.01", theta="-0.2",
                                                                         vega="0.3")),       # locked zero mid
        chain_record(strike=700, option_type="put", quote=q(bid="9.9", ask="10.2"), day=day(BEFORE, 800),
                     implied_volatility=dict(value="0.39"), greeks=dict(delta="-0.45", gamma="0.01", theta="-0.3",
                                                                        vega="0.9", rho="-0.12")),
        chain_record(strike=700, option_type="put", expiration="2026-09-30", day=day(BEFORE, 300), iv=False,
                     greeks=False),                                                            # both_missing, 0 DTE
        chain_record(strike=750, quote=q(bid="1", ask="1.2", observed_at=AS_OF + timedelta(seconds=1))),  # excluded
    ]


CASES = {
    "live_like_meta": lambda: live_snapshot(meta_chain()),
    "live_like_nvda": lambda: live_snapshot(nvda_chain(), underlying="NVDA"),
    "quoted_with_price": lambda: full_snapshot(quoted_chain(), underlying_price=PRICE, calendar_state="regular"),
    "empty_chain": lambda: live_snapshot([]),
}


def snapshot(name):
    return CASES[name]()


def market_intelligence():
    """A real Phase 8 MarketIntelligence for META (as_of 2026-09-23T20:05Z, before the snapshot)."""
    from market_intelligence.builder import build
    from tests import market_intelligence_cases
    return build(market_intelligence_cases.synthesis("meta_real_shaped"))


def intelligence_path(name):
    return FIXTURES / f"{name}.intelligence.json"


def names():
    return sorted(CASES)


def intelligence_v2_path(name):
    return V2_FIXTURES / f"{name}.intelligence.json"


def regenerate_v2():
    """phase9-v2 goldens from the same snapshots (the v1 goldens are never rewritten by this)."""
    from options_intelligence.builder import build
    V2_FIXTURES.mkdir(parents=True, exist_ok=True)
    for name in names():
        intelligence_v2_path(name).write_text(
            canonical_json(build(snapshot(name), calendar=CALENDAR, format="phase9-v2").to_dict()) + "\n",
            encoding="utf-8")


def regenerate():
    from options_intelligence.builder import build
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name in names():
        intelligence_path(name).write_text(canonical_json(build(snapshot(name), calendar=CALENDAR).to_dict()) + "\n",
                                           encoding="utf-8")
    with_mi = build(snapshot("live_like_meta"), market_intelligence(), calendar=CALENDAR)
    intelligence_path("live_like_meta_with_mi").write_text(canonical_json(with_mi.to_dict()) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m tests.options_intelligence_cases")
    parser.add_argument("--regenerate", action="store_true", required=True)
    parser.add_argument("--v2-only", action="store_true", help="write only the phase9-v2 goldens")
    args = parser.parse_args()
    if not args.v2_only:
        regenerate()
    regenerate_v2()
