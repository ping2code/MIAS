"""Phase 7A Massive Stocks adapter (``massive_stocks``), entirely against a fake HTTP layer: sockets are disabled.

Covered:

- configuration and factory, including that ``polygon`` and the ``massive`` alias are unchanged;
- normalization, timestamps, DST and early closes;
- the supported-session filter, envelope, data validation and pagination;
- HTTP failure semantics and key hygiene;
- parity with ``PolygonProvider``: identical bars and identical Phase 6 ``bar_content_hash``.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import io
import json
import logging
import socket
import unittest
from unittest.mock import patch

import requests

from market_data.calendar import default_calendar
from market_data.config import MarketDataConfigError, load_market_data_settings
from market_data.http import ProviderError
from market_data.models import EXCHANGE_TZ, MarketDataError, Session
from market_data.providers import build_provider
from market_data.providers.massive import DEFAULT_BASE_URL, MassiveStocksProvider
from market_data.providers.polygon import PolygonProvider
from persistence.technical_evidence_ledger import bar_content_hash
from tests.market_data_fakes import TEST_KEY, FakeResponse, FakeSession, aggregates_route, calendar_bars, payload, \
    to_results

CAL = default_calendar()
DAY = date(2026, 9, 23)
HOST = "https://api.massive.com"
ENV = dict(MARKET_DATA_PROVIDER="massive_stocks", MARKET_DATA_API_KEY=TEST_KEY, MARKET_DATA_DELAY_SECONDS="0",
           MARKET_DATA_RETRY_BACKOFF_SECONDS="0.5", MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS="0")


def et(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=EXCHANGE_TZ)


def massive(session, *, now=None, sleeps=None, **env):
    settings = load_market_data_settings(dict(ENV, **env))
    return MassiveStocksProvider(settings, calendar=CAL, session=session,
                                 sleep=sleeps.append if sleeps is not None else (lambda s: None),
                                 clock=lambda: now or et(date(2026, 12, 4), 20))


def polygon(session, *, now=None, **env):
    settings = load_market_data_settings(dict(ENV, MARKET_DATA_PROVIDER="polygon", **env))
    return PolygonProvider(settings, calendar=CAL, session=session, sleep=lambda s: None,
                           clock=lambda: now or et(date(2026, 12, 4), 20))


def ok(results, **kwargs):
    return FakeResponse(200, payload(results, **kwargs))


def fetch(session, label="5m", day=DAY, **env):
    return massive(session, **env).get_bars("META", label, et(day, 0), et(day + timedelta(days=1), 0))


class NoNetwork(unittest.TestCase):
    def run(self, result=None):
        with patch.object(socket, "socket", side_effect=OSError("network disabled in tests")), \
                patch.object(socket, "create_connection", side_effect=OSError("network disabled in tests")):
            return super().run(result)


class ConfigAndFactoryTests(NoNetwork):
    def test_massive_stocks_settings_and_factory(self):
        settings = load_market_data_settings(dict(MARKET_DATA_PROVIDER="massive_stocks", MARKET_DATA_API_KEY=TEST_KEY))
        self.assertEqual((settings.provider, settings.base_url, settings.delay_seconds), ("massive_stocks", HOST, 900))
        self.assertEqual(DEFAULT_BASE_URL, HOST)
        provider = build_provider(settings, calendar=CAL, session=FakeSession([]))
        self.assertIs(type(provider), MassiveStocksProvider)
        self.assertEqual(provider.settings.provider, "massive_stocks")
        self.assertEqual(settings.safe_view()["api_key"], "set")
        self.assertNotIn(TEST_KEY, repr(settings) + str(settings.safe_view()))

    def test_polygon_and_massive_alias_unchanged(self):
        for value in ("polygon", "massive", "Massive", "POLYGON"):
            settings = load_market_data_settings(dict(MARKET_DATA_PROVIDER=value, MARKET_DATA_API_KEY=TEST_KEY))
            self.assertEqual((settings.provider, settings.base_url), ("polygon", "https://api.polygon.io"))
            self.assertIs(type(build_provider(settings, calendar=CAL, session=FakeSession([]))), PolygonProvider)
        self.assertEqual(load_market_data_settings({}).provider, "none")
        with self.assertRaises(MarketDataConfigError):
            build_provider(load_market_data_settings({}))

    def test_requires_key_explicit_base_url_and_matching_provider(self):
        with self.assertRaises(MarketDataConfigError):
            load_market_data_settings(dict(MARKET_DATA_PROVIDER="massive_stocks"))
        for url in ("http://api.massive.com", f"{HOST}?apiKey=x"):
            with self.assertRaises(MarketDataConfigError):
                load_market_data_settings(dict(ENV, MARKET_DATA_BASE_URL=url))
        custom = load_market_data_settings(dict(ENV, MARKET_DATA_BASE_URL="https://proxy.example/"))
        self.assertEqual(custom.base_url, "https://proxy.example")
        settings = load_market_data_settings(ENV)
        with self.assertRaises(MarketDataError):
            MassiveStocksProvider(type(settings)(**{**settings.__dict__, "api_key": None}), calendar=CAL)
        with self.assertRaises(ValueError):
            MassiveStocksProvider(load_market_data_settings(dict(ENV, MARKET_DATA_PROVIDER="polygon")), calendar=CAL)


class NormalizationTests(NoNetwork):
    def test_exact_decimal_ohlcv_request_shape_and_bearer(self):
        bars = calendar_bars("META", [DAY], 5, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        session = FakeSession([ok(to_results(bars))])
        got = fetch(session)
        self.assertEqual(got, [b for b in bars if b.session is Session.REGULAR])
        self.assertTrue(all(isinstance(v, Decimal) for b in got for v in (b.open, b.high, b.low, b.close, b.volume)))
        call = session.calls[0]
        self.assertTrue(call["url"].startswith(f"{HOST}/v2/aggs/ticker/META/range/5/minute/"))
        self.assertEqual(call["params"], dict(adjusted="true", sort="asc", limit="50000"))
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {TEST_KEY}")
        self.assertEqual((call["allow_redirects"], call["timeout"]), (False, 10.0))
        self.assertNotIn(TEST_KEY, call["url"] + json.dumps(call["params"]))

    def test_fractional_volume_kept_exactly(self):
        results = to_results(calendar_bars("META", [DAY], 5)[:3])
        results[0]["v"], results[1]["v"] = 12345.6789, 0.25
        got = fetch(FakeSession([FakeResponse(200, text=json.dumps(payload(results)))]))
        self.assertEqual([b.volume for b in got[:2]], [Decimal("12345.6789"), Decimal("0.25")])

    def test_utc_milliseconds_to_new_york_across_dst(self):
        for days, opens in (([date(2026, 3, 6), date(2026, 3, 9)], [time(14, 30), time(13, 30)]),      # EST -> EDT
                            ([date(2026, 10, 30), date(2026, 11, 2)], [time(13, 30), time(14, 30)])):  # EDT -> EST
            bars = calendar_bars("META", days, 30)
            got = massive(FakeSession([ok(to_results(bars))])).get_bars("META", "30m", et(days[0], 0),
                                                                         et(days[1] + timedelta(days=1), 0))
            self.assertEqual([b.timestamp.astimezone(timezone.utc).time() for b in got
                              if b.timestamp.time() == time(9, 30)], opens)
            self.assertTrue(all(b.timestamp.tzinfo is EXCHANGE_TZ for b in got))
            self.assertEqual(got, bars)

    def test_early_close(self):
        half = date(2026, 11, 27)
        bars = calendar_bars("SPY", [half], 5, sessions=(Session.REGULAR, Session.POST))
        got = massive(FakeSession([ok(to_results(bars))])).get_bars("SPY", "5m", et(half, 0), et(half, 23))
        self.assertEqual((len(got), got[-1].timestamp.time()), (42, time(12, 55)))  # 13:00 close; post dropped.
        session = FakeSession(route=aggregates_route({30: calendar_bars("SPY", [half], 30)}))
        hours = massive(session).get_bars("SPY", "1h", et(half, 0), et(half, 23))
        self.assertEqual([h.timestamp.time() for h in hours], [time(9, 30), time(10, 30), time(11, 30), time(12, 30)])
        late = [dict(to_results(bars)[0], t=int(et(half, 17, 30).timestamp() * 1000))]
        with self.assertRaises(MarketDataError):  # After the half-day post session: outside every session.
            massive(FakeSession([ok(late)]), MARKET_DATA_INCLUDE_EXTENDED_HOURS="true").get_bars(
                "SPY", "5m", et(half, 0), et(half, 23))

    def test_supported_session_filter_and_20_00_exclusion(self):
        bars = calendar_bars("SPY", [DAY], 5, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        results = to_results(bars)
        results += [dict(results[0], t=int(et(DAY, 20).timestamp() * 1000)),
                    dict(results[0], t=int(et(DAY, 23, 30).timestamp() * 1000))]
        p = massive(FakeSession([ok(results)]), MARKET_DATA_INCLUDE_EXTENDED_HOURS="true")
        got = p.get_bars("SPY", "5m", et(DAY, 0), et(DAY + timedelta(days=1), 0))
        self.assertEqual(got, bars)
        self.assertEqual((got[0].timestamp.time(), got[-1].timestamp.time()), (time(4), time(19, 55)))
        diag = p.diagnostics[-1]
        self.assertEqual((diag["provider"], diag["excluded_overnight_bars"], diag["raw_bars"]),
                         ("massive_stocks", 2, len(bars)))
        regular = fetch(FakeSession([ok(results)]))
        self.assertEqual({b.session for b in regular}, {Session.REGULAR})

    def test_derived_hour_and_day_follow_mias_semantics(self):
        days = [date(2026, 9, 21), date(2026, 9, 22), DAY]
        source = calendar_bars("NVDA", days, 30, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        session = FakeSession(route=aggregates_route({30: source}))
        p = massive(session, now=et(DAY, 14, 10), MARKET_DATA_INCLUDE_EXTENDED_HOURS="true")
        daily = p.get_bars("NVDA", "1d", et(days[0], 0), et(DAY, 0) + timedelta(days=1))
        self.assertEqual([b.timestamp for b in daily], [et(d, 0) for d in days[:2]])  # Today still open.
        regular = [b for b in source if b.session is Session.REGULAR and b.timestamp.date() == days[0]]
        self.assertEqual((daily[0].open, daily[0].close, daily[0].volume),
                         (regular[0].open, regular[-1].close, sum(b.volume for b in regular)))  # Regular only.
        self.assertTrue(all("/range/30/minute/" in c["url"] for c in session.calls))  # Never vendor daily/hourly.
        hours = p.get_bars("NVDA", "1h", et(DAY, 9, 30), et(DAY, 16))
        self.assertEqual(hours[0].timestamp, et(DAY, 9, 30))


class EnvelopeAndValidationTests(NoNetwork):
    def test_empty_and_missing_results(self):
        for body in (payload([]), dict(status="OK", resultsCount=0), dict(status="DELAYED", results=[])):
            with self.subTest(body=body):
                self.assertEqual(fetch(FakeSession([FakeResponse(200, body)])), [])

    def test_malformed_envelopes_raise_payload_errors(self):
        for response in (FakeResponse(200, text="{not json"), FakeResponse(200, dict(status="ERROR", error="x")),
                         FakeResponse(200, dict(status="NOT_AUTHORIZED")), FakeResponse(200, dict(status="OK", results={})),
                         FakeResponse(200, [])):
            with self.subTest(text=response.text[:40]), self.assertRaises(ProviderError) as caught:
                fetch(FakeSession([response]))
            self.assertEqual(caught.exception.kind, "payload")

    def test_invalid_bars_rejected_not_repaired(self):
        good = to_results(calendar_bars("META", [DAY], 5)[:3])
        cases = dict(duplicate=[good[0], good[0], good[1]], out_of_order=[good[1], good[0]],
                     missing_open=[{k: v for k, v in good[0].items() if k != "o"}],
                     missing_volume=[{k: v for k, v in good[0].items() if k != "v"}],
                     missing_timestamp=[{k: v for k, v in good[0].items() if k != "t"}],
                     float_timestamp=[dict(good[0], t=good[0]["t"] + 0.5)], string_timestamp=[dict(good[0], t="x")],
                     bool_timestamp=[dict(good[0], t=True)], string_price=[dict(good[0], c="740")],
                     negative_volume=[dict(good[0], v=-1)], invalid_ohlc=[dict(good[0], h=float(good[0]["l"]) - 1)],
                     off_grid=[dict(good[0], t=good[0]["t"] + 60_000)],
                     weekend=[dict(good[0], t=int(et(date(2026, 9, 26), 10).timestamp() * 1000))])
        for name, results in cases.items():
            with self.subTest(case=name), self.assertRaises(MarketDataError):
                fetch(FakeSession([ok(results)]))

    def test_pagination_same_host_only(self):
        bars = calendar_bars("META", [DAY], 5)
        first, second = to_results(bars[:40]), to_results(bars[40:])
        session = FakeSession([ok(first, next_url=f"{HOST}/v2/aggs/cursor/abc"), ok(second)])
        p = massive(session)
        self.assertEqual(p.get_bars("META", "5m", et(DAY, 0), et(DAY, 23)), bars)
        self.assertEqual((session.calls[1]["url"], session.calls[1]["params"]), (f"{HOST}/v2/aggs/cursor/abc", None))
        self.assertEqual(session.calls[1]["headers"]["Authorization"], f"Bearer {TEST_KEY}")
        self.assertEqual((p.diagnostics[-1]["pages"], p.diagnostics[-1]["next_url_hosts"]), (2, ["api.massive.com"]))
        for foreign in ("https://api.polygon.io/v2/aggs/cursor/abc", "https://evil.example/x",
                        "http://api.massive.com/v2/aggs/cursor/abc", 42):
            session = FakeSession([ok(first, next_url=foreign)])
            with self.subTest(next_url=foreign), self.assertRaises(ProviderError) as caught:
                massive(session).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
            self.assertEqual((caught.exception.kind, len(session.calls)), ("payload", 1))

    def test_optional_vw_and_n_are_counted_not_exposed(self):
        results = to_results(calendar_bars("META", [DAY], 5)[:4])
        del results[0]["vw"], results[1]["n"]
        p = massive(FakeSession([ok(results)]))
        bars = p.get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
        self.assertEqual((p.diagnostics[-1]["results_with_vw"], p.diagnostics[-1]["results_with_n"]), (3, 3))
        self.assertFalse(any(hasattr(b, "vwap") or hasattr(b, "trade_count") for b in bars))
        polygon_bars = polygon(FakeSession([ok(results)])).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
        self.assertEqual(bars, polygon_bars)


class FailureTests(NoNetwork):
    def setUp(self):
        http_logger = logging.getLogger("market_data.http")
        http_logger.setLevel(logging.ERROR)  # Expected retry warnings; their safety is asserted in KeyHygieneTests.
        self.addCleanup(http_logger.setLevel, logging.NOTSET)

    def run_failure(self, responses, retries="3"):
        session, sleeps = FakeSession(responses), []
        with self.assertRaises(ProviderError) as caught:
            massive(session, sleeps=sleeps, MARKET_DATA_MAX_RETRIES=retries).get_bars("META", "5m", et(DAY, 0),
                                                                                      et(DAY, 23))
        return caught.exception, session, sleeps

    def test_401_and_403_not_retried(self):
        for status in (401, 403):
            error, session, sleeps = self.run_failure([FakeResponse(status, dict(status="NOT_AUTHORIZED"))])
            self.assertEqual((error.kind, error.status, len(session.calls), sleeps), ("auth", status, 1, []))

    def test_429_retry_after_and_bounded(self):
        bars = to_results(calendar_bars("META", [DAY], 5))
        session, sleeps = FakeSession([FakeResponse(429, headers={"Retry-After": "7"}), ok(bars)]), []
        self.assertEqual(len(massive(session, sleeps=sleeps).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))), 78)
        self.assertEqual(sleeps, [7.0])
        error, session, _ = self.run_failure([FakeResponse(429)] * 3, retries="2")
        self.assertEqual((error.kind, len(session.calls)), ("rate_limit", 3))

    def test_5xx_timeout_and_connection_errors_bounded(self):
        bars = to_results(calendar_bars("META", [DAY], 5))
        session, sleeps = FakeSession([FakeResponse(502), requests.Timeout("t"), requests.ConnectionError("c"),
                                       ok(bars)]), []
        self.assertEqual(len(massive(session, sleeps=sleeps).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))), 78)
        self.assertEqual(sleeps, [0.5, 1.0, 2.0])
        for failure, kind in ((FakeResponse(503), "http"), (requests.Timeout("t"), "transport"),
                              (requests.ConnectionError("c"), "transport")):
            error, session, _ = self.run_failure([failure] * 2, retries="1")
            self.assertEqual((error.kind, len(session.calls)), (kind, 2))

    def test_redirect_and_other_4xx_not_retried(self):
        for response in (FakeResponse(301, headers={"Location": "https://evil"}), FakeResponse(404)):
            error, session, sleeps = self.run_failure([response])
            self.assertEqual((len(session.calls), sleeps), (1, []))


class KeyHygieneTests(NoNetwork):
    def test_canary_key_never_leaks(self):
        bars = calendar_bars("META", [DAY], 5)
        texts = []
        with self.assertLogs("market_data", logging.DEBUG) as logs:
            p = massive(FakeSession([FakeResponse(500), ok(to_results(bars[:40]), next_url=f"{HOST}/v2/aggs/cursor/c"),
                                     ok(to_results(bars[40:]))]))
            got = p.get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
            for responses in ([FakeResponse(401)], [FakeResponse(200, text="{bad")], [requests.Timeout("t")] * 4,
                              [ok([], next_url="https://evil.example/x")], [ok([dict(t="x")])]):
                try:
                    massive(FakeSession(responses)).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
                except (ProviderError, MarketDataError) as error:
                    texts += [str(error), repr(error)]
        session_urls = [c["url"] for c in p.http._session.calls] + [json.dumps(c["params"]) for c in p.http._session.calls]
        texts += logs.output + session_urls + [json.dumps(p.diagnostics), str(p.settings.safe_view()), repr(p.settings),
                                               repr(got), json.dumps([b.to_dict() for b in got])]
        self.assertEqual(len(texts) > 10, True)
        for text in texts:
            self.assertNotIn(TEST_KEY, text)
            self.assertNotIn("apiKey", text)
        self.assertTrue(all(c["headers"]["Authorization"] == f"Bearer {TEST_KEY}" for c in p.http._session.calls))
        self.assertIn("event=market_data_fetch provider=massive_stocks symbol=META interval=5m pages=2 bars=78",
                      "\n".join(logs.output))


class PolygonParityTests(NoNetwork):
    """Identical vendor payloads give identical MarketBars and identical Phase 6 bar_content_hash."""

    def test_identical_bars_and_hashes_for_every_interval(self):
        days = CAL.trading_days(date(2026, 11, 20), date(2026, 12, 2))  # Includes Thanksgiving and the half day.
        data = {5: calendar_bars("META", days, 5, sessions=(Session.PRE, Session.REGULAR, Session.POST)),
                30: calendar_bars("META", days, 30, sessions=(Session.PRE, Session.REGULAR, Session.POST))}
        for label in ("5m", "30m", "1h", "1d"):
            args = ("META", label, et(days[0], 0), et(days[-1] + timedelta(days=1), 0))
            m = massive(FakeSession(route=aggregates_route(data))).get_bars(*args)
            p = polygon(FakeSession(route=aggregates_route(data))).get_bars(*args)
            self.assertEqual(m, p, label)
            self.assertTrue(m, label)
            for day in days:
                session_bars = [b for b in m if b.timestamp.astimezone(EXCHANGE_TZ).date() == day]
                other = [b for b in p if b.timestamp.astimezone(EXCHANGE_TZ).date() == day]
                self.assertEqual(bar_content_hash(session_bars, symbol="META", interval=label, session_date=day),
                                 bar_content_hash(other, symbol="META", interval=label, session_date=day))
        self.assertEqual(len([b for b in m if b.timestamp.date() == date(2026, 11, 27)]), 1)

    def test_identical_rejections(self):
        good = to_results(calendar_bars("META", [DAY], 5)[:3])
        for results in ([good[1], good[0]], [dict(good[0], t=1.5)], [{k: v for k, v in good[0].items() if k != "c"}]):
            outcomes = []
            for make in (massive, polygon):
                try:
                    make(FakeSession([ok(results)])).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
                except MarketDataError as error:
                    outcomes.append(str(error))
            self.assertEqual(len(outcomes), 2)
            self.assertEqual(outcomes[0], outcomes[1])

    def test_polygon_log_line_unchanged(self):
        with self.assertLogs("market_data", "INFO") as logs:
            polygon(FakeSession([ok(to_results(calendar_bars("META", [DAY], 5)))])).get_bars(
                "META", "5m", et(DAY, 0), et(DAY, 23))
        self.assertIn("event=market_data_fetch provider=polygon symbol=META interval=5m bars=78", "\n".join(logs.output))


class LiveCheckCommandTests(NoNetwork):
    """``market_data.massive_check`` against a fake Massive host (the real check is user-run only)."""
    NOW = et(date(2026, 9, 24), 11, 2)

    def route(self, bars):
        cursor = f"{HOST}/v2/aggs/cursor/page2"

        def handle(url, params):
            if url == cursor:
                return ok(self.second)
            response = aggregates_route(bars)(url, params)
            body = json.loads(response.text)
            if params and params["limit"] != "50000" and len(body["results"]) > 20:
                self.second = body["results"][20:]
                return ok(body["results"][:20], next_url=cursor)
            return FakeResponse(200, dict(body, status="DELAYED", adjusted=True))
        return handle

    def run_check(self, argv=(), environ=None, session=None):
        from market_data import massive_check
        days = [date(2026, 9, 23), date(2026, 9, 24)]
        data = {5: calendar_bars("META", days, 5, sessions=(Session.PRE, Session.REGULAR, Session.POST)),
                30: calendar_bars("META", days, 30, sessions=(Session.PRE, Session.REGULAR, Session.POST))}
        data = {k: [b for b in v if b.timestamp + b.interval.delta <= self.NOW] for k, v in data.items()}
        session = session or FakeSession(route=self.route(data))
        out = io.StringIO()
        code = massive_check.main(list(argv), environ=dict(ENV) if environ is None else environ, session=session,
                                  clock=lambda: self.NOW, sleep=lambda s: None, out=out)
        return code, out.getvalue(), session

    def test_passes_and_reports_contract_metadata_only(self):
        code, text, session = self.run_check()
        self.assertEqual(code, 0, text)
        report = json.loads(text)
        self.assertEqual((report["live_validation"], report["host"], report["next_url_hosts"]),
                         ("PASSED", "api.massive.com", ["api.massive.com"]))
        self.assertTrue(report["pagination_observed"])
        self.assertEqual(report["fetches"]["pagination_probe_5m"]["pages"], 2)
        self.assertEqual(report["statuses"], ["DELAYED", "OK"])
        self.assertEqual(report["fetches"]["range_5m"]["latest_bar_end"], et(date(2026, 9, 24), 11).isoformat())
        self.assertEqual(report["fetches"]["range_5m"]["observed_age_seconds"], 120)
        self.assertEqual(report["optional_fields"]["range_30m"]["with_vw"], report["optional_fields"]["range_30m"]["results"])
        self.assertEqual(report["requests"], len(session.calls))
        self.assertLessEqual(len(session.calls), 8)
        self.assertTrue(all(c["headers"]["Authorization"] == f"Bearer {TEST_KEY}" for c in session.calls))
        self.assertTrue(all(c["url"].startswith(HOST) for c in session.calls))
        self.assertNotIn(TEST_KEY, text)
        self.assertNotIn('"o"', text)  # No payloads or prices.

    def test_not_executed_without_massive_stocks_and_on_usage_errors(self):
        for environ in ({}, dict(ENV, MARKET_DATA_PROVIDER="polygon"), dict(ENV, MARKET_DATA_PROVIDER="massive")):
            session = FakeSession([])
            code, text, _ = self.run_check(environ=environ, session=session)
            self.assertEqual((code, json.loads(text)["live_validation"], session.calls), (2, "NOT EXECUTED", []))
        for argv in (["--days", "9"], ["--max-requests", "50"], ["--pagination-limit", "1"]):
            self.assertEqual(self.run_check(argv, session=FakeSession([]))[0], 2)

    def test_failures_are_reported_safely_and_budget_is_hard(self):
        code, text, session = self.run_check(session=FakeSession([FakeResponse(403, dict(status="NOT_AUTHORIZED"))]))
        self.assertEqual((code, json.loads(text)["kind"], len(session.calls)), (1, "auth", 1))
        self.assertNotIn(TEST_KEY, text)
        code, text, session = self.run_check(["--max-requests", "1"])
        self.assertEqual((code, json.loads(text)["kind"]), (1, "budget"))
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
