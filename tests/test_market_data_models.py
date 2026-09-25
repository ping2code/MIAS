"""Phase 4 market data model: OHLCV validation, intervals, sessions, series validation and the fixture provider."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from market_data.models import EXCHANGE_TZ, Interval, MarketBar, MarketDataError, Session, classify_session, session_date
from market_data.provider import FixtureProvider, MarketDataProvider
from market_data.validation import validate_series

T0 = datetime(2026, 9, 21, 9, 30, tzinfo=EXCHANGE_TZ)


def bar(i=0, o="100", h="101", l="99", c="100.5", v=1000, symbol="META", interval="5m", ts=None):
    return MarketBar(symbol, ts or T0 + timedelta(minutes=5 * i), interval, o, h, l, c, v)


class MarketBarTests(unittest.TestCase):
    def test_valid_bar_normalizes_types(self):
        b = MarketBar("NVDA", T0, "5m", "180.10", 181, "179.5", Decimal("180.75"), 5)
        self.assertEqual((b.interval, b.open, b.high, b.session), (Interval.M5, Decimal("180.10"), Decimal(181), Session.REGULAR))
        self.assertAlmostEqual(b.typical_price, (181 + 179.5 + 180.75) / 3)
        self.assertEqual(b.to_dict()["close"], "180.75")
        self.assertTrue(b.regular)

    def test_invalid_ohlc_rejected(self):
        for kwargs in (dict(h="99.9"), dict(l="100.6"), dict(h="100.2"), dict(l="100.4", o="100.3"), dict(o="0"),
                       dict(l="-1"), dict(v=-1), dict(v=1.5), dict(v=True)):
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketDataError):
                bar(**kwargs)

    def test_fractional_volume_is_exact(self):
        b = bar(v=Decimal("1234.5678"))
        self.assertEqual(b.volume, Decimal("1234.5678"))
        self.assertEqual(bar(v="0.25").volume, Decimal("0.25"))
        self.assertEqual(bar(v=7).volume, 7)
        self.assertEqual(b.to_dict()["volume"], "1234.5678")
        self.assertEqual((bar(v="-0").volume, str(bar(v="-0").volume)), (0, "0"))  # Negative zero normalized.
        self.assertEqual(bar(v=0).volume, 0)
        self.assertEqual(bar(v=Decimal("1E+3")).to_dict()["volume"], "1000")      # No exponent in serialization.
        self.assertEqual(bar(v=Decimal("0.0000001")).to_dict()["volume"], "0.0000001")
        for bad in (Decimal("-0.1"), "NaN", "sNaN", "Infinity", "-Infinity", Decimal("Infinity"), "abc", "", None, 1.5,
                    True):
            with self.subTest(volume=bad), self.assertRaises(MarketDataError):
                bar(v=bad)

    def test_float_prices_and_non_finite_rejected(self):
        with self.assertRaises(MarketDataError):
            MarketBar("META", T0, "5m", 100.0, Decimal(101), Decimal(99), Decimal(100), 1)
        with self.assertRaises(MarketDataError):
            bar(h="NaN")
        with self.assertRaises(MarketDataError):
            bar(h="abc")

    def test_symbol_timestamp_interval_rules(self):
        with self.assertRaises(MarketDataError):
            bar(symbol="meta")
        with self.assertRaises(MarketDataError):
            bar(ts=datetime(2026, 9, 21, 9, 30))
        with self.assertRaises(MarketDataError):
            bar(interval="2m")
        self.assertEqual(bar(symbol="BRK.B").symbol, "BRK.B")

    def test_intervals(self):
        self.assertEqual([i.label for i in Interval], ["1m", "5m", "15m", "30m", "1h", "1d"])
        self.assertEqual(Interval.parse("H1"), Interval.H1)
        self.assertEqual(Interval.parse(Interval.D1), Interval.D1)
        self.assertEqual(Interval.M15.delta, timedelta(minutes=15))
        self.assertTrue(Interval.H1.intraday)
        self.assertFalse(Interval.D1.intraday)

    def test_sessions_use_exchange_time(self):
        self.assertEqual(classify_session(T0), Session.REGULAR)
        self.assertEqual(classify_session(T0 - timedelta(minutes=1)), Session.PRE)
        self.assertEqual(classify_session(T0.replace(hour=16)), Session.POST)
        self.assertEqual(classify_session(T0.replace(hour=15, minute=55)), Session.REGULAR)
        utc = datetime(2026, 9, 22, 3, 0, tzinfo=timezone.utc)  # 23:00 New York on 21 September.
        self.assertEqual(session_date(utc).isoformat(), "2026-09-21")
        self.assertEqual(bar(ts=datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc)).session, Session.REGULAR)

    def test_daily_bars_have_no_session(self):
        daily = bar(interval="1d", ts=datetime(2026, 9, 21, tzinfo=EXCHANGE_TZ))
        self.assertIsNone(daily.session)
        self.assertTrue(daily.regular)


class SeriesValidationTests(unittest.TestCase):
    def test_valid_series_and_gaps_allowed(self):
        bars = [bar(0), bar(1), bar(5)]
        self.assertEqual(validate_series(bars), tuple(bars))
        self.assertEqual(validate_series([]), ())

    def test_duplicates_ordering_and_mixing_rejected(self):
        with self.assertRaisesRegex(MarketDataError, "duplicate"):
            validate_series([bar(0), bar(1), bar(1)])
        with self.assertRaisesRegex(MarketDataError, "duplicate"):  # Same instant in another timezone.
            validate_series([bar(0), bar(ts=T0.astimezone(timezone.utc))])
        with self.assertRaisesRegex(MarketDataError, "out of order"):
            validate_series([bar(0), bar(2), bar(1)])
        with self.assertRaisesRegex(MarketDataError, "mixes"):
            validate_series([bar(0), bar(1, symbol="NVDA")])
        with self.assertRaisesRegex(MarketDataError, "mixes"):
            validate_series([bar(0), bar(1, interval="1m")])


class FixtureProviderTests(unittest.TestCase):
    def test_filters_half_open_window(self):
        provider = FixtureProvider([[bar(i) for i in range(5)], [bar(i, symbol="NVDA") for i in range(3)]])
        self.assertIsInstance(provider, MarketDataProvider)
        got = provider.get_bars("META", "5m", T0 + timedelta(minutes=5), T0 + timedelta(minutes=20))
        self.assertEqual([b.timestamp for b in got], [T0 + timedelta(minutes=m) for m in (5, 10, 15)])
        self.assertEqual(provider.get_bars("NVDA", Interval.M5, T0, T0 + timedelta(days=1)), [bar(i, symbol="NVDA") for i in range(3)])
        self.assertEqual(provider.get_bars("META", "1m", T0, T0 + timedelta(days=1)), [])

    def test_rejects_bad_input(self):
        with self.assertRaises(MarketDataError):
            FixtureProvider([[bar(0)], [bar(1)]])
        with self.assertRaises(MarketDataError):
            FixtureProvider([[bar(1), bar(0)]])
        provider = FixtureProvider([[bar(0)]])
        with self.assertRaises(MarketDataError):
            provider.get_bars("META", "5m", datetime(2026, 9, 21), T0)
        with self.assertRaises(MarketDataError):
            provider.get_bars("META", "5m", T0, T0)


if __name__ == "__main__":
    unittest.main()
