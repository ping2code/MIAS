"""Phase 7B.1 Market Context engine: exact facts from hand-built completed 5m bars (offline; sockets disabled).

Covered:

- returns (both bases), exact Decimal arithmetic and quantization;
- relative returns;
- benchmarks, alignment and the common cutoff;
- session facts: early close, partial session;
- calendar: weekend, holiday, DST;
- completion rules; previous-close semantics;
- volume, VWAP and freshness;
- determinism and isolation.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import socket
import subprocess
import sys
import os
import unittest
from unittest.mock import patch

from market_context import models as m
from market_context.engine import build_market_context, build_series_context
from market_context.models import SeriesInput
from market_data.calendar import default_calendar
from market_data.models import EXCHANGE_TZ, MarketBar, MarketDataError, Session
from technical.engine import TechnicalEngine
from technical.indicators import vwap as mias_vwap

CAL = default_calendar()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREV, DAY = date(2026, 9, 22), date(2026, 9, 23)
D = Decimal


def et(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=EXCHANGE_TZ)


def bar(symbol, day, slot, o, h=None, l=None, c=None, v="100", session=None):
    """Regular 5m bar number ``slot`` (0 = 09:30) with exact prices."""
    o, c = D(str(o)), D(str(c if c is not None else o))
    h = D(str(h)) if h is not None else max(o, c)
    l = D(str(l)) if l is not None else min(o, c)
    stamp = CAL.session_times(day).open + timedelta(minutes=5 * slot)
    return MarketBar(symbol, stamp, "5m", o, h, l, c, D(str(v)), session=session or CAL.classify(stamp))


def flat(symbol, day, n, price, v="100", start=0):
    return [bar(symbol, day, start + i, price, v=v) for i in range(n)]


def series(symbol, bars, delay=0, provider="polygon"):
    return SeriesInput(symbol=symbol, interval="5m", bars=tuple(bars), provider_id=provider, delay_seconds=delay)


def context(symbol_bars, *benchmarks, now=None, as_of=None, symbol="META", delay=0):
    now = now or et(DAY, 11)
    as_of = as_of or now - timedelta(seconds=delay)
    return build_market_context(series(symbol, symbol_bars, delay),
                                [series(name, bars, delay) for name, bars in benchmarks],
                                now=now, as_of=as_of, calendar=CAL)


def standard(symbol, prev_close, open_, last_close, n=6):
    """Previous session ending at ``prev_close``; today: first bar opens at ``open_``, the last closes at ``last_close``."""
    today = [bar(symbol, DAY, 0, open_, c=open_)] + flat(symbol, DAY, n - 2, open_, start=1) + \
        [bar(symbol, DAY, n - 1, open_, c=last_close)]
    return flat(symbol, PREV, 78, prev_close) + today


class NoNetwork(unittest.TestCase):
    def run(self, result=None):
        with patch.object(socket, "socket", side_effect=OSError("network disabled in tests")), \
                patch.object(socket, "create_connection", side_effect=OSError("network disabled in tests")):
            return super().run(result)


class ReturnTests(NoNetwork):
    def test_positive_negative_zero_both_bases(self):
        for last, prev_ret, open_ret in (("101.25", "0.0125", "0.0024752475"), ("98", "-0.02", "-0.0297029703"),
                                         ("100", "0", "-0.0099009901")):
            sc = context(standard("META", "100", "101", last)).symbol_context
            self.assertEqual((sc.return_since_prev_close, sc.return_since_open), (D(prev_ret), D(open_ret)), last)
            self.assertEqual((sc.previous_close, sc.previous_close_session), (D("100"), PREV))
        flat_day = context(standard("META", "100", "100", "100")).symbol_context
        self.assertEqual((flat_day.return_since_prev_close, flat_day.return_since_open), (D("0"), D("0")))

    def test_exact_decimal_and_half_even_quantization(self):
        for last, expected in (("2.0000000001", "0"), ("2.0000000003", "2E-10"), ("2.00000000025", "1E-10"),
                               ("2.0000000005", "2E-10")):
            sc = context(standard("META", "2", "2", last)).symbol_context
            self.assertEqual(sc.return_since_prev_close, D(expected), last)  # 5E-11 -> 0, 1.5E-10 -> 2E-10 (even).
            self.assertEqual(sc.return_since_prev_close.as_tuple().exponent, -10)
        third = context(standard("META", "3", "3", "4")).symbol_context
        self.assertEqual(third.return_since_prev_close, D("0.3333333333"))
        self.assertEqual(m.SeriesContext("X", "5m", return_since_open=D("0.0125000000")).to_dict()["return_since_open"],
                         "0.0125")  # Canonical MIAS text.

    def test_missing_previous_session(self):
        sc = context(standard("META", "100", "101", "102")[78:]).symbol_context
        self.assertIsNone(sc.return_since_prev_close)
        self.assertIsNone(sc.previous_close)
        self.assertIn(m.NO_PREVIOUS_SESSION, sc.unavailable)
        self.assertEqual(sc.return_since_open, D("0.0099009901"))
        older = flat("META", date(2026, 9, 21), 78, "90") + standard("META", "100", "101", "102")[78:]
        self.assertIsNone(context(older).symbol_context.previous_close)  # Only the immediately previous session.


class RelativeTests(NoNetwork):
    def test_positive_negative_zero_relative(self):
        meta = standard("META", "100", "100", "101.25")
        for bench_last, relative in (("100.4", "0.0085"), ("102", "-0.0075"), ("101.25", "0")):
            ctx = context(meta, ("QQQ", standard("QQQ", "100", "100", bench_last)))
            by_basis = {c.basis: c for c in ctx.comparisons}
            self.assertEqual(set(by_basis), {"prev_close", "open"})
            for comparison in by_basis.values():
                self.assertEqual(comparison.relative_return, D(relative))
                self.assertEqual(comparison.relative_return, comparison.symbol_return - comparison.benchmark_return)
                self.assertEqual((comparison.aligned, comparison.unavailable), (True, ()))

    def test_bases_differ(self):
        ctx = context(standard("META", "100", "102", "103"), ("SPY", standard("SPY", "100", "99", "99")))
        by_basis = {c.basis: c for c in ctx.comparisons}
        self.assertEqual(by_basis["prev_close"].relative_return, D("0.03") - D("-0.01"))
        self.assertEqual(by_basis["open"].relative_return, D("0.0098039216") - D("0"))


class BenchmarkTests(NoNetwork):
    def test_spy_and_qqq_missing_and_self(self):
        meta = standard("META", "100", "100", "101")
        ctx = context(meta, ("SPY", standard("SPY", "100", "100", "100.5")), ("QQQ", standard("QQQ", "100", "100", "102")))
        self.assertEqual([(c.benchmark, c.basis) for c in ctx.comparisons],
                         [("SPY", "prev_close"), ("SPY", "open"), ("QQQ", "prev_close"), ("QQQ", "open")])
        self.assertEqual([b.symbol for b in ctx.benchmark_contexts], ["SPY", "QQQ"])
        for missing in ("SPY", "QQQ"):
            present = "QQQ" if missing == "SPY" else "SPY"
            partial = context(meta, (missing, []), (present, standard(present, "100", "100", "100")))
            got = {c.benchmark: c for c in partial.comparisons}
            self.assertEqual(got[missing].unavailable, (m.NO_BENCHMARK_DATA,))
            self.assertIsNone(got[missing].relative_return)
            self.assertEqual(got[present].relative_return, D("0.01"))
            self.assertEqual(partial.symbol_context.return_since_prev_close, D("0.01"))  # Symbol context intact.
        own = context(standard("SPY", "100", "100", "101"), ("SPY", standard("SPY", "100", "100", "101")),
                      symbol="SPY")
        self.assertEqual({c.unavailable for c in own.comparisons}, {(m.SELF,)})
        self.assertEqual(own.benchmark_contexts, ())
        nothing = context([], ("SPY", standard("SPY", "100", "100", "101")))
        self.assertEqual({c.unavailable for c in nothing.comparisons}, {(m.NO_SYMBOL_DATA,)})
        self.assertEqual(nothing.symbol_context.unavailable, (m.NO_DATA,))


class AlignmentTests(NoNetwork):
    def test_exact_alignment(self):
        ctx = context(standard("META", "100", "100", "101"), ("SPY", standard("SPY", "100", "100", "100")))
        c = ctx.comparisons[0]
        self.assertEqual((c.cutoff, c.symbol_bar_end, c.benchmark_bar_end, c.alignment_gap_seconds, c.aligned),
                         (et(DAY, 10), et(DAY, 10), et(DAY, 10), 0, True))

    def test_common_cutoff_uses_earlier_latest_bar(self):
        meta = standard("META", "100", "100", "100", n=8) + []
        meta[-3] = bar("META", DAY, 5, "100", c="105")  # Bar ending 10:00 closes at 105; later bars move on.
        spy = standard("SPY", "100", "100", "101", n=6)  # Latest SPY bar ends 10:00.
        ctx = context(meta, ("SPY", spy))
        c = ctx.comparisons[0]
        self.assertEqual((c.cutoff, c.symbol_bar_end, c.aligned), (et(DAY, 10), et(DAY, 10), True))
        self.assertEqual((c.symbol_return, c.benchmark_return, c.relative_return), (D("0.05"), D("0.01"), D("0.04")))
        self.assertEqual(ctx.symbol_context.latest_bar_end, et(DAY, 10, 10))
        self.assertEqual(ctx.symbol_context.return_since_prev_close, D("0"))  # Own latest, not the cutoff.

    def test_missing_bar_at_cutoff_is_misaligned_not_compared(self):
        meta = standard("META", "100", "100", "101", n=6)  # Ends 10:00.
        spy = standard("SPY", "100", "100", "101", n=8)
        del spy[78 + 5]  # SPY lacks the bar ending 10:00.
        c = context(meta, ("SPY", spy)).comparisons[0]
        self.assertEqual((c.cutoff, c.benchmark_bar_end, c.aligned, c.alignment_gap_seconds),
                         (et(DAY, 10), et(DAY, 9, 55), False, 300))
        self.assertIn(m.MISALIGNED, c.unavailable)
        self.assertIsNone(c.relative_return)
        self.assertIsNotNone(c.symbol_return)

    def test_session_mismatch(self):
        c = context(standard("META", "100", "100", "101"), ("SPY", flat("SPY", PREV, 78, "100"))).comparisons
        self.assertEqual({x.unavailable for x in c}, {(m.SESSION_MISMATCH,)})


class SessionTests(NoNetwork):
    def test_open_high_low_close_range_and_position(self):
        today = [bar("META", DAY, 0, "100", h="104", l="99", c="101"), bar("META", DAY, 1, "101", h="102", l="96", c="98"),
                 bar("META", DAY, 2, "98", h="99", l="97", c="97.5")]
        sc = context(flat("META", PREV, 78, "100") + today).symbol_context
        self.assertEqual((sc.session_open, sc.session_high, sc.session_low, sc.latest_close, sc.session_range),
                         (D("100"), D("104"), D("96"), D("97.5"), D("8")))
        self.assertEqual(sc.position_in_range, D("0.1875"))
        self.assertEqual((sc.distance_from_high, sc.distance_from_low), (D("0.0666666667"), D("0.0153846154")))
        self.assertEqual((sc.latest_bar_start, sc.latest_bar_end), (et(DAY, 9, 40), et(DAY, 9, 45)))
        for last, position in (("96", "0"), ("104", "1")):
            edge = today[:2] + [bar("META", DAY, 2, "98", h="104" if last == "104" else "99",
                                    l="96" if last == "96" else "97", c=last)]
            self.assertEqual(context(flat("META", PREV, 78, "100") + edge).symbol_context.position_in_range,
                             D(position))

    def test_zero_range(self):
        sc = context(flat("META", PREV, 78, "100") + flat("META", DAY, 3, "100")).symbol_context
        self.assertEqual((sc.session_range, sc.position_in_range), (D("0"), None))
        self.assertIn(m.ZERO_RANGE, sc.unavailable)
        one = context(flat("META", PREV, 78, "100") + [bar("META", DAY, 0, "100", h="101", l="99", c="100.5")])
        self.assertEqual((one.symbol_context.bars_completed, one.symbol_context.position_in_range), (1, D("0.75")))

    def test_partial_session_counts(self):
        today = flat("META", DAY, 18, "100")
        del today[4]
        sc = context(flat("META", PREV, 78, "100") + today, now=et(DAY, 11, 2)).symbol_context
        self.assertEqual((sc.bars_completed, sc.bars_expected_so_far), (17, 18))

    def test_extended_hours_excluded(self):
        pre = MarketBar("META", et(DAY, 8), "5m", D("50"), D("200"), D("40"), D("150"), D("99999"), session=Session.PRE)
        post = MarketBar("META", et(PREV, 16, 30), "5m", D("300"), D("300"), D("300"), D("300"), D("5"),
                         session=Session.POST)
        bars = flat("META", PREV, 78, "100") + [post, pre] + flat("META", DAY, 3, "101")
        sc = context(bars).symbol_context
        self.assertEqual((sc.previous_close, sc.session_high, sc.session_low, sc.session_volume),
                         (D("100"), D("101"), D("101"), D("300")))

    def test_early_close(self):
        half, before = date(2026, 11, 27), date(2026, 11, 25)
        bars = flat("META", before, 78, "100") + flat("META", half, 42, "101")
        sc = context(bars, now=et(half, 14), as_of=et(half, 14)).symbol_context
        self.assertEqual((sc.bars_completed, sc.bars_expected_so_far, sc.latest_bar_end), (42, 42, et(half, 13)))
        self.assertEqual((sc.previous_close_session, sc.return_since_prev_close), (before, D("0.01")))  # Skips Thanksgiving.
        self.assertEqual(sc.freshness.status, "market_closed")


class CalendarTests(NoNetwork):
    def test_weekend(self):
        fri = date(2026, 9, 25)
        ctx = context(flat("META", date(2026, 9, 24), 78, "100") + flat("META", fri, 78, "102"),
                      now=et(date(2026, 9, 26), 12))
        self.assertEqual((ctx.session_date, ctx.calendar_state, ctx.symbol_context.freshness.status),
                         (fri, "closed", "market_closed"))
        monday = context(flat("META", fri, 78, "100") + flat("META", date(2026, 9, 28), 6, "101"),
                         now=et(date(2026, 9, 28), 10)).symbol_context
        self.assertEqual((monday.previous_close_session, monday.return_since_prev_close), (fri, D("0.01")))

    def test_holiday(self):
        ctx = context(flat("META", date(2026, 11, 24), 78, "100") + flat("META", date(2026, 11, 25), 78, "99"),
                      now=et(date(2026, 11, 26), 12))
        self.assertEqual((ctx.session_date, ctx.calendar_state), (date(2026, 11, 25), "closed"))
        self.assertEqual(ctx.symbol_context.return_since_prev_close, D("-0.01"))

    def test_dst_transitions(self):
        for before, after in ((date(2026, 3, 6), date(2026, 3, 9)), (date(2026, 10, 30), date(2026, 11, 2))):
            bars = flat("META", before, 78, "100") + flat("META", after, 12, "103")
            sc = context(bars, now=et(after, 10, 30)).symbol_context
            self.assertEqual((sc.previous_close_session, sc.return_since_prev_close), (before, D("0.03")))
            self.assertEqual((sc.bars_completed, sc.bars_expected_so_far, sc.latest_bar_end),
                             (12, 12, et(after, 10, 30)))
            self.assertEqual(sc.freshness.status, "current")


class CompletionAndInputTests(NoNetwork):
    def test_completed_accepted_forming_rejected(self):
        bars = flat("META", PREV, 78, "100") + flat("META", DAY, 6, "101")  # Last bar ends 10:00.
        self.assertEqual(context(bars, now=et(DAY, 10)).symbol_context.bars_completed, 6)
        with self.assertRaises(MarketDataError):
            context(bars, now=et(DAY, 9, 59))
        with self.assertRaises(MarketDataError):
            context(bars, now=et(DAY, 10, 30), delay=900)  # as_of 10:15 is fine...
            context(bars, now=et(DAY, 10, 10), delay=900)  # ...as_of 09:55 is not.

    def test_input_contract(self):
        bars = flat("META", DAY, 3, "100")
        now = et(DAY, 11)
        for bad in (SeriesInput("META", "1h", tuple(bars), "polygon", 0), SeriesInput("NVDA", "5m", tuple(bars), "polygon", 0),
                    SeriesInput("META", "5m", tuple(reversed(bars)), "polygon", 0)):
            with self.assertRaises(MarketDataError):
                build_series_context(bad, now=now, as_of=now, calendar=CAL)
        with self.assertRaises(MarketDataError):
            build_series_context(series("META", bars), now=now.replace(tzinfo=None), as_of=now, calendar=CAL)
        with self.assertRaises(MarketDataError):
            build_series_context(series("META", bars), now=now, as_of=now + timedelta(seconds=1), calendar=CAL)


class VolumeAndVwapTests(NoNetwork):
    def test_session_volume_exact(self):
        today = [bar("META", DAY, i, "100", v=v) for i, v in enumerate(("1000.5", "0.25", "12345.678901", "0"))]
        sc = context(flat("META", PREV, 78, "100") + today, now=et(DAY, 9, 50)).symbol_context
        self.assertEqual(sc.session_volume, D("13346.428901"))
        self.assertEqual((sc.bars_completed, sc.bars_expected_so_far), (4, 4))

    def test_vwap_reuses_mias_formula(self):
        today = [bar("META", DAY, 0, "100", h="104", l="99", c="101", v="300"),
                 bar("META", DAY, 1, "101", h="102", l="96", c="98", v="100"),
                 bar("META", DAY, 2, "98", h="99", l="97", c="97.5", v="600")]
        bars = flat("META", PREV, 78, "90") + today
        sc = context(bars).symbol_context
        self.assertEqual(sc.vwap, mias_vwap(bars)[-1])
        self.assertEqual(sc.vwap, TechnicalEngine(calendar=CAL).replay(bars)[-1].vwap)
        expected = (101.33333333333333 * 300 + 98.66666666666667 * 100 + 97.83333333333333 * 600) / 1000
        self.assertAlmostEqual(sc.vwap, expected, places=9)
        self.assertEqual(sc.close_vs_vwap, (97.5 - sc.vwap) / sc.vwap)
        zero = context(flat("META", PREV, 78, "90") + flat("META", DAY, 2, "100", v="0")).symbol_context
        self.assertEqual((zero.vwap, zero.close_vs_vwap), (None, None))
        self.assertIn(m.NO_VWAP, zero.unavailable)


class FreshnessTests(NoNetwork):
    def test_statuses(self):
        bars = flat("META", PREV, 78, "100") + flat("META", DAY, 14, "101")  # Latest ends 10:40.
        f = context(bars, now=et(DAY, 10, 42)).symbol_context.freshness
        self.assertEqual((f.status, f.lag_bars, f.age_seconds, f.latest_bar_end, f.expected_latest_bar_end),
                         ("current", 0, 120, et(DAY, 10, 40), et(DAY, 10, 40)))
        f = context(bars, now=et(DAY, 10, 52)).symbol_context.freshness
        self.assertEqual((f.status, f.lag_bars, f.expected_latest_bar_end), ("lagging", 2, et(DAY, 10, 50)))
        f = context(flat("META", PREV, 78, "100") + flat("META", DAY, 78, "101"), now=et(DAY, 17)).symbol_context.freshness
        self.assertEqual((f.status, f.lag_bars, f.expected_latest_bar_end), ("market_closed", 0, et(DAY, 16)))
        f = context(flat("META", PREV, 78, "100"), now=et(DAY, 17)).symbol_context.freshness
        self.assertEqual((f.status, f.lag_bars), ("lagging", 78))  # A whole session missing.
        f = context([], now=et(DAY, 11)).symbol_context.freshness
        self.assertEqual((f.status, f.latest_bar_end, f.expected_latest_bar_end), ("no_data", None, et(DAY, 11)))
        off_grid = MarketBar("META", et(DAY, 9, 32), "5m", D("1"), D("1"), D("1"), D("1"), D("1"),
                             session=Session.REGULAR)
        f = context([off_grid], now=et(DAY, 11)).symbol_context.freshness
        self.assertEqual(f.status, "unknown")

    def test_configured_delay_moves_expected_cutoff(self):
        bars = flat("META", PREV, 78, "100") + flat("META", DAY, 11, "101")  # Latest ends 10:25.
        delayed = context(bars, now=et(DAY, 10, 40), delay=900).symbol_context.freshness
        self.assertEqual((delayed.status, delayed.expected_latest_bar_end, delayed.configured_delay_seconds,
                          delayed.as_of), ("current", et(DAY, 10, 25), 900, et(DAY, 10, 25)))
        live = context(bars, now=et(DAY, 10, 40)).symbol_context.freshness
        self.assertEqual((live.status, live.lag_bars), ("lagging", 3))
        early = context(flat("META", PREV, 78, "100"), now=et(DAY, 9, 40), delay=900).symbol_context.freshness
        self.assertEqual((early.status, early.expected_latest_bar_end), ("current", et(PREV, 16)))

    def test_freshness_never_changes_metrics(self):
        bars = flat("META", PREV, 78, "100") + flat("META", DAY, 14, "101")
        as_of = et(DAY, 10, 40)
        a = context(bars, now=et(DAY, 10, 42), as_of=as_of).symbol_context
        b = context(bars, now=et(DAY, 17), as_of=as_of, delay=900).symbol_context
        strip = lambda sc: {k: v for k, v in sc.to_dict().items() if k != "freshness"}  # noqa: E731
        self.assertEqual(strip(a), strip(b))  # Same bars and as_of: identical facts whatever the freshness.
        self.assertEqual((a.freshness.status, b.freshness.status), ("current", "market_closed"))
        self.assertEqual((a.freshness.configured_delay_seconds, b.freshness.configured_delay_seconds), (0, 900))


class DeterminismAndIsolationTests(NoNetwork):
    def test_byte_identical_to_dict(self):
        build = lambda: json.dumps(context(standard("META", "100", "100", "101"),  # noqa: E731
                                           ("SPY", standard("SPY", "100", "100", "100.5"))).to_dict(), sort_keys=True)
        self.assertEqual(build(), build())
        data = json.loads(build())
        self.assertEqual(data["context_format_version"], "phase7b-v1")
        self.assertEqual(data["symbol_context"]["return_since_prev_close"], "0.01")

    def test_no_interpretive_fields_or_values(self):
        forbidden = ("buy", "sell", "bullish", "bearish", "weak", "strong", "outperform", "underperform", "signal",
                     "score", "confidence", "recommend", "state")
        names = set()
        for model in (m.SeriesContext, m.Freshness, m.BenchmarkComparison, m.MarketContext):
            names |= {f for f in model.__dataclass_fields__}
        self.assertFalse([n for n in names if any(w in n for w in forbidden) and n != "calendar_state"])
        text = json.dumps(context(standard("META", "100", "100", "101"),
                                  ("QQQ", standard("QQQ", "100", "100", "102"))).to_dict()).lower()
        for word in forbidden[:-1]:
            self.assertNotIn(word, text)

    def test_no_research_evidence_or_ai_imports(self):
        code = ("import sys, market_context.runner, market_context.engine\n"
                "bad = sorted(n for n in sys.modules if n.split('.')[0] in "
                "('evaluation', 'evidence', 'openai', 'anthropic', 'redis', 'telegram'))\n"
                "print(bad)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])


if __name__ == "__main__":
    unittest.main()
