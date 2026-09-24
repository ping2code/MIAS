"""Phase 4B exchange calendar: trading days, holidays, half-days, DST, sessions, validation, VWAP and gap rules."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import unittest

from market_data.calendar import FIRST_DATE, LAST_DATE, default_calendar
from market_data.models import EXCHANGE_TZ, MarketBar, MarketDataError, Session, classify_session
from market_data.validation import validate_calendar_series
from technical import indicators
from technical.engine import TechnicalEngine
from technical.incremental import IncrementalTechnicalEngine
from tests.market_data_fakes import calendar_bars

CAL = default_calendar()


def et(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=EXCHANGE_TZ)


def bar(stamp, interval="5m", price="100", volume=100, session=None, symbol="META"):
    p = Decimal(price)
    return MarketBar(symbol, stamp, interval, p, p + 1, p - 1, p, volume, session=session)


class TradingDayTests(unittest.TestCase):
    def test_normal_weekday(self):
        times = CAL.session_times(date(2026, 9, 23))
        self.assertEqual((times.open, times.close), (et(date(2026, 9, 23), 9, 30), et(date(2026, 9, 23), 16)))
        self.assertEqual((times.pre_open.time(), times.post_close.time()), (time(4), time(20)))
        self.assertFalse(times.early_close)
        self.assertTrue(CAL.is_trading_day(date(2026, 9, 23)))

    def test_weekend(self):
        self.assertIsNone(CAL.session_times(date(2026, 9, 26)))
        self.assertIsNone(CAL.session_times(date(2026, 9, 27)))
        self.assertEqual(CAL.classify(et(date(2026, 9, 26), 11)), Session.CLOSED)

    def test_us_market_holidays(self):
        for day in (date(2026, 1, 1), date(2026, 1, 19), date(2026, 4, 3), date(2026, 7, 3), date(2026, 9, 7),
                    date(2026, 11, 26), date(2026, 12, 25)):
            with self.subTest(day=day):
                self.assertFalse(CAL.is_trading_day(day))
                self.assertEqual(CAL.classify(et(day, 10)), Session.CLOSED)
        self.assertTrue(CAL.is_trading_day(date(2026, 7, 2)))  # 4 July 2026 is a Saturday: observed on Friday 3 July.

    def test_half_days(self):
        for day in (date(2026, 11, 27), date(2026, 12, 24), date(2025, 7, 3)):
            with self.subTest(day=day):
                times = CAL.session_times(day)
                self.assertTrue(times.early_close)
                self.assertEqual(times.close.time(), time(13))
                self.assertEqual(times.post_close.time(), time(17))
                self.assertEqual(CAL.classify(et(day, 12, 55)), Session.REGULAR)
                self.assertEqual(CAL.classify(et(day, 13)), Session.POST)
                self.assertEqual(CAL.classify(et(day, 17)), Session.CLOSED)

    def test_daylight_saving_transitions(self):
        before, after = CAL.session_times(date(2026, 3, 6)), CAL.session_times(date(2026, 3, 9))
        self.assertEqual(before.open.astimezone(timezone.utc).time(), time(14, 30))  # EST (UTC-5).
        self.assertEqual(after.open.astimezone(timezone.utc).time(), time(13, 30))   # EDT (UTC-4).
        fall_before, fall_after = CAL.session_times(date(2026, 10, 30)), CAL.session_times(date(2026, 11, 2))
        self.assertEqual(fall_before.close.astimezone(timezone.utc).time(), time(20))
        self.assertEqual(fall_after.close.astimezone(timezone.utc).time(), time(21))
        self.assertEqual(CAL.classify(datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc)), Session.REGULAR)
        self.assertEqual(CAL.classify(datetime(2026, 3, 6, 13, 30, tzinfo=timezone.utc)), Session.PRE)

    def test_previous_and_next_trading_day(self):
        self.assertEqual(CAL.previous_trading_day(date(2026, 9, 21)), date(2026, 9, 18))   # Monday -> Friday.
        self.assertEqual(CAL.previous_trading_day(date(2026, 9, 8)), date(2026, 9, 4))     # After Labor Day.
        self.assertEqual(CAL.previous_trading_day(date(2026, 11, 27)), date(2026, 11, 25))  # After Thanksgiving.
        self.assertEqual(CAL.previous_trading_day(date(2026, 9, 26)), date(2026, 9, 25))   # From a Saturday.
        self.assertEqual(CAL.next_trading_day(date(2026, 7, 2)), date(2026, 7, 6))
        self.assertEqual(len(CAL.trading_days(date(2026, 9, 1), date(2026, 9, 30))), 21)

    def test_sessions_and_bounds(self):
        day = date(2026, 9, 23)
        self.assertEqual([CAL.classify(et(day, h, m)) for h, m in ((3, 59), (4, 0), (9, 29), (9, 30), (15, 59), (16, 0),
                                                                   (19, 59), (20, 0))],
                         [Session.CLOSED, Session.PRE, Session.PRE, Session.REGULAR, Session.REGULAR, Session.POST,
                          Session.POST, Session.CLOSED])
        with self.assertRaises(MarketDataError):
            CAL.session_times(FIRST_DATE - timedelta(days=1))
        with self.assertRaises(MarketDataError):
            CAL.session_times(LAST_DATE + timedelta(days=1))
        with self.assertRaises(MarketDataError):
            CAL.classify(datetime(2026, 9, 23, 10))

    def test_phase4_weekday_helper_is_unchanged(self):
        self.assertEqual(classify_session(et(date(2026, 11, 26), 10)), Session.REGULAR)  # No calendar: weekday rules.
        self.assertEqual(classify_session(et(date(2026, 9, 23), 2)), Session.PRE)

    def test_bar_end(self):
        day, half = date(2026, 9, 23), date(2026, 11, 27)
        self.assertEqual(CAL.bar_end(bar(et(day, 10))), et(day, 10, 5))
        self.assertEqual(CAL.bar_end(bar(et(day, 15, 30), "1h")), et(day, 16))      # Truncated at the close.
        self.assertEqual(CAL.bar_end(bar(et(half, 12, 30), "1h")), et(half, 13))
        daily = bar(datetime.combine(half, time(0), tzinfo=EXCHANGE_TZ), "1d")
        self.assertEqual(CAL.bar_end(daily), et(half, 13))
        with self.assertRaises(MarketDataError):
            CAL.bar_end(bar(et(date(2026, 9, 7), 10)))


class CalendarValidationTests(unittest.TestCase):
    def test_valid_series(self):
        bars = calendar_bars("META", [date(2026, 11, 25), date(2026, 11, 27)], 5,
                             sessions=(Session.PRE, Session.REGULAR, Session.POST))
        self.assertEqual(len(validate_calendar_series(bars, CAL)), 16 * 12 + (5.5 + 3.5 + 4) * 12)

    def test_rejections(self):
        cases = dict(holiday=[bar(et(date(2026, 9, 7), 10))], overnight=[bar(et(date(2026, 9, 23), 2))],
                     label=[bar(et(date(2026, 9, 23), 10), session=Session.PRE)],
                     half_day_label=[bar(et(date(2026, 11, 27), 14), session=Session.REGULAR)],
                     clock_hour=[bar(et(date(2026, 9, 23), 10), "1h")],
                     daily_time=[bar(et(date(2026, 9, 23), 9, 30), "1d")],
                     daily_holiday=[bar(datetime.combine(date(2026, 9, 7), time(0), tzinfo=EXCHANGE_TZ), "1d")],
                     misaligned=[bar(et(date(2026, 9, 23), 10, 2))])
        for name, bars in cases.items():
            with self.subTest(case=name), self.assertRaises(MarketDataError):
                validate_calendar_series(bars, CAL)
        self.assertEqual(len(validate_calendar_series([bar(et(date(2026, 9, 23), 10, 30), "1h")], CAL)), 1)


class VwapCalendarTests(unittest.TestCase):
    def test_reset_across_weekend_and_holiday_and_half_day_post_excluded(self):
        days = [date(2026, 9, 4), date(2026, 9, 8), date(2026, 11, 27)]  # Friday, Tuesday after Labor Day, half-day.
        bars = calendar_bars("NVDA", days, 30, sessions=(Session.REGULAR, Session.POST))
        values = indicators.vwap(bars)
        for i, b in enumerate(bars):
            first_of_session = b.session is Session.REGULAR and (i == 0 or bars[i - 1].timestamp.date() != b.timestamp.date())
            if first_of_session:
                self.assertAlmostEqual(values[i], b.typical_price)  # No carry-over across the weekend/holiday.
            if b.session is Session.POST:
                self.assertIsNone(values[i])
        half = [b for b in bars if b.timestamp.date() == date(2026, 11, 27)]
        self.assertEqual(sum(b.session is Session.REGULAR for b in half), 7)  # 09:30-13:00.
        self.assertEqual(half[7].timestamp.time(), time(13))
        self.assertIs(half[7].session, Session.POST)


class GapCalendarTests(unittest.TestCase):
    def gap_at(self, bars, engine_cls, calendar, day):
        engine = engine_cls(calendar=calendar)
        snaps = engine.replay(bars) if engine_cls is TechnicalEngine else [engine.update(b) for b in bars]
        first = next(s for s, b in zip(snaps, bars) if b.timestamp.date() == day and b.session is Session.REGULAR)
        return first.gap_type, first.gap_percent

    def check(self, days, day, expected_prev_close_day):
        bars = calendar_bars("META", days, 30, sessions=(Session.REGULAR, Session.POST))
        prev = [b for b in bars if b.timestamp.date() == expected_prev_close_day and b.session is Session.REGULAR][-1]
        opening = next(b for b in bars if b.timestamp.date() == day)
        expected = round((float(opening.open) - float(prev.close)) / float(prev.close) * 100, 4)
        for engine_cls in (TechnicalEngine, IncrementalTechnicalEngine):
            with self.subTest(engine=engine_cls.__name__):
                self.assertEqual(self.gap_at(bars, engine_cls, CAL, day)[1], expected)

    def test_monday_vs_friday(self):
        self.check([date(2026, 9, 18), date(2026, 9, 21)], date(2026, 9, 21), date(2026, 9, 18))

    def test_post_holiday_open(self):
        self.check([date(2026, 9, 4), date(2026, 9, 8)], date(2026, 9, 8), date(2026, 9, 4))

    def test_half_day_prior_session_uses_regular_close_not_post_market(self):
        self.check([date(2026, 11, 27), date(2026, 11, 30)], date(2026, 11, 30), date(2026, 11, 27))

    def test_missing_previous_session_is_unknown_with_calendar(self):
        bars = calendar_bars("META", [date(2026, 9, 17), date(2026, 9, 21)], 30)  # Friday 18 September missing.
        for engine_cls in (TechnicalEngine, IncrementalTechnicalEngine):
            self.assertEqual(self.gap_at(bars, engine_cls, CAL, date(2026, 9, 21)), (None, None))
            self.assertIsNotNone(self.gap_at(bars, engine_cls, None, date(2026, 9, 21))[1])  # Phase 4 behaviour.

    def test_daily_gap_requires_previous_trading_day(self):
        def daily(day, open_, close):
            return MarketBar("META", datetime.combine(day, time(0), tzinfo=EXCHANGE_TZ), "1d", Decimal(open_),
                             Decimal(open_) + 5, Decimal(close) - 5, Decimal(close), 1000)
        bars = [daily(date(2026, 9, 4), "100", "100"), daily(date(2026, 9, 8), "103", "104"),
                daily(date(2026, 9, 10), "110", "110")]  # 9 September missing.
        snaps = TechnicalEngine(calendar=CAL).replay(bars)
        self.assertEqual((snaps[1].gap_type, snaps[1].gap_percent), ("gap_up", 3.0))
        self.assertEqual(snaps[2].gap_type, None)
        engine = IncrementalTechnicalEngine(calendar=CAL)
        self.assertEqual([engine.update(b).gap_type for b in bars], [s.gap_type for s in snaps])


if __name__ == "__main__":
    unittest.main()
