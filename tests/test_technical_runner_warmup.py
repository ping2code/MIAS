"""Phase 4C session-anchored warm-up windows: deterministic snapshots across runs (idempotent persistence)."""
from datetime import date, datetime, time
import unittest

from market_data.calendar import default_calendar
from market_data.config import load_market_data_settings
from market_data.models import EXCHANGE_TZ, Interval
from market_data.providers.polygon import PolygonProvider
from persistence.technical_snapshot_repository import snapshot_row
from technical.runner import WARMUP_SESSIONS, reference_day, run_symbol, warmup_start
from tests.market_data_fakes import TEST_KEY, FakeSession, aggregates_route, calendar_bars

CAL = default_calendar()
DAYS = CAL.trading_days(date(2025, 1, 2), date(2026, 9, 28))
DATA = {5: calendar_bars("META", DAYS[-15:], 5, base=740.0), 30: calendar_bars("META", DAYS, 30, base=740.0)}


def et(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=EXCHANGE_TZ)


def provider(now):
    settings = load_market_data_settings(dict(MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY,
                                              MARKET_DATA_DELAY_SECONDS="0"))
    return PolygonProvider(settings, calendar=CAL, session=FakeSession(route=aggregates_route(DATA)),
                           sleep=lambda s: None, clock=lambda: now)


class ReferenceDayTests(unittest.TestCase):
    def test_reference_day_rules(self):
        wed, thu = date(2026, 9, 23), date(2026, 9, 24)
        self.assertEqual(reference_day(CAL, et(thu, 9, 34), Interval.M5), wed)   # First 5m bar not complete yet.
        self.assertEqual(reference_day(CAL, et(thu, 9, 35), Interval.M5), thu)
        self.assertEqual(reference_day(CAL, et(thu, 10, 29), Interval.H1), wed)
        self.assertEqual(reference_day(CAL, et(thu, 10, 30), Interval.H1), thu)
        self.assertEqual(reference_day(CAL, et(thu, 15, 59), Interval.D1), wed)  # Daily bar completes at the close.
        self.assertEqual(reference_day(CAL, et(thu, 16), Interval.D1), thu)
        self.assertEqual(reference_day(CAL, et(date(2026, 9, 26), 12), Interval.M5), date(2026, 9, 25))  # Saturday.
        half = date(2026, 11, 27)
        self.assertEqual(reference_day(CAL, et(half, 13), Interval.D1), half)  # Early close.

    def test_window_length_in_sessions(self):
        now = et(date(2026, 9, 23), 20)
        for interval, sessions in WARMUP_SESSIONS.items():
            start = warmup_start(CAL, now, interval)
            self.assertEqual(start.time(), time(0))
            self.assertEqual(len(CAL.trading_days(start.date(), date(2026, 9, 23))), sessions, interval.label)


class DeterminismTests(unittest.TestCase):
    def rows(self, now):
        p = provider(now)
        multi, context = run_symbol(p, "META", [Interval.D1, Interval.H1, Interval.M5])
        return {label: snapshot_row(s, provider="polygon", engine_version="phase4c-v2", provider_delay_seconds=0,
                                    warmup_start=context[label]["warmup_start"], warmup_bars=context[label]["bars"])
                for label, s in multi.timeframes.items()}

    def test_same_latest_bar_gives_identical_snapshots_across_runs_and_midnight(self):
        evening = self.rows(et(date(2026, 9, 23), 17))
        late = self.rows(et(date(2026, 9, 23), 23, 59))
        after_midnight = self.rows(et(date(2026, 9, 24), 0, 30))
        pre_open = self.rows(et(date(2026, 9, 24), 9, 30))
        for label in ("1d", "1h", "5m"):
            hashes = {r[label]["content_hash"] for r in (evening, late, after_midnight, pre_open)}
            self.assertEqual(len(hashes), 1, label)  # Same bar, same window: persistence stays idempotent.

    def test_new_bar_moves_the_window_forward(self):
        before = self.rows(et(date(2026, 9, 24), 9, 30))
        after = self.rows(et(date(2026, 9, 24), 9, 35))
        self.assertNotEqual(before["5m"]["snapshot_timestamp"], after["5m"]["snapshot_timestamp"])
        self.assertGreater(after["5m"]["warmup_start"], before["5m"]["warmup_start"])
        self.assertEqual(before["1d"]["content_hash"], after["1d"]["content_hash"])  # Daily reference unchanged.


if __name__ == "__main__":
    unittest.main()
