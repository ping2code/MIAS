"""Phase 4C bounded live-check tool, exercised against a fake HTTP layer (no credentials, no network)."""
from datetime import date, datetime, time
import io
import json
import unittest

from market_data.calendar import default_calendar
from market_data.config import load_market_data_settings
from market_data.live_check import main
from market_data.models import EXCHANGE_TZ, Session
from market_data.providers.polygon import PolygonProvider
from tests.market_data_fakes import TEST_KEY, FakeResponse, FakeSession, aggregates_route, calendar_bars, payload, to_results

CAL = default_calendar()
NOW = datetime.combine(date(2026, 9, 23), time(20), tzinfo=EXCHANGE_TZ)
DAYS = CAL.trading_days(date(2026, 9, 1), date(2026, 9, 23))
DATA = {5: calendar_bars("META", DAYS, 5, base=740.0, sessions=(Session.PRE, Session.REGULAR, Session.POST)),
        30: calendar_bars("META", DAYS, 30, base=740.0, sessions=(Session.PRE, Session.REGULAR, Session.POST))}
ENV = dict(MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY)


def provider(session, **env):
    return PolygonProvider(load_market_data_settings(dict(ENV, **env)), calendar=CAL, session=session,
                           sleep=lambda s: None, clock=lambda: NOW)


