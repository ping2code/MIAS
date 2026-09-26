"""Phase 7B.1 Market Context runner: fetch-once budget, provider independence, partial failure, usage, key hygiene.

The fake vendor HTTP layer (``tests.market_data_fakes``) is used, and sockets are disabled.
"""
from datetime import date, datetime, time
import contextlib
import io
import json
import socket
import unittest
from unittest.mock import patch

from market_context import runner
from market_data.calendar import default_calendar
from market_data.config import load_market_data_settings
from market_data.models import EXCHANGE_TZ, Session
from market_data.providers.massive import MassiveStocksProvider
from market_data.providers.polygon import PolygonProvider
from tests.market_data_fakes import TEST_KEY, FakeResponse, FakeSession, aggregates_route, calendar_bars

CAL = default_calendar()
NOW = datetime.combine(date(2026, 9, 23), time(11, 2), tzinfo=EXCHANGE_TZ)
DAYS = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)]
BASES = dict(META=740.0, NVDA=182.0, MSFT=510.0, SPY=660.0, QQQ=590.0)
DATA = {s: calendar_bars(s, DAYS, 5, base=b, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        for s, b in BASES.items()}


def route(url, params, failing=()):
    symbol = url.split("/ticker/")[1].split("/")[0]
    if symbol in failing:
        return FakeResponse(403, dict(status="NOT_AUTHORIZED"))
    return aggregates_route({5: DATA[symbol]})(url, params)


def provider(kind="polygon", failing=(), delay="0"):
    env = dict(MARKET_DATA_PROVIDER=kind, MARKET_DATA_API_KEY=TEST_KEY, MARKET_DATA_DELAY_SECONDS=delay,
               MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS="0")
    cls = PolygonProvider if kind == "polygon" else MassiveStocksProvider
    session = FakeSession(route=lambda url, params: route(url, params, failing))
    return cls(load_market_data_settings(env), calendar=CAL, session=session, sleep=lambda s: None,
               clock=lambda: NOW), session


class RunnerTests(unittest.TestCase):
    def run(self, result=None):
        with patch.object(socket, "socket", side_effect=OSError("network disabled in tests")), \
                patch.object(socket, "create_connection", side_effect=OSError("network disabled in tests")):
            return super().run(result)

    def run_main(self, argv, prov=None, environ=None):
        out = io.StringIO()
        code = runner.main(argv, environ=environ or {}, provider=prov, clock=lambda: NOW, out=out)
        return code, out.getvalue()

    def test_each_unique_symbol_fetched_exactly_once(self):
        prov, session = provider()
        code, text = self.run_main(["--symbols", "META,NVDA,MSFT,META", "--benchmarks", "SPY,QQQ,SPY"], prov)
        self.assertEqual(code, 0, text)
        fetched = [c["url"].split("/ticker/")[1].split("/")[0] for c in session.calls]
        self.assertEqual(fetched, ["META", "NVDA", "MSFT", "SPY", "QQQ"])
        self.assertTrue(all("/range/5/minute/" in c["url"] for c in session.calls))
        result = json.loads(text)
        self.assertEqual((result["provider_requests"], result["errors"]), (5, {}))
        self.assertEqual([c["symbol"] for c in result["contexts"]], ["META", "NVDA", "MSFT"])
        meta = result["contexts"][0]
        self.assertEqual(len(meta["comparisons"]), 4)
        self.assertEqual(meta["session_date"], "2026-09-23")
        self.assertEqual(meta["symbol_context"]["bars_completed"], 18)  # 09:30 .. 10:55 completed at 11:02.
        self.assertEqual(meta["symbol_context"]["freshness"]["status"], "current")
        self.assertEqual(meta["symbol_context"]["previous_close_session"], "2026-09-22")
        self.assertTrue(all(c["aligned"] and c["relative_return"] is not None for c in meta["comparisons"]))
        self.assertEqual(meta["provenance"]["inputs"]["QQQ"]["provider"], "polygon")
        self.assertEqual((meta["provenance"]["adjusted"], meta["provenance"]["include_extended_hours"]), (True, False))

    def test_polygon_and_massive_inputs_give_identical_context(self):
        results = []
        for kind in ("polygon", "massive_stocks"):
            prov, _ = provider(kind)
            code, text = self.run_main(["--symbols", "META,NVDA", "--benchmarks", "SPY,QQQ"], prov)
            self.assertEqual(code, 0)
            data = json.loads(text)
            for ctx in data["contexts"]:
                providers = {v.pop("provider") for v in ctx["provenance"]["inputs"].values()}
                self.assertEqual(providers, {kind})
            results.append(json.dumps(data, sort_keys=True))
        self.assertEqual(results[0], results[1])

    def test_partial_provider_failure_keeps_other_context(self):
        prov, _ = provider(failing=("QQQ",))
        with self.assertLogs("market_data", "INFO"):
            code, text = self.run_main(["--symbols", "META", "--benchmarks", "SPY,QQQ"], prov)
        self.assertEqual(code, 1)
        result = json.loads(text)
        self.assertEqual(result["errors"], {"QQQ": "auth"})
        comparisons = {(c["benchmark"], c["basis"]): c for c in result["contexts"][0]["comparisons"]}
        self.assertEqual(comparisons[("QQQ", "open")]["unavailable"], ["no_benchmark_data"])
        self.assertIsNotNone(comparisons[("SPY", "open")]["relative_return"])
        self.assertIsNotNone(result["contexts"][0]["symbol_context"]["return_since_open"])
        self.assertNotIn(TEST_KEY, text)

    def test_self_benchmark_and_configured_delay(self):
        prov, session = provider(delay="900")
        code, text = self.run_main(["--symbols", "SPY", "--benchmarks", "SPY,QQQ"], prov)
        self.assertEqual((code, len(session.calls)), (0, 2))
        ctx = json.loads(text)["contexts"][0]
        spy = [c for c in ctx["comparisons"] if c["benchmark"] == "SPY"]
        self.assertEqual({tuple(c["unavailable"]) for c in spy}, {("self",)})
        self.assertEqual(ctx["as_of"], "2026-09-23T10:47:00-04:00")
        self.assertEqual(ctx["symbol_context"]["latest_bar_end"], "2026-09-23T10:45:00-04:00")
        self.assertEqual(ctx["symbol_context"]["freshness"]["configured_delay_seconds"], 900)

    def test_usage_and_configuration_errors_make_no_requests(self):
        prov, session = provider()
        for argv in (["--symbols", ""], ["--symbols", "meta!"], ["--symbols", ",".join(f"S{i}" for i in range(25))], []):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.run_main(argv, prov)[0], 2)
        self.assertEqual(session.calls, [])
        code, text = self.run_main(["--symbols", "META"], None, environ={})
        self.assertEqual(code, 2)
        self.assertNotIn(TEST_KEY, text)

    def test_output_is_facts_only(self):
        prov, _ = provider()
        _, text = self.run_main(["--symbols", "META"], prov)
        lowered = text.lower()
        for word in ("buy", "sell", "bullish", "bearish", "outperform", "underperform", "score", "confidence"):
            self.assertNotIn(word, lowered)
        self.assertNotIn(TEST_KEY, text)


if __name__ == "__main__":
    unittest.main()
