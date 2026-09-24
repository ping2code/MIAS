"""Phase 4B deterministic aggregation (never across sessions) and the completed-bar policy."""
from datetime import date, datetime, time, timedelta
import unittest

from market_data.aggregation import aggregate, derive_completed
from market_data.calendar import default_calendar
from market_data.completion import completed_bars, split_completed
from market_data.models import EXCHANGE_TZ, Interval, MarketDataError, Session
from tests.market_data_fakes import calendar_bars

CAL = default_calendar()
DAY, HALF = date(2026, 9, 23), date(2026, 11, 27)


def et(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=EXCHANGE_TZ)


class AggregationTests(unittest.TestCase):
    def check_ohlcv(self, source, out, target_minutes):
        for bucket in out:
            members = [b for b in source if bucket.timestamp <= b.timestamp < CAL.bar_end(bucket)]
            self.assertEqual(bucket.open, members[0].open)
            self.assertEqual(bucket.high, max(b.high for b in members))
            self.assertEqual(bucket.low, min(b.low for b in members))
            self.assertEqual(bucket.close, members[-1].close)
            self.assertEqual(bucket.volume, sum(b.volume for b in members))

    def test_one_minute_to_five_and_fifteen(self):
        source = calendar_bars("META", [DAY], 1)
        five, fifteen = aggregate(source, "5m", CAL), aggregate(source, "15m", CAL)
        self.assertEqual((len(source), len(five), len(fifteen)), (390, 78, 26))
        self.assertEqual(five[0].timestamp, et(DAY, 9, 30))
        self.check_ohlcv(source, five, 5)
        self.check_ohlcv(source, fifteen, 15)
        self.assertTrue(all(b.interval is Interval.M15 and b.session is Session.REGULAR for b in fifteen))

    def test_five_minute_to_session_anchored_hour(self):
        source = calendar_bars("NVDA", [DAY], 5)
        hours = aggregate(source, "1h", CAL)
        self.assertEqual([b.timestamp.time() for b in hours], [time(9, 30), time(10, 30), time(11, 30), time(12, 30),
                                                               time(13, 30), time(14, 30), time(15, 30)])
        self.assertEqual(CAL.bar_end(hours[-1]), et(DAY, 16))  # Truncated final hour.
        self.check_ohlcv(source, hours, 60)

    def test_half_day_hours(self):
        hours = aggregate(calendar_bars("NVDA", [HALF], 30), "1h", CAL)
        self.assertEqual([b.timestamp.time() for b in hours], [time(9, 30), time(10, 30), time(11, 30), time(12, 30)])
        self.assertEqual(CAL.bar_end(hours[-1]), et(HALF, 13))

    def test_never_crosses_sessions(self):
        source = calendar_bars("META", [DAY, date(2026, 9, 24)], 30, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        hours = aggregate(source, "1h", CAL)
        for bucket in hours:
            members = [b for b in source if bucket.timestamp <= b.timestamp < CAL.bar_end(bucket)]
            self.assertEqual({b.session for b in members}, {bucket.session})
            self.assertEqual({b.timestamp.date() for b in members}, {bucket.timestamp.date()})
        pre = [b for b in hours if b.session is Session.PRE and b.timestamp.date() == DAY]
        self.assertEqual(pre[-1].timestamp.time(), time(9))
        self.assertEqual(CAL.bar_end(pre[-1]), et(DAY, 9, 30))  # The 09:00 pre-market bucket stops at the open.

    def test_daily_from_regular_session_only(self):
        source = calendar_bars("META", [DAY, HALF], 30, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        daily = aggregate(source, "1d", CAL)
        self.assertEqual([b.timestamp for b in daily], [datetime.combine(d, time(0), tzinfo=EXCHANGE_TZ) for d in (DAY, HALF)])
        regular = [b for b in source if b.session is Session.REGULAR and b.timestamp.date() == HALF]
        self.assertEqual((daily[1].open, daily[1].close, daily[1].volume),
                         (regular[0].open, regular[-1].close, sum(b.volume for b in regular)))
        self.assertIsNone(daily[0].session)

    def test_missing_source_bars_are_not_invented(self):
        source = [b for b in calendar_bars("META", [DAY], 5) if b.timestamp.time() != time(10, 0)]
        hours = aggregate(source, "1h", CAL)
        self.assertEqual(len(hours), 7)
        self.assertEqual(hours[0].volume, sum(b.volume for b in source[:11]))

    def test_invalid_targets(self):
        source = calendar_bars("META", [DAY], 30)
        for target in ("5m", "30m", "45m"):
            with self.subTest(target=target), self.assertRaises(MarketDataError):
                aggregate(source, target, CAL)
        with self.assertRaises(MarketDataError):
            aggregate([source[1], source[0]], "1h", CAL)  # Out-of-order input is rejected, not re-sorted.
        daily = aggregate(source, "1d", CAL)
        with self.assertRaises(MarketDataError):
            aggregate(daily, "1d", CAL)
        self.assertEqual(aggregate([], "1h", CAL), [])


class CompletionTests(unittest.TestCase):
    def test_split_completed(self):
        bars = calendar_bars("META", [DAY], 5)
        done, forming = split_completed(bars, et(DAY, 10, 2), CAL)
        self.assertEqual(done[-1].timestamp, et(DAY, 9, 55))  # Ends 10:00 <= 10:02.
        self.assertEqual(forming[0].timestamp, et(DAY, 10))
        self.assertEqual(len(done) + len(forming), len(bars))
        self.assertEqual(len(completed_bars(bars, et(DAY, 16), CAL)), 78)
        with self.assertRaises(ValueError):
            completed_bars(bars, datetime(2026, 9, 23, 12), CAL)

    def test_derive_completed_respects_as_of_and_coverage(self):
        bars = calendar_bars("META", [DAY], 30)
        hours = derive_completed(bars, "1h", CAL, et(DAY, 11, 20))
        self.assertEqual([b.timestamp.time() for b in hours], [time(9, 30)])  # The 10:30 bucket ends 11:30 > 11:20.
        hours = derive_completed(bars, "1h", CAL, et(DAY, 11, 30))
        self.assertEqual([b.timestamp.time() for b in hours], [time(9, 30), time(10, 30)])
        coverage_short = derive_completed(bars[:4], "1h", CAL, et(DAY, 16))  # Source data ends at 11:30.
        self.assertEqual([b.timestamp.time() for b in coverage_short], [time(9, 30), time(10, 30)])
        daily_partial = derive_completed(bars, "1d", CAL, et(DAY, 15, 59))
        self.assertEqual(daily_partial, [])
        self.assertEqual(len(derive_completed(bars, "1d", CAL, et(DAY, 16))), 1)
        self.assertEqual(derive_completed([], "1h", CAL, et(DAY, 16)), [])


if __name__ == "__main__":
    unittest.main()
