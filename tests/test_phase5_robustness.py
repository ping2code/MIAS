"""Phase 5 robustness validation: circular-shift null, pre-registered hypotheses and frozen rules, 1h pivot research
isolation, cross-sector matrix runs, progress/resume, reproducibility, report generation and secret safety."""
from collections import Counter
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, time
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np

from evaluation import hypotheses as hyp
from evaluation import nulls
from evaluation.matrix import main as matrix_main
from evaluation.phase5_report import ReportError, build, load, main as report_main
from evaluation.technical_replay import FORMAT_VERSION, evaluate
from market_data.calendar import default_calendar
from market_data.config import load_market_data_settings
from market_data.models import EXCHANGE_TZ
from market_data.providers.polygon import PolygonProvider
from orchestrator.job_runner import ROOT
from technical.models import TechnicalConfig
from tests.market_data_fakes import TEST_KEY, FakeSession, aggregates_route, calendar_bars
from tests.technical_fixtures import SCENARIOS

CAL = default_calendar()
PINNED = dict(H1="a4d93c803683213428ac87515b168757f249423b6e30355c89a4dbebebde9b71",
              H1X="127b1d753b0768da5a77ba45951a1fb01fdda35f103139d76ef9653516863546",
              H2="67f79e81489528aa81d4e7d63d109b77df7b692d6b7eae8da05756123358231b",
              H3="bc3de4174b3fa93898c72caadd282029368d99a4ebff22449b35a11f8c2b7cf8")
STATES = list("aaabbbbcccaaddddbbba" * 5)


class CircularShiftTests(unittest.TestCase):
    def test_shift_preserves_counts_and_circular_runs(self):
        base_counts, base_runs = Counter(STATES), nulls.circular_runs(STATES)
        for k in (1, 7, 33, 99):
            rotated = nulls.shifted(STATES, k)
            self.assertEqual(Counter(rotated), base_counts)
            self.assertEqual(nulls.circular_runs(rotated), base_runs)
            self.assertEqual(rotated, list(np.roll(np.array(STATES), k)))

    def test_zero_and_near_identity_shifts_excluded_and_deterministic(self):
        shifts = nulls.draw_shifts(100, 3, seed=5, key="k", iterations=2000)
        self.assertGreaterEqual(shifts.min(), nulls.min_shift(3))
        self.assertLessEqual(shifts.max(), 100 - nulls.min_shift(3))
        self.assertNotIn(0, shifts)
        self.assertTrue((shifts == nulls.draw_shifts(100, 3, seed=5, key="k", iterations=2000)).all())
        self.assertFalse((shifts == nulls.draw_shifts(100, 3, seed=6, key="k", iterations=2000)).all())
        self.assertEqual(len(nulls.draw_shifts(7, 3, seed=5, key="k")), 0)  # Too short: no valid shift.

    def test_null_statistics_percentile_and_p_value(self):
        rng = np.random.default_rng(1)
        states, label = [], "x"
        while len(states) < 400:  # Random run lengths: a periodic pattern would let some shifts re-align exactly.
            states += [label] * int(rng.integers(1, 9))
            label = "y" if label == "x" else "x"
        states = states[:400]
        aligned = [0.01 if s == "x" else -0.0025 for s in states]  # Returns tied to state x: extreme.
        base = float(np.mean(aligned))
        null = nulls.circular_shift_null(states, aligned, base, seed=1, key="a", horizon=1, iterations=500)["x"]
        self.assertEqual((null["status"], null["observed_percentile"], null["iterations"]), ("OK", 100.0, 500))
        self.assertAlmostEqual(null["p_two_sided"], 2 / 501, places=6)
        self.assertGreater(null["observed_excess_mean"], null["null_p97_5"])
        noise = list(rng.normal(0, 0.01, 400))
        base = float(np.mean(noise))
        flat = nulls.circular_shift_null(states, noise, base, seed=1, key="b", horizon=1, iterations=500)["x"]
        self.assertLessEqual(flat["null_p2_5"], flat["null_p50"])
        self.assertLessEqual(flat["null_p50"], flat["null_p97_5"])
        self.assertTrue(0 < flat["p_two_sided"] <= 1)
        self.assertEqual(flat, nulls.circular_shift_null(states, noise, base, seed=1, key="b", horizon=1,
                                                         iterations=500)["x"])

    def test_null_respects_label_validity_and_never_changes_states(self):
        bars = SCENARIOS["gap_up_failure"]("META")
        a = evaluate(bars, iterations=200)
        b = evaluate(bars, iterations=200)
        self.assertEqual(a, b)
        for state, entry in a["states"].items():
            for h, stats in entry["horizons"].items():
                if stats["status"] == "OK":
                    null = stats["circular_shift"]
                    self.assertAlmostEqual(null["observed_excess_mean"], stats["excess"]["mean"], places=6)
                else:
                    self.assertNotIn("circular_shift", stats)  # Minimum-sample rule: no null claim either.
        values = [None, 0.1, None, 0.2, 0.3, None, 0.4, 0.5, 0.6, None, 0.7, 0.8] * 3
        states = list("abababababab" * 3)
        valid_counts = {Counter(s for s, v in zip(nulls.shifted(states, k), values) if v is not None).total()
                        for k in (4, 8, 12, 20)}
        self.assertEqual(valid_counts, {sum(v is not None for v in values)})  # Same validity mask under every shift.

    def test_clustering_and_overlap_diagnostics(self):
        c = nulls.clustering(list("aaabcc"))
        self.assertEqual(c["a"], dict(runs=1, mean_run=3, median_run=3, max_run=3, multi_bar_fraction=1.0))
        self.assertEqual(c["b"]["multi_bar_fraction"], 0.0)
        o = nulls.overlap(list("aaxxxa"), [0.1] * 6, 2)
        self.assertEqual(o["a"], dict(observations=3, overlapping_fraction=round(2 / 3, 6)))
        self.assertEqual(nulls.overlap(list("aaaa"), [0.1] * 4, 1)["a"]["overlapping_fraction"], 0.0)


