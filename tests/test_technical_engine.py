"""Phase 4 engine on the deterministic SYNTHETIC META/NVDA scenario corpus (not historical prices)."""
from datetime import datetime, timedelta
from decimal import Decimal
import json
import unittest

from market_data.models import EXCHANGE_TZ, MarketBar, MarketDataError
from market_data.provider import FixtureProvider
from technical.engine import TechnicalEngine
from technical.formatter import format_snapshot
from technical.models import TechnicalConfig
from tests.technical_fixtures import DAY1, DAY2, SCENARIOS

ENGINE = TechnicalEngine()
SYMBOLS = ("META", "NVDA")
FINAL = dict(bullish_trend=("bullish_setup", "HIGH", "bullish"), bearish_trend=("bearish_setup", "HIGH", "bearish"),
             range=("range", "LOW", "range"), gap_up_continuation=("bullish_setup", "HIGH", "bullish"),
             gap_up_failure=("bearish_setup", "MEDIUM", "bearish"), vwap_reclaim=("bullish_setup", "HIGH", "bullish"),
             vwap_rejection=("bearish_setup", "HIGH", "bearish"), ema_crossover=("bullish_setup", "HIGH", "bullish"),
             breakout_high_volume=("bullish_setup", "HIGH", "bullish"), false_breakout=("range", "LOW", "range"))


def run(name, symbol):
    return ENGINE.replay(SCENARIOS[name](symbol))


class ScenarioTests(unittest.TestCase):
    def test_corpus_is_complete_and_final_states_hold_for_both_symbols(self):
        self.assertEqual(set(SCENARIOS), set(FINAL))
        for name, (state, confidence, trend) in FINAL.items():
            for symbol in SYMBOLS:
                with self.subTest(scenario=name, symbol=symbol):
                    last = run(name, symbol)[-1]
                    self.assertEqual((last.signal.state, last.signal.confidence, last.trend), (state, confidence, trend))
                    self.assertEqual(last.symbol, symbol)

    def test_early_bars_are_insufficient_data(self):
        snaps = run("bullish_trend", "META")
        self.assertTrue(all(s.signal.state == "insufficient_data" for s in snaps[:19]))  # EMA20 needs 20 bars.
        self.assertNotEqual(snaps[19].signal.state, "insufficient_data")
        self.assertIsNone(snaps[-1].ema["ema200"])  # 78 bars: the 200-period EMA honestly stays unavailable.

    def test_bullish_and_bearish_structure(self):
        up, down = run("bullish_trend", "NVDA")[-1], run("bearish_trend", "NVDA")[-1]
        self.assertEqual((up.last_high_type, up.last_low_type), ("HH", "HL"))
        self.assertEqual((down.last_high_type, down.last_low_type), ("LH", "LL"))
        self.assertEqual(up.ema_state["alignment"], "bullish_alignment")
        self.assertEqual(down.ema_state["alignment"], "bearish_alignment")
        self.assertTrue(up.support_levels and not up.resistance_levels)

    def test_range_has_both_levels_and_no_breaks(self):
        for symbol in SYMBOLS:
            snaps = run("range", symbol)
            self.assertTrue(snaps[-1].support_levels and snaps[-1].resistance_levels)
            self.assertEqual({s.breakout_state for s in snaps}, {"none"})

    def test_gap_up_continuation_and_failure(self):
        for symbol in SYMBOLS:
            cont, fail = run("gap_up_continuation", symbol), run("gap_up_failure", symbol)
            day2 = [s for s in cont if s.timestamp.date() == DAY2]
            self.assertTrue(all(s.gap_type is None for s in cont[:78]))  # No previous session on day 1.
            self.assertTrue(all(s.gap_type == "gap_up" and abs(s.gap_percent - 2.0) < 0.01 for s in day2))
            self.assertEqual(fail[-1].gap_type, "gap_up")
            # The gap still conflicts with the bearish fade, so confidence is capped at MEDIUM.
            self.assertGreaterEqual(fail[-1].signal.conflicting, 1)
            self.assertIn("gap up +2.00%", fail[-1].signal.reasons)

    def test_vwap_reclaim_and_rejection(self):
        for symbol in SYMBOLS:
            reclaim = [s.vwap_state["position"] for s in run("vwap_reclaim", symbol)]
            self.assertIn("crossing_above_vwap", reclaim[30:])
            first_cross = reclaim.index("crossing_above_vwap", 30)
            self.assertTrue(all(p == "below_vwap" for p in reclaim[20:first_cross]))
            self.assertTrue(all(p == "above_vwap" for p in reclaim[first_cross + 1:]))
            rejection = [s.vwap_state["position"] for s in run("vwap_rejection", symbol)]
            up = rejection.index("crossing_above_vwap", 30)
            down = rejection.index("crossing_below_vwap", up)
            self.assertTrue(all(p == "below_vwap" for p in rejection[down + 1:]))

    def test_ema_crossover(self):
        for symbol in SYMBOLS:
            snaps = run("ema_crossover", symbol)
            day1_end = snaps[77]
            self.assertEqual(day1_end.ema_state["alignment"], "bearish_alignment")
            crosses = [s.index for s in snaps if s.ema_state["ema9_20_cross"] == "bullish_cross"]
            self.assertTrue(crosses and crosses[0] > 77)
            self.assertEqual(snaps[-1].ema_state["alignment"], "bullish_alignment")
            self.assertEqual(snaps[78].gap_type, "no_gap")

    def test_high_relative_volume_breakout(self):
        for symbol in SYMBOLS:
            snaps = run("breakout_high_volume", symbol)
            events = [(s.index, s.breakout_state) for s in snaps if s.breakout_state != "none"]
            self.assertEqual(events, [(57, "breakout")])
            bar = snaps[57]
            self.assertEqual((bar.signal.state, bar.signal.confidence), ("breakout_watch", "HIGH"))
            self.assertGreater(bar.relative_volume, 3.0)
            self.assertTrue(any("elevated" in r for r in bar.signal.reasons))
            self.assertEqual(snaps[56].signal.state, "range")

    def test_false_breakout(self):
        for symbol in SYMBOLS:
            snaps = run("false_breakout", symbol)
            self.assertEqual(snaps[57].signal.state, "breakout_watch")
            self.assertEqual([s.breakout_state for s in snaps[58:61]], ["none", "failed_breakout", "failed_breakout"])
            self.assertEqual(snaps[59].breakout_level, snaps[57].breakout_level)
            self.assertIn(f"failed breakout of level {snaps[57].breakout_level:.2f}", snaps[59].signal.reasons)
            self.assertEqual(snaps[61].breakout_state, "none")  # The failure window (3 bars) has expired.

    def test_symbols_are_generic(self):
        meta, nvda = run("bullish_trend", "META")[-1], run("bullish_trend", "NVDA")[-1]
        self.assertGreater(meta.price, 700)
        self.assertLess(nvda.price, 200)
        self.assertEqual(meta.signal.state, nvda.signal.state)


