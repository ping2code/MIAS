"""Phase 4B incremental engine: field-for-field equality with replay, bounded memory, warm-up/restart, ordering rules.

Tolerance is zero: both engines perform the same floating-point operations in the
same order, so every snapshot must compare equal with ``==``.
"""
from dataclasses import fields
from datetime import date, timedelta
import unittest

from market_data.calendar import default_calendar
from market_data.models import MarketDataError
from technical.engine import TechnicalEngine
from technical.incremental import DuplicateBarError, IncrementalTechnicalEngine, OutOfOrderBarError
from technical.models import TechnicalConfig
from tests.market_data_fakes import calendar_bars
from tests.technical_fixtures import SCENARIOS

CAL = default_calendar()


def long_series(sessions=40, minutes=5, symbol="NVDA"):
    return calendar_bars(symbol, CAL.trading_days(date(2026, 1, 5), date(2026, 12, 31))[:sessions], minutes, base=180.0)


class EquivalenceTests(unittest.TestCase):
    def assert_same(self, expected, got, label):
        if expected != got:
            diff = [f.name for f in fields(expected) if getattr(expected, f.name) != getattr(got, f.name)]
            self.fail(f"{label}: fields differ: {diff}")

    def compare(self, bars, config=None, calendar=None, label=""):
        reference = TechnicalEngine(config, calendar).replay(bars)
        engine = IncrementalTechnicalEngine(config, calendar)
        for i, bar in enumerate(bars):
            self.assert_same(reference[i], engine.update(bar), f"{label} bar {i}")
        return reference

    def test_every_phase4_fixture_every_bar(self):
        for name, build in SCENARIOS.items():
            for symbol in ("META", "NVDA"):
                with self.subTest(scenario=name, symbol=symbol):
                    self.compare(build(symbol), label=f"{name} {symbol}")
                    self.compare(build(symbol), calendar=CAL, label=f"{name} {symbol} calendar")

    def test_other_configurations(self):
        bars = SCENARIOS["gap_up_failure"]("META")
        for config in (TechnicalConfig(pivot_window=3), TechnicalConfig(ema_periods=(5, 10, 20, 50), rsi_period=7,
                                                                       atr_period=5, volume_lookback=10),
                       TechnicalConfig(equal_tolerance_pct=0.05, failure_lookback=5, min_touches=3)):
            with self.subTest(config=config):
                self.compare(bars, config)

    def test_long_sequence_with_bounded_pivot_history(self):
        bars = long_series(sessions=30)
        config = TechnicalConfig(max_pivot_history=12)
        reference = self.compare(bars, config, CAL, "long bounded")
        self.assertGreater(reference[-1].evidence["confirmed_pivots"], 12)  # The bound is actually exercised.
        self.assertIsNotNone(reference[-1].ema["ema200"])

    def test_daily_bars(self):
        from market_data.aggregation import aggregate
        daily = aggregate(long_series(sessions=60, minutes=30), "1d", CAL)
        self.compare(daily, calendar=CAL, label="daily")