def run(argv, prov=None, environ=None):
    out = io.StringIO()
    code = main(argv, environ=environ, provider=prov, out=out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, lines, out.getvalue()


class LiveCheckTests(unittest.TestCase):
    def test_not_executed_without_provider(self):
        code, lines, _ = run([], environ={})
        self.assertEqual(code, 2)
        self.assertEqual(lines, [dict(live_validation="NOT EXECUTED",
                                      reason="MARKET_DATA_PROVIDER is not configured in the process environment")])
        code, lines, _ = run([], environ=dict(MARKET_DATA_PROVIDER="polygon"))
        self.assertEqual((code, lines[0]["live_validation"]), (2, "NOT EXECUTED"))
        self.assertEqual(run(["--days", "11"], environ={})[0], 2)
        self.assertEqual(run(["--symbols", "A,B,C,D,E"], environ={})[0], 2)

    def test_successful_check_reports_safe_metadata(self):
        session = FakeSession(route=aggregates_route(DATA))
        code, lines, text = run(["--symbols", "META", "--intervals", "5m,1h,1d", "--days", "3"],
                                provider(session, MARKET_DATA_DELAY_SECONDS="900"))
        self.assertEqual(code, 0)
        five, hour, day, final = lines
        self.assertEqual((five["requested_interval"], five["source_interval"], hour["source_interval"],
                          day["source_interval"]), ("5m", "5m", "30m", "30m"))
        self.assertEqual((five["bar_count"], hour["bar_count"], day["bar_count"]), (78 * 3, 7 * 3, 3))
        self.assertEqual(five["validation"], "ok")
        self.assertTrue(all(five["checks"].values()))
        self.assertEqual(five["completed_bar_count"], five["bar_count"])
        # Extended hours are fetched, then dropped by default; the fetch stops at the cut-off (now - 15 min = 19:45),
        # so the 19:50 and 19:55 post-market bars of the last day are not requested.
        self.assertEqual(five["source_bars_fetched"], 192 * 3 - 2)
        self.assertEqual((five["volume_json_types"], five["pages"], five["pagination_observed"]), (["int"], 1, False))
        self.assertEqual(five["first_timestamp"], "2026-09-21T09:30:00-04:00")
        self.assertEqual(five["latest_bar_age_seconds"], 4 * 3600)
        self.assertEqual(final, dict(live_validation="EXECUTED", result="ok", requests=3,
                                     checked_at=final["checked_at"]))
        self.assertNotIn(TEST_KEY, text)
        self.assertNotIn('"o":', text)  # No raw vendor fields.

    def test_pagination_delayed_status_rate_limit_and_volume_types(self):
        bars = [b for b in DATA[5] if b.timestamp.date() == date(2026, 9, 23)]
        first = to_results(bars[:100])
        second = [dict(r, v=float(r["v"])) for r in to_results(bars[100:])]
        session = FakeSession([
            FakeResponse(429, headers={"Retry-After": "1", "X-RateLimit-Remaining": "0"}),
            FakeResponse(200, dict(payload(first, status="DELAYED", next_url="https://api.polygon.io/v2/aggs/next"),
                                   adjusted=True)),
            FakeResponse(200, dict(payload(second, status="DELAYED"), adjusted=True))])
        code, lines, _ = run(["--symbols", "META", "--intervals", "5m", "--days", "1"], provider(session))
        summary = lines[0]
        self.assertEqual(code, 0)
        self.assertEqual((summary["pages"], summary["pagination_observed"], summary["statuses"]), (2, True, ["DELAYED"]))
        self.assertEqual((summary["rate_limited_responses"], summary["rate_limit_headers"]),
                         (1, {"Retry-After": "1", "X-RateLimit-Remaining": "0"}))
        self.assertEqual(summary["volume_json_types"], ["Decimal", "int"])  # Whole-number floats are accepted.
        self.assertEqual((summary["adjusted_echo"], summary["checks"]["adjusted_echo_matches"]), (["True"], True))

    def test_adjusted_mismatch_fails_check(self):
        bars = [b for b in DATA[5] if b.timestamp.date() == date(2026, 9, 23)]
        session = FakeSession([FakeResponse(200, dict(payload(to_results(bars)), adjusted=False))])
        code, lines, _ = run(["--symbols", "META", "--intervals", "5m", "--days", "1"], provider(session))
        self.assertEqual((code, lines[0]["validation"], lines[0]["checks"]["adjusted_echo_matches"]),
                         (1, "check_failed", False))

    def test_failures_are_reported_safely(self):
        good = to_results([b for b in DATA[5] if b.timestamp.date() == date(2026, 9, 23)])
        cases = dict(
            auth=([FakeResponse(401)], "auth"), rate_limit=([FakeResponse(429)] * 4, "rate_limit"),
            server=([FakeResponse(503)] * 4, "http"), malformed=([FakeResponse(200, text="{oops")], "payload"),
            negative_volume=([FakeResponse(200, payload([dict(good[0], v=-10.5)]))], "data"),
            duplicate=([FakeResponse(200, payload([good[0], good[0]]))], "data"),
            seconds_timestamp=([FakeResponse(200, payload([dict(good[0], t=good[0]["t"] // 1000)]))], "data"),
            bad_pagination=([FakeResponse(200, payload(good[:5], next_url="https://evil.example/x"))], "payload"),
            timeout=([__import__("requests").Timeout("slow")] * 4, "transport"))
        for name, (responses, kind) in cases.items():
            with self.subTest(case=name):
                code, lines, text = run(["--symbols", "META", "--intervals", "5m", "--days", "1"],
                                        provider(FakeSession(list(responses))))
                self.assertEqual((code, lines[0]["validation"], lines[0]["error_kind"]), (1, "failed", kind))
                self.assertEqual(lines[-1]["result"], "failed")
                self.assertNotIn(TEST_KEY, text)

    def test_fractional_volume_is_reported_not_rejected(self):
        good = to_results([b for b in DATA[5] if b.timestamp.date() == date(2026, 9, 23)])
        good[0]["v"], good[5]["v"] = 1500.125, 99.5
        code, lines, _ = run(["--symbols", "META", "--intervals", "5m", "--days", "1"],
                             provider(FakeSession([FakeResponse(200, payload(good))])))
        summary = lines[0]
        self.assertEqual((code, summary["validation"], summary["checks"]["volume_valid"]), (0, "ok", True))
        self.assertEqual((summary["fractional_volumes"], summary["max_fractional_volume_part"]), (2, "0.5"))
        self.assertEqual(summary["min_request_interval_seconds"], 12.0)
        self.assertEqual(summary["excluded_overnight_bars"], 0)

    def test_empty_response(self):
        code, lines, _ = run(["--symbols", "META", "--intervals", "5m", "--days", "1"],
                             provider(FakeSession([FakeResponse(200, payload([]))])))
        self.assertEqual((code, lines[0]["bar_count"], lines[0]["validation"]), (0, 0, "ok"))

    def test_request_budget_is_enforced(self):
        session = FakeSession(route=aggregates_route(DATA))
        code, lines, _ = run(["--symbols", "META", "--intervals", "5m,1h", "--days", "1", "--max-requests", "1"],
                             provider(session))
        self.assertEqual(code, 1)
        self.assertEqual((lines[1]["validation"], lines[1]["error_kind"]), ("failed", "budget"))
        self.assertEqual(len(session.calls), 1)

    def test_states_mode(self):
        session = FakeSession(route=aggregates_route(DATA))
        code, lines, _ = run(["--symbols", "META", "--intervals", "5m,1d", "--days", "1", "--states"],
                             provider(session, MARKET_DATA_DELAY_SECONDS="0"))
        states = lines[2]["final_states"]
        self.assertEqual(code, 0)
        self.assertEqual(set(states), {"5m", "1d"})
        self.assertEqual(states["5m"]["last_bar"], "2026-09-23T15:55:00-04:00")
        self.assertIn(states["1d"]["state"], ("bullish_setup", "bearish_setup", "bullish_momentum", "bearish_momentum",
                                              "breakout_watch", "breakdown_watch", "range", "mixed", "insufficient_data"))


if __name__ == "__main__":
    unittest.main()
