"""Phase 4 indicators, pinned to independently published values and brute-force references.

The EMA and RSI reference series are the widely reproduced StockCharts
"ChartSchool" worked examples (10-day EMA; 14-period Wilder RSI). Brute-force
references recompute each output from scratch at every index, which also proves
the functions are causal.
"""
from datetime import datetime, timedelta
from decimal import Decimal
import unittest

from market_data.models import EXCHANGE_TZ, MarketBar
from technical import indicators

EMA_CLOSES = [22.27, 22.19, 22.08, 22.17, 22.18, 22.13, 22.23, 22.43, 22.24, 22.29, 22.15, 22.39, 22.38, 22.61, 23.36,
              24.05, 23.75, 23.83, 23.95, 23.63, 23.82, 23.87, 23.65, 23.19, 23.10, 23.33, 22.68, 23.10, 22.40, 22.17]
EMA10_PUBLISHED = [22.22, 22.21, 22.24, 22.27, 22.33, 22.52, 22.80, 22.97, 23.13, 23.28, 23.34, 23.43, 23.51, 23.53,
                   23.47, 23.40, 23.39, 23.26, 23.23, 23.08, 22.92]
RSI_CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931, 46.0328,
              45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439, 46.2122, 46.2521, 45.7137, 46.4515,
              45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672, 43.4205, 42.6628, 43.1314]
RSI14_PUBLISHED = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42, 39.99, 41.46, 41.87,
                   45.46, 37.30, 33.08, 37.77]
T0 = datetime(2026, 9, 21, 9, 30, tzinfo=EXCHANGE_TZ)


def make_bars(rows, start=T0, step=timedelta(minutes=5), interval="5m"):
    return [MarketBar("META", start + step * i, interval, *(Decimal(str(x)) for x in (o, h, l, c)), v)
            for i, (o, h, l, c, v) in enumerate(rows)]


class MovingAverageTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(indicators.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_ema_matches_published_stockcharts_table(self):
        out = indicators.ema(EMA_CLOSES, 10)
        self.assertEqual(out[:9], [None] * 9)
        self.assertAlmostEqual(out[9], sum(EMA_CLOSES[:10]) / 10)  # Seeded with the SMA.
        for i, (got, published) in enumerate(zip(out[9:], EMA10_PUBLISHED)):
            # The published table rounds intermediate values to cents, so one row (23.54) differs by a rounding step.
            self.assertAlmostEqual(got, published, delta=0.011, msg=f"row {i + 9}")
        self.assertEqual([round(v, 2) for v in out[9:]][13], 23.53)

    def test_ema_recurrence_and_warmup(self):
        out = indicators.ema([10, 11, 12, 13, 14, 15], 3)
        k = 0.5
        expected = [None, None, 11.0]
        for value in (13, 14, 15):
            expected.append((value - expected[-1]) * k + expected[-1])
        self.assertEqual(out, expected)
        self.assertEqual(indicators.ema([1, 2], 3), [None, None])

    def test_invalid_periods(self):
        for period in (0, -1, 2.5, True):
            with self.subTest(period=period), self.assertRaises(ValueError):
                indicators.ema([1, 2, 3], period)


class RsiTests(unittest.TestCase):
    def test_rsi_matches_published_wilder_example(self):
        out = indicators.rsi(RSI_CLOSES, 14)
        self.assertEqual(out[:14], [None] * 14)
        self.assertEqual([round(v, 2) for v in out[14:]], RSI14_PUBLISHED)

    def test_edge_cases(self):
        self.assertEqual(indicators.rsi(list(range(1, 17)), 14)[14:], [100.0, 100.0])   # Gains only.
        self.assertEqual(indicators.rsi(list(range(16, 0, -1)), 14)[14:], [0.0, 0.0])   # Losses only.
        self.assertEqual(indicators.rsi([5.0] * 16, 14)[14:], [50.0, 50.0])             # Flat.
        self.assertEqual(indicators.rsi([1.0] * 14, 14), [None] * 14)                   # Needs period + 1 closes.


def brute_atr(bars, period, index):
    trs = []
    for i in range(index + 1):
        h, l = float(bars[i].high), float(bars[i].low)
        trs.append(h - l if i == 0 else max(h - l, abs(h - float(bars[i - 1].close)), abs(l - float(bars[i - 1].close))))
    if index < period - 1:
        return None
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


class TrueRangeAtrTests(unittest.TestCase):
    ROWS = [(10, 11, 9, 10.5, 1), (10.5, 12, 10, 11, 1), (13, 14, 12.5, 13.5, 1),  # Gap up: |high - prev close| wins.
            (9, 10, 8, 9.5, 1), (9.5, 9.8, 9.2, 9.6, 1), (9.6, 11, 9.6, 10.9, 1), (10.9, 11, 10.1, 10.2, 1)]

    def test_true_range_includes_gaps(self):
        tr = indicators.true_range(make_bars(self.ROWS))
        self.assertEqual(tr[:4], [2.0, 2.0, 3.0, 5.5])  # Gap-down bar: |low - previous close| = 13.5 - 8.
        self.assertAlmostEqual(tr[4], 0.6, places=12)

    def test_atr_matches_brute_force(self):
        bars = make_bars(self.ROWS)
        out = indicators.atr(bars, 3)
        for i in range(len(bars)):
            expected = brute_atr(bars, 3, i)
            if expected is None:
                self.assertIsNone(out[i])
            else:
                self.assertAlmostEqual(out[i], expected, places=12)
        self.assertAlmostEqual(out[2], (2 + 2 + 3) / 3)


class VwapTests(unittest.TestCase):
    def test_vwap_cumulative(self):
        bars = make_bars([(10, 11, 9, 10, 100), (10, 12, 10, 11, 300), (11, 13, 11, 12, 0)])
        tp = [(11 + 9 + 10) / 3, (12 + 10 + 11) / 3]
        expected = (tp[0] * 100 + tp[1] * 300) / 400
        out = indicators.vwap(bars)
        self.assertAlmostEqual(out[0], tp[0])
        self.assertAlmostEqual(out[1], expected)
        self.assertAlmostEqual(out[2], expected)  # A zero-volume bar adds nothing.

    def test_zero_volume_session_start_is_none(self):
        out = indicators.vwap(make_bars([(10, 11, 9, 10, 0), (10, 11, 9, 10, 50)]))
        self.assertIsNone(out[0])
        self.assertAlmostEqual(out[1], 10.0)

    def test_session_reset_and_extended_hours(self):
        day1 = make_bars([(10, 11, 9, 10, 100), (10, 11, 9, 10, 100)])
        post = make_bars([(20, 21, 19, 20, 999)], start=T0.replace(hour=16))
        pre = make_bars([(30, 31, 29, 30, 999)], start=T0.replace(day=22, hour=8))
        day2 = make_bars([(50, 51, 49, 50, 10), (60, 61, 59, 60, 10)], start=T0.replace(day=22))
        out = indicators.vwap(day1 + post + pre + day2)
        self.assertEqual(out[2:4], [None, None])  # Extended-hours bars are excluded.
        self.assertAlmostEqual(out[4], 50.0)      # Reset: day 1 volume does not carry over.
        self.assertAlmostEqual(out[5], 55.0)

    def test_daily_bars_have_no_vwap(self):
        daily = make_bars([(10, 11, 9, 10, 100)] * 2, start=datetime(2026, 9, 21, tzinfo=EXCHANGE_TZ),
                          step=timedelta(days=1), interval="1d")
        self.assertEqual(indicators.vwap(daily), [None, None])


class VolumeTests(unittest.TestCase):
    def test_average_and_relative_volume_exclude_current_bar(self):
        bars = make_bars([(10, 11, 9, 10, v) for v in (100, 200, 300, 600)])
        averages, relatives = indicators.relative_volume(bars, 3)
        self.assertEqual(averages, [None, None, None, 200.0])
        self.assertEqual(relatives, [None, None, None, 3.0])

    def test_zero_average_volume(self):
        bars = make_bars([(10, 11, 9, 10, v) for v in (0, 0, 50)])
        self.assertEqual(indicators.relative_volume(bars, 2), ([None, None, 0.0], [None, None, None]))


class FractionalVolumeTests(unittest.TestCase):
    def test_vwap_and_relative_volume_with_fractional_volume(self):
        rows = [(10, 11, 9, 10, "100.5"), (10, 12, 10, 11, "0.25"), (11, 13, 11, 12, "300.125")]
        bars = [MarketBar("META", T0 + timedelta(minutes=5 * i), "5m", Decimal(o), Decimal(h), Decimal(l), Decimal(c),
                          Decimal(v)) for i, (o, h, l, c, v) in enumerate(rows)]
        tp = [(11 + 9 + 10) / 3, (12 + 10 + 11) / 3, (13 + 11 + 12) / 3]
        vols = [100.5, 0.25, 300.125]
        out = indicators.vwap(bars)
        self.assertAlmostEqual(out[2], sum(t * v for t, v in zip(tp, vols)) / sum(vols), places=12)
        averages, relatives = indicators.relative_volume(bars, 2)
        self.assertEqual(averages[2], (100.5 + 0.25) / 2)
        self.assertEqual(relatives[2], 300.125 / ((100.5 + 0.25) / 2))

    def test_integer_volumes_unchanged(self):
        """Whole-number volumes give exactly the Phase 4 results (float sums of integers are exact)."""
        bars = make_bars([(10, 11, 9, 10, v) for v in (100, 200, 300, 600)])
        self.assertEqual(indicators.relative_volume(bars, 3), ([None, None, None, 200.0], [None, None, None, 3.0]))
        self.assertEqual(indicators.vwap(bars)[3], 10.0)


class CausalityTests(unittest.TestCase):
    def test_outputs_do_not_change_when_future_data_is_appended(self):
        bars = make_bars([(10 + i % 5, 12 + i % 5, 8 + i % 3 * 0.5, 10 + i % 4 * 0.5, 100 + 10 * i) for i in range(40)])
        closes = [float(b.close) for b in bars]
        full = dict(ema=indicators.ema(closes, 9), rsi=indicators.rsi(closes, 14), atr=indicators.atr(bars, 14),
                    vwap=indicators.vwap(bars), rv=indicators.relative_volume(bars, 20)[1])
        for n in range(1, len(bars) + 1):
            prefix = dict(ema=indicators.ema(closes[:n], 9), rsi=indicators.rsi(closes[:n], 14),
                          atr=indicators.atr(bars[:n], 14), vwap=indicators.vwap(bars[:n]),
                          rv=indicators.relative_volume(bars[:n], 20)[1])
            for name, values in prefix.items():
                self.assertEqual(values, full[name][:n], f"{name} changed at prefix {n}")


if __name__ == "__main__":
    unittest.main()