class HypothesisRegistryTests(unittest.TestCase):
    def test_registry_contents(self):
        self.assertEqual([h.id for h in hyp.REGISTRY], ["H1", "H1X", "H2", "H3"])
        self.assertEqual((hyp.H1.states, hyp.H1.intervals, hyp.H1.horizons, hyp.H1.symbols),
                         (("bearish_setup",), ("1d",), (5,), ("META", "MSFT")))
        self.assertIn("circular_shift", hyp.H1.null_model)
        self.assertEqual(hyp.H3.pivot_windows, (1, 2, 3, 4))
        self.assertEqual(set(hyp.H1X.symbols) & set(hyp.H1.symbols), set())
        for h in hyp.REGISTRY:
            self.assertEqual(h.min_sample, 30)

    def test_hypotheses_are_frozen_and_pinned(self):
        self.assertEqual(hyp.REGISTERED_HASHES, PINNED)  # Any edit to a definition fails here.
        with self.assertRaises(FrozenInstanceError):
            hyp.H1.horizons = (3,)
        self.assertNotEqual(replace(hyp.H1, horizons=(3,)).content_hash(), PINNED["H1"])
        doc = open(os.path.join(ROOT, "docs", "phase5-preregistered-hypotheses.md")).read()
        for key, value in PINNED.items():
            self.assertIn(f"| {key} | `{value}` |", doc)


def cell(n, excess, lo=-50.0, hi=50.0):
    return dict(count=n, excess_bps=excess, null_p2_5_bps=lo, null_p97_5_bps=hi)


