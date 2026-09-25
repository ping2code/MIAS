"""Phase 4B evaluation harness: descriptive metrics, forward labels that never feed back, CLI exit codes."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr

from evaluation import metrics
from evaluation.technical_replay import NOTE, evaluate, main
from market_data.http import ProviderError
from market_data.models import EXCHANGE_TZ, MarketBar
from technical.engine import TechnicalEngine
from tests.technical_fixtures import SCENARIOS


def bars_from(closes, day=date(2026, 9, 23), start=time(9, 30)):
    out = []
    for i, c in enumerate(closes):
        p = Decimal(str(c))
        stamp = datetime.combine(day, start, tzinfo=EXCHANGE_TZ) + timedelta(minutes=5 * i)
        out.append(MarketBar("META", stamp, "5m", p, p + 1, p - 1, p, 100))
    return out


class MetricTests(unittest.TestCase):
    STATES = ["a", "a", "b", "b", "b", "a", "c"]

    def test_frequency_runs_durations_transitions(self):
        self.assertEqual(metrics.state_frequency(self.STATES)["b"], dict(count=3, share=round(3 / 7, 6)))
        self.assertEqual(metrics.state_runs(self.STATES), [("a", 0, 2), ("b", 2, 3), ("a", 5, 1), ("c", 6, 1)])
        self.assertEqual(metrics.durations(self.STATES)["a"], dict(runs=2, mean_bars=1.5, median_bars=1.5, max_bars=2,
                                                                  min_bars=1))
        self.assertEqual(metrics.transitions(self.STATES), {"a->b": 1, "a->c": 1, "b->a": 1})
        self.assertEqual(metrics.transitions(["x"]), {})

    def test_forward_labels_math(self):
        bars = bars_from([100, 102, 99, 105])
        labels = metrics.forward_labels(bars, (1, 3))
        self.assertAlmostEqual(labels[0][1]["forward_return"], 0.02)
        self.assertAlmostEqual(labels[0][3]["forward_return"], 0.05)
        self.assertAlmostEqual(labels[0][3]["max_up"], 106 / 100 - 1)   # Highest high over bars 1..3.
        self.assertAlmostEqual(labels[0][3]["max_down"], 98 / 100 - 1)  # Lowest low over bars 1..3.
        self.assertIsNone(labels[1][3])  # Past the end of the data.

    def test_session_bounded_labels(self):
        bars = bars_from([100, 101], start=time(15, 50)) + bars_from([110, 111], day=date(2026, 9, 24))
        self.assertIsNone(metrics.forward_labels(bars, (2,))[0][2])
        self.assertIsNotNone(metrics.forward_labels(bars, (2,), session_bounded=False)[0][2])

    def test_direction_aware_excursions(self):
        bars = bars_from([100, 104, 97])
        labels = metrics.forward_labels(bars, (2,))
        report = metrics.forward_by_state(["bullish_setup", "x", "x"], labels, (2,))
        bullish = report["bullish_setup"]["horizons"]["2"]
        self.assertAlmostEqual(bullish["mfe"]["mean"], 0.05)
        self.assertAlmostEqual(bullish["mae"]["mean"], -0.04)
        bearish = metrics.forward_by_state(["bearish_setup", "x", "x"], labels, (2,))["bearish_setup"]["horizons"]["2"]
        self.assertAlmostEqual(bearish["mfe"]["mean"], 0.04)
        self.assertAlmostEqual(bearish["mae"]["mean"], -0.05)
        self.assertNotIn("mfe", report["x"]["horizons"]["2"])
        self.assertEqual(report["x"]["horizons"]["2"]["forward_return"], dict(count=0))


class EvaluateTests(unittest.TestCase):
    def test_report(self):
        bars = SCENARIOS["false_breakout"]("NVDA")
        report = evaluate(bars)
        self.assertEqual((report["metadata"]["bar_count"], report["metadata"]["interval"], report["note"]),
                         (78, "5m", NOTE))
        self.assertIn("not a backtest", report["note"])
        self.assertEqual(sum(v["count"] for v in report["state_frequency"].values()), 78)
        self.assertEqual(report["final_state"]["state"], "range")
        self.assertEqual(set(report["states"]["breakout_watch"]["horizons"]), {"1", "3", "5", "10"})
        self.assertNotIn("forward", report)  # Phase 4D: the unsuppressed Phase 4C section is gone.
        json.dumps(report)
        self.assertEqual(evaluate([]), dict(evaluation_format_version="phase4d-v1", bars=0, note=NOTE))

    def test_states_use_only_past_data_and_labels_do_not_feed_back(self):
        bars = SCENARIOS["gap_up_failure"]("META")
        full = [s.signal.state for s in TechnicalEngine().replay(bars)]
        for cut in (40, 100):
            truncated = evaluate(bars[:cut])
            prefix = full[:cut]
            self.assertEqual(truncated["state_frequency"], metrics.state_frequency(prefix))
        # Changing only the evaluation horizons never changes states.
        a, b = evaluate(bars, horizons=(1,)), evaluate(bars, horizons=(10, 20))
        self.assertEqual((a["state_frequency"], a["transitions"]), (b["state_frequency"], b["transitions"]))


class FakeProvider:
    calendar = None

    def __init__(self, bars=None, error=None):
        self.bars, self.error = bars, error

    def get_bars(self, symbol, interval, start, end):
        if self.error:
            raise self.error
        return self.bars


class CliTests(unittest.TestCase):
    ARGS = ["--symbol", "meta", "--interval", "5m", "--start", "2026-09-21", "--end", "2026-09-23"]

    def run_main(self, argv, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_success(self):
        code, out, _ = self.run_main(self.ARGS, provider=FakeProvider(SCENARIOS["range"]("META")))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["metadata"]["bar_count"], 78)

    def test_provider_failure_and_config_errors(self):
        code, _, err = self.run_main(self.ARGS, provider=FakeProvider(error=ProviderError("auth", "HTTP 401 for x")))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", err)
        self.assertEqual(self.run_main(self.ARGS, environ={})[0], 2)                      # Provider not configured.
        self.assertEqual(self.run_main(self.ARGS[:4], environ={})[0], 2)                  # Missing arguments.
        self.assertEqual(self.run_main(self.ARGS + ["--horizons", "0"], environ={})[0], 2)


if __name__ == "__main__":
    unittest.main()