class EngineBehaviourTests(unittest.TestCase):
    def test_empty_and_invalid_series(self):
        self.assertEqual(ENGINE.replay([]), [])
        self.assertIsNone(ENGINE.analyze([]))
        bars = SCENARIOS["range"]("META")
        with self.assertRaises(MarketDataError):
            ENGINE.replay([bars[1], bars[0]])

    def test_snapshot_volume_is_exact_decimal(self):
        from dataclasses import replace
        from decimal import Decimal
        bars = [replace(b, volume=b.volume + Decimal("0.123456789123")) for b in SCENARIOS["range"]("META")]
        snap = ENGINE.analyze(bars)
        self.assertEqual((type(snap.volume), snap.volume), (Decimal, bars[-1].volume))
        self.assertEqual(snap.to_dict()["volume"], str(bars[-1].volume.normalize()))

    def test_snapshot_is_json_serializable(self):
        data = ENGINE.analyze(SCENARIOS["gap_up_continuation"]("META")).to_dict()
        text = json.dumps(data)
        self.assertEqual(json.loads(text)["signal"]["state"], "bullish_setup")
        for key in ("ema", "vwap", "rsi", "atr", "relative_volume", "trend", "support_levels", "resistance_levels",
                    "gap_type", "breakout_state", "ema_state", "vwap_state", "momentum", "evidence"):
            self.assertIn(key, data)

    def test_extended_hours_bars_have_no_vwap_or_gap(self):
        base = SCENARIOS["range"]("NVDA")
        pre = [MarketBar("NVDA", datetime(2026, 9, 21, 8, 0, tzinfo=EXCHANGE_TZ) + timedelta(minutes=5 * i), "5m",
                         Decimal("180"), Decimal("180.5"), Decimal("179.5"), Decimal("180.2"), 5000) for i in range(3)]
        snaps = ENGINE.replay(pre + base)
        self.assertTrue(all(s.vwap is None and s.gap_type is None for s in snaps[:3]))
        self.assertEqual(snaps[3].vwap_state["position"], ENGINE.replay(base)[0].vwap_state["position"])

    def test_daily_bars_and_gap_down(self):
        closes = [100 + i * 0.5 for i in range(30)]
        bars = []
        for i, close in enumerate(closes):
            open_ = close - 0.2 if i != 25 else closes[i - 1] * 0.98  # One 2% gap down.
            bars.append(MarketBar("META", datetime(2026, 8, 1, tzinfo=EXCHANGE_TZ) + timedelta(days=i), "1d",
                                  Decimal(str(round(open_, 2))), Decimal(str(round(max(open_, close) + 0.3, 2))),
                                  Decimal(str(round(min(open_, close) - 0.3, 2))), Decimal(str(close)), 1_000_000))
        snaps = ENGINE.replay(bars)
        self.assertEqual(snaps[25].gap_type, "gap_down")
        self.assertAlmostEqual(snaps[25].gap_percent, -2.0, places=2)
        self.assertTrue(all(s.vwap is None and s.vwap_state["position"] is None for s in snaps))
        self.assertEqual(snaps[0].gap_type, None)
        self.assertEqual(snaps[1].gap_type, "no_gap")

    def test_config_is_applied(self):
        engine = TechnicalEngine(TechnicalConfig(ema_periods=(5, 10, 20, 50), pivot_window=3))
        snaps = engine.replay(SCENARIOS["bullish_trend"]("META"))
        self.assertIn("ema5", snaps[-1].ema)
        self.assertEqual(snaps[13].signal.reasons, ("warming up: RSI not yet available",))  # EMA10 and ATR are ready.
        self.assertNotEqual(snaps[14].signal.state, "insufficient_data")
        with self.assertRaises(ValueError):
            TechnicalConfig(ema_periods=(20, 9, 50, 200))
        with self.assertRaises(ValueError):
            TechnicalConfig(rsi_strong=80)
        with self.assertRaises(ValueError):
            TechnicalConfig(pivot_window=0)
        with self.assertRaises(ValueError):
            TechnicalConfig(buffer_pct=-1)

    def test_fixture_provider_feeds_engine(self):
        provider = FixtureProvider([SCENARIOS["gap_up_continuation"]("META")])
        start = datetime.combine(DAY2, datetime.min.time(), tzinfo=EXCHANGE_TZ)
        day2 = provider.get_bars("META", "5m", start, start + timedelta(days=1))
        self.assertEqual(len(day2), 78)
        self.assertIsNone(ENGINE.analyze(day2).gap_type)  # Without day 1 the gap is honestly unknown.
        everything = provider.get_bars("META", "5m", start - timedelta(days=1), start + timedelta(days=1))
        self.assertEqual(ENGINE.analyze(everything).gap_type, "gap_up")
        self.assertEqual(DAY1 + timedelta(days=1), DAY2)


