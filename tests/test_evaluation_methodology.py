"""Phase 4D evaluation methodology: session policy, baselines, excess returns, bootstrap/random-entry determinism,
sample rules, research-only pivot windows, setup lag, transitions, summaries, matrix periods, reproducibility, safety."""
from dataclasses import replace
from datetime import date, datetime, time
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from evaluation import metrics, setup_lag, statistics
from evaluation.matrix import main as matrix_main, parse_periods
from evaluation.research import (PRODUCTION_CONFIG, ResearchConfigError, differing_fields, is_production,
                                 research_config)
from evaluation.summary import cross_period, cross_symbol
from evaluation.technical_replay import FORMAT_VERSION, evaluate, main as replay_main
from market_data.aggregation import aggregate
from market_data.calendar import default_calendar
from market_data.config import load_market_data_settings
from market_data.models import EXCHANGE_TZ
from market_data.providers.polygon import PolygonProvider
from orchestrator.job_runner import ROOT
from technical.engine import TechnicalEngine
from technical.models import TechnicalConfig
from tests.market_data_fakes import TEST_KEY, FakeSession, aggregates_route, calendar_bars
from tests.technical_fixtures import SCENARIOS

CAL = default_calendar()
DAYS = CAL.trading_days(date(2026, 6, 1), date(2026, 9, 23))
FAST = dict(iterations=200)


def hourly(symbol="META", days=None, base=500.0):
    return aggregate(calendar_bars(symbol, days or DAYS[-30:], 30, base=base), "1h", CAL)


class SessionPolicyTests(unittest.TestCase):
    def test_session_bound_1h_horizons_1_2_3_and_empty_10(self):
        bars = hourly(days=DAYS[-10:])  # 10 full sessions x 7 hourly bars.
        labels = metrics.forward_labels(bars, (1, 2, 3, 5, 10), session_bounded=True)
        counts = {h: sum(1 for l in labels if l[h] is not None) for h in (1, 2, 3, 5, 10)}
        self.assertEqual(counts, {1: 60, 2: 50, 3: 40, 5: 20, 10: 0})  # Per session: 6, 5, 4, 2, 0 bars qualify.

    def test_no_session_bound_crosses_sessions_including_gaps(self):
        bars = hourly(days=DAYS[-10:])
        labels = metrics.forward_labels(bars, (5, 10), session_bounded=False)
        self.assertEqual(sum(1 for l in labels if l[10] is not None), len(bars) - 10)
        last_of_day = next(i for i in range(len(bars) - 1) if bars[i].timestamp.date() != bars[i + 1].timestamp.date())
        label = labels[last_of_day][5]
        self.assertAlmostEqual(label["forward_return"], float(bars[last_of_day + 5].close) / float(bars[last_of_day].close) - 1)

    def test_evaluate_and_cli_session_flags(self):
        bars = hourly()
        bound = evaluate(bars, horizons=(1, 2, 3), **FAST)
        cross = evaluate(bars, horizons=(1, 3, 5, 10), session_bounded=False, **FAST)
        self.assertEqual((bound["metadata"]["session_bound"], cross["metadata"]["session_bound"]), (True, False))
        self.assertEqual(cross["baseline"]["10"]["count"], len(bars) - 10)
        self.assertEqual(evaluate(bars, horizons=(10,), **FAST)["baseline"]["10"]["count"], 0)
        provider = FakeProvider(bars)
        for flag, expected in (("--session-bound", True), ("--no-session-bound", False)):
            out = io.StringIO()
            code = replay_main(["--symbol", "META", "--interval", "1h", "--start", "2026-06-01", "--end", "2026-09-24",
                                "--horizons", "1,2,3", flag, "--iterations", "200"], provider=provider, out=out)
            self.assertEqual((code, json.loads(out.getvalue())["metadata"]["session_bound"]), (0, expected))
        saved, sys.stderr = sys.stderr, io.StringIO()  # argparse prints usage to stderr.
        try:
            both = replay_main(["--symbol", "META", "--interval", "1h", "--start", "2026-06-01", "--end", "2026-09-24",
                                "--session-bound", "--no-session-bound"], provider=provider, out=io.StringIO())
        finally:
            sys.stderr = saved
        self.assertEqual(both, 2)  # The two flags are mutually exclusive: usage error.

    def test_no_look_ahead_states_independent_of_labels_and_future(self):
        bars = hourly()
        a = evaluate(bars, horizons=(1, 2, 3), **FAST)
        b = evaluate(bars, horizons=(1, 3, 5, 10), session_bounded=False, **FAST)
        self.assertEqual((a["state_frequency"], a["transitions"]), (b["state_frequency"], b["transitions"]))
        labels = metrics.forward_labels(bars, (3,), session_bounded=False)
        mutated = bars[:20] + [replace(bar, close=bar.close * 2, high=bar.high * 2) for bar in bars[20:]]
        # A label only uses bars t+1..t+h, so labels of bars t <= 16 are unaffected by changes from bar 20 on.
        self.assertEqual(metrics.forward_labels(mutated, (3,), session_bounded=False)[:17], labels[:17])
        full = [s.signal.state for s in TechnicalEngine().replay(bars)]
        self.assertEqual(evaluate(bars[:40], **FAST)["state_frequency"], metrics.state_frequency(full[:40]))


class BaselineAndStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.bars = hourly()
        self.report = evaluate(self.bars, horizons=(1, 3), session_bounded=False, **FAST)

    def test_all_bars_baseline_and_excess(self):
        labels = metrics.forward_labels(self.bars, (1,), session_bounded=False)
        pool = [l[1]["forward_return"] for l in labels if l[1] is not None]
        base = self.report["baseline"]["1"]
        self.assertEqual(base["count"], len(pool))
        self.assertAlmostEqual(base["mean"], sum(pool) / len(pool), places=8)
        for entry in self.report["states"].values():
            stats = entry["horizons"]["1"]
            if stats["status"] == "OK":
                self.assertAlmostEqual(stats["excess"]["mean"], stats["forward_return"]["mean"] - base["mean"], places=7)
                self.assertAlmostEqual(stats["excess"]["median"], stats["forward_return"]["median"] - base["median"],
                                       places=7)
                lo, hi = stats["excess"]["ci95_mean"]
                self.assertAlmostEqual(hi - lo, stats["forward_return"]["ci95_mean"][1] - stats["forward_return"]["ci95_mean"][0],
                                       places=7)

    def test_bootstrap_and_random_entry_are_deterministic(self):
        values = [0.01 * ((i * 37) % 11 - 5) for i in range(120)]
        a = statistics.bootstrap(values, seed=7, key="k")
        self.assertEqual(a, statistics.bootstrap(values, seed=7, key="k"))
        self.assertNotEqual(a, statistics.bootstrap(values, seed=8, key="k"))
        self.assertNotEqual(a, statistics.bootstrap(values, seed=7, key="other"))
        self.assertLessEqual(a["mean"][0], sum(values) / len(values))
        self.assertGreaterEqual(a["mean"][1], sum(values) / len(values))
        self.assertEqual(statistics.bootstrap([0.02] * 40, seed=1, key="c")["mean"], [0.02, 0.02])
        r = statistics.random_entry(values, 40, 0.5, seed=7, key="r")
        self.assertEqual(r, statistics.random_entry(values, 40, 0.5, seed=7, key="r"))
        self.assertEqual(r["state_percentile"], 100.0)  # A mean above every possible draw.
        self.assertLessEqual(r["p2_5"], r["p50"])
        self.assertLessEqual(r["p50"], r["p97_5"])
        self.assertEqual(evaluate(self.bars, horizons=(1, 3), session_bounded=False, **FAST), self.report)

    def test_sample_labels_and_suppression(self):
        self.assertEqual([statistics.sample_label(n) for n in (0, 29, 30, 99, 100, 499, 500)],
                         ["INSUFFICIENT", "INSUFFICIENT", "SMALL", "SMALL", "MODERATE", "MODERATE", "LARGE"])
        small = statistics.state_horizon([0.01] * 29, [0.0] * 100, dict(mean=0.0, median=0.0), seed=1, key="s",
                                         excursions=dict(mfe=[0.1] * 29, mae=[-0.1] * 29))
        self.assertEqual(small, dict(count=29, sample_label="INSUFFICIENT", status="INSUFFICIENT_SAMPLE"))
        for entry in self.report["states"].values():
            for stats in entry["horizons"].values():
                if stats["count"] < 30:
                    self.assertEqual(set(stats), {"count", "sample_label", "status"})
                else:
                    self.assertEqual(stats["status"], "OK")
                    self.assertTrue({"forward_return", "excess", "random_entry"} <= set(stats))

    def test_mfe_mae_orientation_and_medians(self):
        for state, entry in self.report["states"].items():
            for stats in entry["horizons"].values():
                if stats["status"] != "OK":
                    continue
                names = {"mfe", "mae"} if entry["direction"] != "none" else {"max_up", "max_down"}
                self.assertTrue(names <= set(stats))
                for name in names:
                    self.assertEqual(set(stats[name]), {"mean", "median"})

    def test_transition_probabilities(self):
        probs = self.report["transition_probabilities"]
        for state, entry in probs.items():
            self.assertEqual(entry["exits"], sum(v for k, v in self.report["transitions"].items()
                                                 if k.split("->")[0] == state))
            self.assertAlmostEqual(sum(t["share"] for t in entry["to"].values()), 1.0, places=5)