class ClassificationTests(unittest.TestCase):
    def test_symbol_rules_and_minimum_sample(self):
        self.assertEqual(hyp.classify_symbol_excess(cell(29, 200)), "INSUFFICIENT")
        self.assertEqual(hyp.classify_symbol_excess(None), "INSUFFICIENT")
        self.assertEqual(hyp.classify_symbol_excess(cell(30, 200)), "CONSISTENT")
        self.assertEqual(hyp.classify_symbol_excess(cell(80, -60)), "UNSUPPORTED")   # Wrong direction.
        self.assertEqual(hyp.classify_symbol_excess(cell(80, 8, -5, 5)), "UNSUPPORTED")  # Near zero.
        self.assertEqual(hyp.classify_symbol_excess(cell(80, 40)), "UNSUPPORTED")    # Inside the null band.

    def test_h1_requires_holdout(self):
        good = dict(META=cell(70, 150), MSFT=cell(50, 120))
        self.assertEqual(hyp.classify_h1(good, {})[0], "INSUFFICIENT")            # No holdout: never SUPPORTED.
        self.assertEqual(hyp.classify_h1(good, dict(META=cell(40, 90), MSFT=cell(40, 80)))[0], "SUPPORTED")
        self.assertEqual(hyp.classify_h1(good, dict(META=cell(40, 90), MSFT=cell(40, -80)))[0], "MIXED")
        self.assertEqual(hyp.classify_h1(dict(META=cell(70, 150), MSFT=cell(50, 10)), {})[0], "MIXED")
        self.assertEqual(hyp.classify_h1(dict(META=cell(70, -5), MSFT=cell(50, 10)), {})[0], "UNSUPPORTED")
        self.assertEqual(hyp.classify_h1(dict(META=cell(20, 150), MSFT=cell(50, 120)), {})[0], "INSUFFICIENT")

    def test_h1x_h2_h3(self):
        self.assertEqual(hyp.classify_h1x(dict(A=cell(40, 90), B=cell(40, 90), C=cell(40, 90), D=cell(40, 90)))[0],
                         "SUPPORTED")
        self.assertEqual(hyp.classify_h1x(dict(A=cell(40, 90), B=cell(40, -90), C=cell(40, 90), D=cell(40, 5)))[0],
                         "MIXED")
        self.assertEqual(hyp.classify_h1x(dict(A=cell(40, 90), B=cell(10, 90), C=cell(40, 90), D=cell(40, 90)))[0],
                         "INSUFFICIENT")
        bull = dict(direction="bullish", count=50, mean=0.002, ci95=[0.001, 0.003])
        bear = dict(direction="bearish", count=50, mean=-0.002, ci95=[-0.003, -0.001])
        self.assertEqual(hyp.classify_h2([bull, bull, bear, bear])[0], "SUPPORTED")
        wrong = dict(bull, ci95=[-0.003, -0.001])
        self.assertEqual(hyp.classify_h2([bull, bull, bear, bear, bear, wrong])[0], "MIXED")  # Opposite caps.
        self.assertEqual(hyp.classify_h2([bull, dict(bull, count=5)])[0], "INSUFFICIENT")
        mono = dict(occurrences=[90, 80, 70, 60], move=[0.001, 0.002, 0.003, 0.004])
        flip = dict(occurrences=[60, 80, 70, 60], move=[0.001, 0.002, 0.003, 0.004])
        self.assertEqual(hyp.classify_h3([mono] * 4)[0], "SUPPORTED")
        self.assertEqual(hyp.classify_h3([mono, mono, flip, flip])[0], "MIXED")
        self.assertEqual(hyp.classify_h3([dict(mono, occurrences=[90, 80, 20, 10])] * 4)[0], "INSUFFICIENT")


UNIVERSE = ("META", "MSFT", "JPM", "UNH", "CAT", "XOM")
DAYS = CAL.trading_days(date(2026, 6, 1), date(2026, 8, 28))
DATA = {s: {30: calendar_bars(s, DAYS, 30, base=100.0 + 20 * i)} for i, s in enumerate(UNIVERSE)}