class MemoryBoundTests(unittest.TestCase):
    def test_state_size_is_independent_of_history_length(self):
        config = TechnicalConfig(max_pivot_history=50)
        bars = long_series(sessions=60)
        engine = IncrementalTechnicalEngine(config, CAL)
        sizes = []
        for i, bar in enumerate(bars):
            engine.update(bar)
            if i in (len(bars) // 2, len(bars) - 1):
                sizes.append(engine.state_size("NVDA", "5m"))
        self.assertEqual(sizes[0]["volumes"], sizes[1]["volumes"])
        for size in sizes:
            self.assertEqual(size["volumes"], config.volume_lookback)
            self.assertEqual(size["window"], 2 * config.pivot_window + 1)
            self.assertLessEqual(size["level_pivots"], config.max_pivot_history)
            self.assertLessEqual(size["closes"], config.failure_lookback)
            self.assertLessEqual(size["events"], config.failure_lookback + 1)
        self.assertEqual(sizes[1]["level_pivots"], config.max_pivot_history)
        state = engine._series[("NVDA", bars[0].interval)]
        self.assertTrue(all(ema.seed == [] for ema in state.emas.values()))  # Seed buffers released after warm-up.
        self.assertEqual((state.gain.seed, state.atr.seed), ([], []))

    def test_memory_does_not_grow_with_bars(self):
        import tracemalloc
        bars = long_series(sessions=40)
        engine = IncrementalTechnicalEngine(TechnicalConfig(max_pivot_history=50))
        for bar in bars[:800]:
            engine.update(bar)
        tracemalloc.start()
        try:
            for bar in bars[800:1600]:
                engine.update(bar)
            first, _ = tracemalloc.get_traced_memory()
            for bar in bars[1600:2400]:
                engine.update(bar)
            second, _ = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(second - first, 64 * 1024)  # Another 800 bars add no retained state (allocator noise only).


class OrderingAndWarmupTests(unittest.TestCase):
    def test_duplicate_and_out_of_order_rejected_without_state_change(self):
        bars = SCENARIOS["range"]("META")
        engine = IncrementalTechnicalEngine()
        for bar in bars[:30]:
            last = engine.update(bar)
        with self.assertRaises(DuplicateBarError):
            engine.update(bars[29])
        with self.assertRaises(OutOfOrderBarError):
            engine.update(bars[10])
        self.assertIs(engine.latest("META", "5m"), last)
        self.assertEqual(engine.update(bars[30]), TechnicalEngine().replay(bars[:31])[-1])
        self.assertTrue(issubclass(DuplicateBarError, MarketDataError))

    def test_warmup_equals_replay_and_update(self):
        bars = SCENARIOS["ema_crossover"]("NVDA")
        engine = IncrementalTechnicalEngine()
        self.assertEqual(engine.warmup(bars), TechnicalEngine().analyze(bars))
        self.assertIsNone(IncrementalTechnicalEngine().warmup([]))
        with self.assertRaises(MarketDataError):
            IncrementalTechnicalEngine().warmup([bars[1], bars[0]])

    def test_restart_from_warmup_then_live_bars(self):
        bars = SCENARIOS["breakout_high_volume"]("META")
        reference = TechnicalEngine().replay(bars)
        for cut in (1, 20, 57, 77):
            restarted = IncrementalTechnicalEngine()
            restarted.warmup(bars[:cut])
            live = [restarted.update(bar) for bar in bars[cut:]]
            self.assertEqual(live, reference[cut:], f"cut {cut}")

    def test_independent_series_and_reset(self):
        meta, nvda = SCENARIOS["bullish_trend"]("META"), SCENARIOS["bearish_trend"]("NVDA")
        engine = IncrementalTechnicalEngine()
        for a, b in zip(meta, nvda):
            engine.update(a)
            engine.update(b)
        self.assertEqual(engine.latest("META", "5m"), TechnicalEngine().analyze(meta))
        self.assertEqual(engine.latest("NVDA", "5m"), TechnicalEngine().analyze(nvda))
        self.assertEqual(engine.series_keys(), [("META", "5m"), ("NVDA", "5m")])
        engine.reset("META", "5m")
        self.assertIsNone(engine.latest("META", "5m"))
        self.assertEqual(engine.update(meta[0]).index, 0)

    def test_calendar_validation_on_update(self):
        from datetime import datetime, time
        from decimal import Decimal
        from market_data.models import EXCHANGE_TZ, MarketBar
        engine = IncrementalTechnicalEngine(calendar=CAL)
        holiday = MarketBar("META", datetime.combine(date(2026, 9, 7), time(10), tzinfo=EXCHANGE_TZ), "5m",
                            Decimal(1), Decimal(2), Decimal("0.5"), Decimal(1), 1)
        with self.assertRaises(MarketDataError):
            engine.update(holiday)
        with self.assertRaises(MarketDataError):
            engine.update("not a bar")
        self.assertEqual(engine.series_keys(), [])


if __name__ == "__main__":
    unittest.main()