class ResearchConfigTests(unittest.TestCase):
    def test_research_config_varies_only_pivot_window(self):
        config = research_config(3)
        self.assertEqual(differing_fields(config), ["pivot_window"])
        self.assertTrue(is_production(research_config(2)))
        self.assertEqual(PRODUCTION_CONFIG, TechnicalConfig())
        self.assertEqual(TechnicalConfig().pivot_window, 2)
        for bad in (0, 11, "3", True, 2.5):
            with self.assertRaises(ResearchConfigError):
                research_config(bad)
        report = evaluate(SCENARIOS["bullish_trend"]("META"), config=config, **FAST)
        self.assertEqual((report["metadata"]["research_config"], report["metadata"]["pivot_window"]), (True, 3))
        with self.assertRaises(ResearchConfigError):
            evaluate(SCENARIOS["bullish_trend"]("META"), config=TechnicalConfig(rsi_period=7), **FAST)

    def test_production_runner_never_loads_research_config(self):
        code = ("import sys, technical.runner, technical.incremental, persistence.technical_shadow\n"
                "print(sorted(m for m in sys.modules if m.startswith('evaluation')))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])
        # The live runner builds IncrementalTechnicalEngine(calendar=...), i.e. the production default configuration.
        from technical.incremental import IncrementalTechnicalEngine
        self.assertEqual(IncrementalTechnicalEngine(calendar=CAL).config, PRODUCTION_CONFIG)
        research_config(4)
        self.assertEqual(PRODUCTION_CONFIG.pivot_window, 2)  # Building research configs never mutates production.

    def test_setup_lag_measurement(self):
        bars = SCENARIOS["gap_up_failure"]("META") + []
        for window in (1, 2, 3, 4):
            snapshots = TechnicalEngine(research_config(window)).replay(bars)
            events = setup_lag.setup_events(bars, snapshots)
            self.assertTrue(events, window)
            for e in events:
                self.assertEqual(e["confirmation_lag_bars"], window)
                self.assertGreaterEqual(e["setup_delay_bars"], e["confirmation_lag_bars"])
                self.assertEqual(e["setup_after_confirmation_bars"], e["setup_delay_bars"] - window)
                self.assertLess(e["pivot_candidate_timestamp"], e["pivot_confirmed_timestamp"])
                self.assertLessEqual(e["pivot_confirmed_timestamp"], e["setup_timestamp"])
        summary = setup_lag.summarize(events)
        self.assertEqual(summary["bearish_setup"]["status"], "INSUFFICIENT_SAMPLE")
        big = setup_lag.summarize([dict(events[0], setup_delay_bars=5)] * 30)
        self.assertEqual((big[events[0]["state"]]["status"], big[events[0]["state"]]["median_setup_delay_bars"]), ("OK", 5))


def fake_report(symbol, period, excess, count=100, state="range", h="1", interval="1d"):
    stats = (dict(count=count, sample_label=statistics.sample_label(count), status="INSUFFICIENT_SAMPLE")
             if count < 30 else dict(count=count, status="OK", excess=dict(mean=excess, median=excess,
                                                                            ci95_mean=[excess - 0.01, excess + 0.01])))
    return dict(metadata=dict(symbol=symbol, interval=interval, session_bound=False, pivot_window=2,
                              period=dict(label=period)), states={state: dict(horizons={h: stats})})


class SummaryTests(unittest.TestCase):
    def test_cross_symbol_labels(self):
        rows = cross_symbol([fake_report("META", "A", 0.01), fake_report("NVDA", "A", 0.02), fake_report("SPY", "A", -0.005),
                             fake_report("MSFT", "A", 0.03, count=10)])
        (row,) = rows
        self.assertEqual((row["symbols_meeting_min_sample"], row["direction"], row["label"]),
                         (["META", "NVDA", "SPY"], "mixed", "mixed_across_symbols"))
        self.assertEqual(row["excess_mean_range"], [-0.005, 0.02])
        consistent = cross_symbol([fake_report("META", "A", 0.01), fake_report("NVDA", "A", 0.02)])[0]
        self.assertEqual((consistent["direction"], consistent["label"]), ("positive", "directionally_consistent"))
        alone = cross_symbol([fake_report("META", "A", 0.01), fake_report("NVDA", "A", 0.02, count=5)])[0]
        self.assertEqual(alone["label"], "insufficient_evidence")
        for r in rows:
            self.assertNotIn(r["label"], ("profitable", "winning", "best", "tradeable"))

    def test_cross_period(self):
        rows = cross_period([fake_report("META", "A", 0.01), fake_report("META", "B", -0.02),
                             fake_report("NVDA", "A", 0.01), fake_report("NVDA", "B", 0.015),
                             fake_report("SPY", "A", 0.01), fake_report("SPY", "B", 0.01, count=12)])
        by = {r["symbol"]: r for r in rows}
        self.assertEqual((by["META"]["same_sign"], by["META"]["label"]), (False, "mixed_across_periods"))
        self.assertEqual((by["NVDA"]["label"], by["NVDA"]["magnitude_ratio_b_over_a"], by["NVDA"]["ci95_overlap"]),
                         ("directionally_consistent", 1.5, True))
        self.assertEqual((by["SPY"]["label"], by["SPY"]["count_b"]), ("insufficient_evidence", 12))


class FakeProvider:
    calendar = CAL

    class settings:
        provider = "polygon"
        min_request_interval_seconds = 0.0

    def __init__(self, bars):
        self.bars = bars

    def get_bars(self, symbol, interval, start, end):
        return self.bars


MATRIX_DAYS = CAL.trading_days(date(2026, 6, 1), date(2026, 8, 28))
MATRIX_DATA = {s: {30: calendar_bars(s, MATRIX_DAYS, 30, base=b)} for s, b in
               (("META", 740.0), ("NVDA", 182.0), ("MSFT", 510.0), ("SPY", 660.0))}


def matrix_provider(session_log=None):
    def route(url, params):
        symbol = url.split("/ticker/")[1].split("/")[0]
        if session_log is not None:
            session_log.append(url)
        return aggregates_route(MATRIX_DATA[symbol])(url, params)
    settings = load_market_data_settings(dict(MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY,
                                              MARKET_DATA_DELAY_SECONDS="0", MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS="0"))
    return PolygonProvider(settings, calendar=CAL, session=FakeSession(route=route), sleep=lambda s: None,
                           clock=lambda: datetime.combine(date(2026, 9, 1), time(20), tzinfo=EXCHANGE_TZ))


class MatrixTests(unittest.TestCase):
    ARGS = ["--symbols", "META,NVDA,MSFT,SPY", "--intervals", "1h,1d", "--pivot-windows", "1,2,3",
            "--periods", "A=2026-07-15:2026-08-29,B=2026-06-01:2026-07-15", "--iterations", "100", "--seed", "99",
            "--pseudo-split", ""]

    def run_matrix(self, out_dir, log=None):
        stdout = io.StringIO()
        code = matrix_main(["--out", out_dir, *self.ARGS], provider=matrix_provider(log), out=stdout)
        return code, stdout.getvalue()

    def test_periods_must_not_overlap(self):
        self.assertEqual([p["label"] for p in parse_periods("A=2025-01-02:2026-09-24,B=2024-10-01:2025-01-01")],
                         ["A", "B"])
        for bad in ("A=2025-01-02:2026-09-24,B=2024-10-01:2025-01-03", "A=2025-01-02:2024-01-01",
                    "A=2025-01-02:2025-02-01,A=2025-03-01:2025-04-01"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_periods(bad)

    def test_matrix_generic_symbols_periods_reproducible_and_safe(self):
        first, second = tempfile.mkdtemp(), tempfile.mkdtemp()
        log = []
        code, stdout = self.run_matrix(first, log)
        self.assertEqual(code, 0, stdout)
        self.assertEqual(self.run_matrix(second)[0], 0)
        files = sorted(os.listdir(first))
        self.assertEqual(files, sorted(os.listdir(second)))
        for name in files:  # Same data + config + seed: byte-identical output.
            with open(os.path.join(first, name)) as a, open(os.path.join(second, name)) as b:
                self.assertEqual(a.read(), b.read(), name)
        # 4 symbols x 2 periods x (1h sb + 1h xs for pivot windows 1/2/3, + 1d) = 56 reports; one 30m fetch each.
        self.assertEqual(len([f for f in files if f.endswith(".json") and f not in ("summary.json", "plan.json")]), 56)
        self.assertEqual(len(log), 8)
        report = json.load(open(os.path.join(first, "SPY_1h_A_xs_pw2.json")))
        meta = report["metadata"]
        self.assertEqual((report["evaluation_format_version"], meta["symbol"], meta["period"]["label"],
                          meta["session_bound"], meta["horizons"], meta["pivot_window"], meta["seed"], meta["provider"]),
                         (FORMAT_VERSION, "SPY", "A", False, [1, 3, 5, 10], 2, 99, "polygon"))
        self.assertGreaterEqual(meta["first_bar"], "2026-07-15")
        period_b = json.load(open(os.path.join(first, "MSFT_1h_B_sb_pw3.json")))["metadata"]
        self.assertLess(period_b["last_bar"], "2026-07-15")
        self.assertTrue(period_b["research_config"])
        summary = json.load(open(os.path.join(first, "summary.json")))
        self.assertTrue(summary["cross_symbol"] and summary["cross_period"] and summary["pivot_research"])
        self.assertEqual({r["pivot_window"] for r in summary["cross_symbol"]}, {2})  # Research runs stay separate.
        plan = json.load(open(os.path.join(first, "plan.json")))
        self.assertEqual(len(plan["fetch_diagnostics"]), 8)  # One 30m fetch per symbol-period.
        self.assertEqual(plan["excluded_overnight_bars"], 0)
        self.assertTrue(all(set(f) >= {"pages", "raw_bars", "kept_bars", "excluded_overnight_bars"}
                            for f in plan["fetch_diagnostics"]))
        everything = stdout + "".join(open(os.path.join(first, n)).read() for n in files)
        self.assertNotIn(TEST_KEY, everything)

    def test_overnight_bars_are_counted_in_plan(self):
        from datetime import timedelta
        overnight = calendar_bars("SPY", MATRIX_DAYS[:1], 30, base=660.0)[0]
        extra = replace(overnight, timestamp=datetime.combine(MATRIX_DAYS[0], time(20), tzinfo=EXCHANGE_TZ),
                        session=None)
        data = dict(MATRIX_DATA)
        data["SPY"] = {30: sorted(MATRIX_DATA["SPY"][30] + [extra], key=lambda b: b.timestamp)}
        def route(url, params):
            return aggregates_route(data[url.split("/ticker/")[1].split("/")[0]])(url, params)
        provider = matrix_provider()
        provider.http._session = FakeSession(route=route)
        out_dir = tempfile.mkdtemp()
        code = matrix_main(["--out", out_dir, "--symbols", "SPY", "--intervals", "1d", "--pivot-windows", "2",
                            "--periods", "B=2026-06-01:2026-07-15", "--iterations", "100", "--pseudo-split", ""],
                           provider=provider,
                           out=io.StringIO())
        plan = json.load(open(os.path.join(out_dir, "plan.json")))
        self.assertEqual((code, plan["excluded_overnight_bars"], plan["failures"]), (0, 1, []))

    def test_dry_run_and_budget_cap(self):
        out = io.StringIO()
        code = matrix_main(["--out", "/nonexistent", "--dry-run", *self.ARGS], provider=matrix_provider(), out=out)
        plan = json.loads(out.getvalue())
        self.assertEqual((code, plan["dry_run"], plan["budget"]["requests_total"]), (0, True, 8))
        out = io.StringIO()
        self.assertEqual(matrix_main(["--out", "/nonexistent", *self.ARGS, "--max-requests", "3"],
                                     provider=matrix_provider(), out=out), 2)
        self.assertIn("exceed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