def fake_provider(log=None):
    def route(url, params):
        if log is not None:
            log.append(url)
        return aggregates_route(DATA[url.split("/ticker/")[1].split("/")[0]])(url, params)
    settings = load_market_data_settings(dict(MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY,
                                              MARKET_DATA_DELAY_SECONDS="0", MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS="0"))
    return PolygonProvider(settings, calendar=CAL, session=FakeSession(route=route), sleep=lambda s: None,
                           clock=lambda: datetime.combine(date(2026, 9, 1), time(20), tzinfo=EXCHANGE_TZ))


ARGS = ["--symbols", ",".join(UNIVERSE), "--intervals", "1h,1d", "--pivot-windows", "1,2,4",
        "--periods", "A=2026-07-15:2026-08-29,B=2026-06-01:2026-07-15", "--pseudo-split", "A=2026-08-05",
        "--iterations", "100", "--seed", "7"]


def run_matrix(out_dir, *extra, log=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = matrix_main(["--out", out_dir, *ARGS, *extra], provider=fake_provider(log), out=stdout, err=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


class MatrixPhase5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.log = []
        cls.code, cls.stdout, cls.stderr = run_matrix(cls.dir, log=cls.log)

    def files(self, directory=None):
        return sorted(f for f in os.listdir(directory or self.dir) if f.endswith(".json"))

    def test_generic_cross_sector_run_and_research_isolation(self):
        self.assertEqual(self.code, 0, self.stdout)
        plan = json.load(open(os.path.join(self.dir, "plan.json")))
        # 6 symbols x 2 periods x (1h sb+xs for windows 1/2/4 = 6, + 1d) + 2 pseudo halves x 6 symbols = 96.
        self.assertEqual((plan["expected_reports"], len(plan["files"])), (96, 96))
        self.assertEqual(plan["hypothesis_hashes"], PINNED)
        self.assertEqual(len(self.log), 12)  # One 30m fetch per symbol-period, pseudo halves reuse it.
        report = json.load(open(os.path.join(self.dir, "CAT_1h_A_xs_pw4.json")))
        self.assertEqual((report["evaluation_format_version"], report["metadata"]["research_config"],
                          report["metadata"]["pivot_window"]), (FORMAT_VERSION, True, 4))
        pseudo = json.load(open(os.path.join(self.dir, "JPM_1d_A2_d_pw2.json")))["metadata"]["period"]
        self.assertEqual((pseudo["pseudo_holdout"], pseudo["start"]), (True, "2026-08-05"))
        summary = json.load(open(os.path.join(self.dir, "summary.json")))
        self.assertEqual({r["pivot_window"] for r in summary["cross_symbol"]}, {2})
        self.assertNotIn("A1", {r["period"] for r in summary["cross_symbol"]})
        self.assertIn("A", summary["correlations"])
        self.assertEqual(summary["correlations"]["A"]["symbols"], sorted(UNIVERSE))

    def test_progress_output(self):
        lines = [l for l in self.stderr.splitlines() if l.startswith("[")]
        self.assertTrue(any("step=fetch" in l and "fetch_pages=" in l for l in lines))
        self.assertTrue(lines[-1].startswith("[96/96]"))
        self.assertTrue(all("elapsed_seconds=" in l and "estimated_remaining_seconds=" in l for l in lines))
        self.assertTrue(any("reports_written=96" in l for l in lines))

    def test_no_secret_logging(self):
        everything = self.stdout + self.stderr + "".join(open(os.path.join(self.dir, f)).read() for f in self.files())
        self.assertNotIn(TEST_KEY, everything)
        self.assertNotIn('"o":', everything)  # No raw vendor payload fields.

    def test_resume_skips_valid_and_reruns_corrupt(self):
        target = tempfile.mkdtemp()
        for name in self.files():
            with open(os.path.join(self.dir, name)) as src, open(os.path.join(target, name), "w") as dst:
                dst.write(src.read())
        log = []
        code, stdout, stderr = run_matrix(target, "--resume", log=log)
        self.assertEqual((code, len(log), json.loads(stdout)["resumed"]), (0, 0, 96))  # Nothing refetched.
        with open(os.path.join(target, "UNH_1h_B_sb_pw1.json"), "w") as handle:
            handle.write("{corrupt")
        with open(os.path.join(target, "UNH_1d_A_d_pw2.json")) as handle:
            wrong = json.load(handle)
        wrong["metadata"]["seed"] = 1
        with open(os.path.join(target, "UNH_1d_A_d_pw2.json"), "w") as handle:
            json.dump(wrong, handle)
        log = []
        code, stdout, stderr = run_matrix(target, "--resume", log=log)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(u.split("/ticker/")[1].split("/")[0] for u in log), ["UNH", "UNH"])  # Only UNH A and B.
        self.assertIn("status=rerun_invalid", stderr)
        for name in ("UNH_1h_B_sb_pw1.json", "UNH_1d_A_d_pw2.json", "summary.json"):
            with open(os.path.join(target, name)) as a, open(os.path.join(self.dir, name)) as b:
                self.assertEqual(a.read(), b.read(), name)  # Restored byte-identically.

    def test_byte_identical_reproducibility(self):
        again = tempfile.mkdtemp()
        self.assertEqual(run_matrix(again)[0], 0)
        for name in self.files():
            if name == "plan.json":
                continue
            with open(os.path.join(again, name)) as a, open(os.path.join(self.dir, name)) as b:
                self.assertEqual(a.read(), b.read(), name)

    def test_dry_run_estimate(self):
        out = io.StringIO()
        code = matrix_main(["--out", "/nonexistent", "--dry-run", *ARGS], provider=fake_provider(), out=out,
                           err=io.StringIO())
        plan = json.loads(out.getvalue())
        self.assertEqual((code, plan["expected_reports"], plan["budget"]["requests_total"], plan["research_configs"]),
                         (0, 96, 12, [1, 4]))
        self.assertEqual(plan["hypothesis_hashes"], PINNED)

    def test_report_generation_and_hash_guard(self):
        out = tempfile.mktemp(suffix=".md")
        self.assertEqual(report_main(["--in", self.dir, "--out", out]), 0)
        text = open(out).read()
        for fragment in ("## 0. Verdicts (frozen rules)", "| H1 |", "| H1X |", "| H2 |", "| H3 |",
                         "## 7. Null models", "## 13. Symbol dependence"):
            self.assertIn(fragment, text)
        self.assertNotIn("best", text.lower().replace("bests", ""))
        reports, plan, summary = load(self.dir)
        results, _ = build(reports, plan, summary)
        self.assertIn(results["H1"]["status"], ("SUPPORTED", "MIXED", "UNSUPPORTED", "INSUFFICIENT"))
        self.assertNotEqual(results["H1"]["status"], "SUPPORTED")  # No holdout exists: never SUPPORTED.
        tampered = tempfile.mkdtemp()
        for name in self.files():
            with open(os.path.join(self.dir, name)) as src:
                data = json.load(src)
            if name == "plan.json":
                data["hypothesis_hashes"]["H1"] = "0" * 64
            with open(os.path.join(tampered, name), "w") as dst:
                json.dump(data, dst)
        with self.assertRaises(ReportError):
            load(tampered)


class LiveRunnerIsolationTests(unittest.TestCase):
    def test_live_runner_never_loads_research_code(self):
        code = ("import sys, technical.runner, technical.incremental, persistence.technical_shadow\n"
                "print(sorted(m for m in sys.modules if m.startswith('evaluation')))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])
        self.assertEqual(TechnicalConfig().pivot_window, 2)


if __name__ == "__main__":
    unittest.main()
