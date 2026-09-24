"""Phase 4B multi-timeframe snapshots: coexisting, independent, never fused, missing timeframes explicit."""
from dataclasses import fields
from datetime import date
import unittest

from market_data.aggregation import aggregate
from market_data.calendar import default_calendar
from market_data.models import MarketDataError
from technical.engine import TechnicalEngine
from technical.formatter import format_multi_timeframe
from technical.incremental import IncrementalTechnicalEngine
from technical.models import MultiTimeframeSnapshot
from technical.multitimeframe import analyze_timeframes, from_engine
from tests.market_data_fakes import calendar_bars
from tests.technical_fixtures import SCENARIOS

CAL = default_calendar()


def history(symbol="META"):
    days = CAL.trading_days(date(2025, 9, 1), date(2026, 9, 23))
    thirty = calendar_bars(symbol, days, 30, base=700.0)
    return {"5m": calendar_bars(symbol, days[-5:], 5, base=720.0), "1h": aggregate(thirty, "1h", CAL),
            "1d": aggregate(thirty, "1d", CAL)}


class MultiTimeframeTests(unittest.TestCase):
    def test_timeframes_coexist_and_match_independent_analysis(self):
        bars = history()
        multi = analyze_timeframes("META", bars, calendar=CAL)
        self.assertEqual(list(multi.timeframes), ["1d", "1h", "5m"])
        self.assertEqual(multi.missing, {})
        for label, snapshot in multi.timeframes.items():
            self.assertEqual(snapshot, TechnicalEngine(calendar=CAL).analyze(bars[label]), label)
            self.assertEqual(snapshot.interval, label)
        self.assertEqual(multi.timestamp, max(s.timestamp for s in multi.timeframes.values()))
        self.assertIsNotNone(multi.timeframes["1d"].ema["ema200"])

    def test_independent_states_are_preserved(self):
        bullish, bearish = SCENARIOS["bullish_trend"]("META"), SCENARIOS["bearish_trend"]("META")
        hourly = aggregate(bullish, "1h", CAL)
        multi = analyze_timeframes("META", {"5m": bearish, "1h": hourly}, timeframes=("1h", "5m"))
        self.assertEqual(multi.timeframes["5m"].signal.state, "bearish_setup")
        self.assertEqual(multi.timeframes["1h"].signal.state, TechnicalEngine().analyze(hourly).signal.state)
        self.assertNotEqual(multi.timeframes["5m"].signal.state, multi.timeframes["1h"].signal.state)

    def test_no_composite_score(self):
        self.assertEqual([f.name for f in fields(MultiTimeframeSnapshot)], ["symbol", "timestamp", "timeframes", "missing"])
        data = analyze_timeframes("META", {"5m": SCENARIOS["range"]("META")}, timeframes=("5m",)).to_dict()
        self.assertEqual(set(data), {"symbol", "timestamp", "timeframes", "missing"})
        text = format_multi_timeframe(analyze_timeframes("META", history(), calendar=CAL)).lower()
        for word in ("buy", "sell", "overall", "composite", "combined signal:"):
            self.assertNotIn(word, text)
        self.assertIn("no combined signal is produced", text)

    def test_missing_timeframe_is_explicit(self):
        multi = analyze_timeframes("NVDA", {"5m": SCENARIOS["range"]("NVDA")})
        self.assertEqual((multi.timeframes["1d"], multi.timeframes["1h"]), (None, None))
        self.assertEqual(multi.missing, {"1d": "no completed bars processed", "1h": "no completed bars processed"})
        text = format_multi_timeframe(multi)
        self.assertIn("Daily:\n  unavailable (no completed bars processed)", text)
        self.assertIn("5m:\n  State: range (confidence LOW)", text)
        empty = from_engine(IncrementalTechnicalEngine(), "NVDA")
        self.assertIsNone(empty.timestamp)
        self.assertIsNone(empty.to_dict()["timestamp"])

    def test_wrong_symbol_or_interval_rejected(self):
        with self.assertRaises(MarketDataError):
            analyze_timeframes("NVDA", {"5m": SCENARIOS["range"]("META")})
        with self.assertRaises(MarketDataError):
            analyze_timeframes("META", {"1h": SCENARIOS["range"]("META")})

    def test_formatter_layout(self):
        text = format_multi_timeframe(analyze_timeframes("META", history(), calendar=CAL))
        self.assertTrue(text.startswith("META\n\nDaily:\n  State: "))
        self.assertIn("\n1h:\n  State: ", text)
        self.assertIn("\n5m:\n  State: ", text)
        self.assertIn("  Structure: ", text)
        self.assertIn("  RSI: ", text)


if __name__ == "__main__":
    unittest.main()