class FormatterTests(unittest.TestCase):
    def test_breakout_text(self):
        text = format_snapshot(run("breakout_high_volume", "META")[57])
        self.assertTrue(text.startswith("META — 5m\nPrice: "))
        for fragment in ("Trend: Range", "EMA:\n9 > 20 > 50", "above VWAP", "RSI(14):\n", "x average", "Breakout of ",
                         "Signal:\nBREAKOUT_WATCH (confidence HIGH)", "Reasons:\n- breakout of level"):
            self.assertIn(fragment, text)

    def test_gap_and_levels_text(self):
        text = format_snapshot(run("gap_up_continuation", "NVDA")[-1])
        self.assertIn("Gap:\ngap up (+2.00%)", text)
        self.assertIn("Levels:\nSupport ", text)
        self.assertIn("BULLISH_SETUP (confidence HIGH)", text)

    def test_warming_up_text(self):
        text = format_snapshot(run("range", "META")[3])
        self.assertIn("EMA:\nwarming up", text)
        self.assertIn("RSI(14):\nn/a", text)
        self.assertIn("INSUFFICIENT_DATA (confidence LOW)", text)

    def test_no_trade_language(self):
        for name in SCENARIOS:
            text = format_snapshot(run(name, "NVDA")[-1]).lower()
            for word in ("buy", "sell", "entry", "target", "stop loss", "position size"):
                self.assertNotIn(word, text, name)


if __name__ == "__main__":
    unittest.main()
