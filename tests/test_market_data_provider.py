"""Phase 4B Polygon/Massive provider, HTTP behaviour and configuration, all against a fake HTTP layer (no live calls)."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import logging
import unittest

import requests

from market_data.calendar import default_calendar
from market_data.config import MarketDataConfigError, load_market_data_settings
from market_data.http import JsonHttpClient, ProviderError
from market_data.aggregation import aggregate
from market_data.models import EXCHANGE_TZ, Interval, MarketDataError, Session
from market_data.providers import build_provider
from market_data.providers.polygon import PolygonProvider
from tests.market_data_fakes import (TEST_KEY, FakeResponse, FakeSession, aggregates_route, calendar_bars, payload,
                                     to_results)

CAL = default_calendar()
DAY = date(2026, 9, 23)
ENV = dict(MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY, MARKET_DATA_DELAY_SECONDS="0",
           MARKET_DATA_RETRY_BACKOFF_SECONDS="0.5")


def et(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=EXCHANGE_TZ)


def provider(session, *, now=None, sleeps=None, **env):
    settings = load_market_data_settings(dict(ENV, **env))
    return PolygonProvider(settings, calendar=CAL, session=session, sleep=(sleeps.append if sleeps is not None else
                                                                             (lambda s: None)),
                           clock=lambda: now or et(date(2026, 9, 25), 20))


def ok(results, **kwargs):
    return FakeResponse(200, payload(results, **kwargs))


class ConfigTests(unittest.TestCase):
    def test_defaults_and_none_provider(self):
        settings = load_market_data_settings({})
        self.assertEqual((settings.provider, settings.api_key, settings.base_url), ("none", None, None))
        with self.assertRaises(MarketDataConfigError):
            build_provider(settings)

    def test_polygon_settings(self):
        settings = load_market_data_settings(dict(MARKET_DATA_PROVIDER="Massive", MARKET_DATA_API_KEY=TEST_KEY))
        self.assertEqual((settings.provider, settings.base_url, settings.timeout_seconds, settings.max_retries,
                          settings.delay_seconds, settings.adjusted, settings.include_extended_hours),
                         ("polygon", "https://api.polygon.io", 10.0, 3, 900, True, False))
        self.assertNotIn(TEST_KEY, repr(settings))
        self.assertNotIn(TEST_KEY, str(settings.safe_view()))
        self.assertEqual(settings.safe_view()["api_key"], "set")

    def test_pacing_settings(self):
        settings = load_market_data_settings(dict(MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY))
        self.assertEqual((settings.min_request_interval_seconds, settings.rate_limit_fallback_wait_seconds), (12.0, 15.0))
        custom = load_market_data_settings(dict(ENV, MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS="0",
                                                MARKET_DATA_RATE_LIMIT_FALLBACK_WAIT_SECONDS="30"))
        self.assertEqual((custom.min_request_interval_seconds, custom.rate_limit_fallback_wait_seconds), (0.0, 30.0))
        for env in (dict(ENV, MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS="121"),
                    dict(ENV, MARKET_DATA_RATE_LIMIT_FALLBACK_WAIT_SECONDS="-1")):
            with self.assertRaises(MarketDataConfigError):
                load_market_data_settings(env)

    def test_invalid_settings(self):
        bad = [dict(MARKET_DATA_PROVIDER="yahoo"), dict(MARKET_DATA_PROVIDER="polygon"),
               dict(ENV, MARKET_DATA_HTTP_TIMEOUT_SECONDS="0"), dict(ENV, MARKET_DATA_MAX_RETRIES="9"),
               dict(ENV, MARKET_DATA_MAX_RETRIES="x"), dict(ENV, MARKET_DATA_BASE_URL="http://api.polygon.io"),
               dict(ENV, MARKET_DATA_BASE_URL="https://api.polygon.io?apiKey=1"), dict(ENV, MARKET_DATA_ADJUSTED="yes"),
               dict(ENV, MARKET_DATA_DELAY_SECONDS="-1")]
        for environ in bad:
            with self.subTest(environ={k: v for k, v in environ.items() if k != "MARKET_DATA_API_KEY"}):
                with self.assertRaises(MarketDataConfigError) as caught:
                    load_market_data_settings(environ)
                self.assertNotIn(TEST_KEY, str(caught.exception))


class HttpTests(unittest.TestCase):
    def client(self, session, sleeps, retries=3):
        return JsonHttpClient(headers={"Authorization": f"Bearer {TEST_KEY}"}, timeout_seconds=7, max_retries=retries,
                              backoff_seconds=0.5, max_rate_limit_wait_seconds=30, session=session, sleep=sleeps.append)

    def test_success_parses_decimals_and_sends_safe_request(self):
        session, sleeps = FakeSession([FakeResponse(200, text='{"status": "OK", "x": 740.105}')]), []
        self.assertEqual(self.client(session, sleeps).get_json("https://api.polygon.io/v2/x", dict(a="1"))["x"],
                         Decimal("740.105"))
        call = session.calls[0]
        self.assertEqual((call["timeout"], call["allow_redirects"]), (7, False))
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {TEST_KEY}")

    def test_auth_failures_are_not_retried(self):
        for status in (401, 403):
            session, sleeps = FakeSession([FakeResponse(status)]), []
            with self.assertRaises(ProviderError) as caught:
                self.client(session, sleeps).get_json("https://api.polygon.io/v2/x")
            self.assertEqual((caught.exception.kind, len(session.calls), sleeps), ("auth", 1, []))

    def test_rate_limit_honours_retry_after_with_cap(self):
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "12"}),
                               FakeResponse(429, headers={"Retry-After": "999"}), FakeResponse(200, {"status": "OK"})])
        sleeps = []
        self.assertEqual(self.client(session, sleeps).get_json("https://api.polygon.io/v2/x"), {"status": "OK"})
        self.assertEqual(sleeps, [12.0, 30])

    def test_rate_limit_exhausted(self):
        session, sleeps = FakeSession([FakeResponse(429)] * 4), []
        with self.assertRaises(ProviderError) as caught:
            self.client(session, sleeps).get_json("https://api.polygon.io/v2/x")
        self.assertEqual((caught.exception.kind, caught.exception.status, len(session.calls)), ("rate_limit", 429, 4))
        self.assertEqual(sleeps, [0.5, 1.0, 2.0])

    def test_transient_server_errors_and_timeouts_retry_boundedly(self):
        session = FakeSession([FakeResponse(503), requests.Timeout("slow"), requests.ConnectionError("reset"),
                               FakeResponse(200, {"status": "OK"})])
        sleeps = []
        self.assertEqual(self.client(session, sleeps).get_json("https://api.polygon.io/v2/x"), {"status": "OK"})
        self.assertEqual(len(sleeps), 3)
        session = FakeSession([requests.Timeout("slow")] * 2)
        with self.assertRaises(ProviderError) as caught:
            self.client(session, [], retries=1).get_json("https://api.polygon.io/v2/x")
        self.assertEqual((caught.exception.kind, len(session.calls)), ("transport", 2))

    def test_permanent_errors_are_not_retried(self):
        for response in (FakeResponse(404), FakeResponse(400), FakeResponse(301, headers={"Location": "https://evil"}),
                         FakeResponse(200, text="{not json")):
            session, sleeps = FakeSession([response]), []
            with self.assertRaises(ProviderError) as caught:
                self.client(session, sleeps).get_json("https://api.polygon.io/v2/aggs?apiKey=zzz")
            self.assertEqual((len(session.calls), sleeps), (1, []))
            self.assertNotIn("apiKey", str(caught.exception))
            self.assertNotIn(TEST_KEY, str(caught.exception))

    def test_pacing_spaces_requests_including_retries(self):
        clock, sleeps = [100.0], []

        def sleep(seconds):
            sleeps.append(round(seconds, 6))
            clock[0] += seconds
        session = FakeSession([FakeResponse(200, {"status": "OK"}), FakeResponse(503), FakeResponse(200, {"status": "OK"})])
        client = JsonHttpClient(headers={}, timeout_seconds=5, max_retries=2, backoff_seconds=0.5,
                                max_rate_limit_wait_seconds=30, session=session, sleep=sleep, min_interval_seconds=12,
                                clock=lambda: clock[0])
        client.get_json("https://api.polygon.io/a")
        clock[0] += 3  # 3 s of other work before the next request.
        client.get_json("https://api.polygon.io/b")
        # Pace 9 s before request 2; its 503 backs off 0.5 s; then pace the remaining 11.5 s before the retry.
        self.assertEqual(sleeps, [9.0, 0.5, 11.5])
        self.assertEqual(client.paced_seconds, 20.5)

    def test_rate_limit_without_retry_after_uses_fallback_wait(self):
        session, sleeps = FakeSession([FakeResponse(429), FakeResponse(429, headers={"Retry-After": "2"}),
                                       FakeResponse(200, {"status": "OK"})]), []
        client = JsonHttpClient(headers={}, timeout_seconds=5, max_retries=3, backoff_seconds=0.5,
                                max_rate_limit_wait_seconds=30, session=session, sleep=sleeps.append,
                                rate_limit_fallback_seconds=15)
        with self.assertLogs("market_data.http", "WARNING"):
            client.get_json("https://api.polygon.io/v2/x")
        self.assertEqual(sleeps, [15, 2.0])  # No header: fallback wait. Header present: honoured.

    def test_retry_logs_are_credential_safe(self):
        session = FakeSession([FakeResponse(500), FakeResponse(200, {"status": "OK"})])
        with self.assertLogs("market_data.http", "WARNING") as logs:
            self.client(session, []).get_json("https://api.polygon.io/v2/aggs/ticker/META?apiKey=secret")
        text = "\n".join(logs.output)
        self.assertIn("target=api.polygon.io/v2/aggs/ticker/META", text)
        self.assertNotIn("secret", text)
        self.assertNotIn(TEST_KEY, text)


class ProviderTests(unittest.TestCase):
    def test_successful_response_normalizes_bars(self):
        bars = calendar_bars("META", [DAY], 5, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        session = FakeSession([ok(to_results(bars))])
        got = provider(session).get_bars("META", "5m", et(DAY, 0), et(DAY + timedelta(days=1), 0))
        regular = [b for b in bars if b.session is Session.REGULAR]
        self.assertEqual(got, regular)  # Extended hours dropped by default; exact Decimal prices; ET timestamps.
        self.assertEqual(got[0].timestamp.astimezone(timezone.utc), datetime(2026, 9, 23, 13, 30, tzinfo=timezone.utc))
        self.assertEqual(got[0].timestamp.tzinfo, EXCHANGE_TZ)  # Normalized to exchange time.
        call = session.calls[0]
        self.assertIn("/v2/aggs/ticker/META/range/5/minute/", call["url"])
        self.assertEqual(call["params"], dict(adjusted="true", sort="asc", limit="50000"))
        self.assertNotIn(TEST_KEY, call["url"])

    def test_extended_hours_kept_when_configured(self):
        bars = calendar_bars("META", [DAY], 30, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        got = provider(FakeSession([ok(to_results(bars))]), MARKET_DATA_INCLUDE_EXTENDED_HOURS="true").get_bars(
            "META", "30m", et(DAY, 0), et(DAY, 23))
        self.assertEqual({b.session for b in got}, {Session.PRE, Session.REGULAR, Session.POST})

    def test_interval_mapping(self):
        for label, path in (("1m", "/range/1/minute/"), ("5m", "/range/5/minute/"), ("15m", "/range/15/minute/"),
                            ("30m", "/range/30/minute/"), ("1h", "/range/30/minute/"), ("1d", "/range/30/minute/")):
            session = FakeSession([ok([])])
            provider(session).get_bars("NVDA", label, et(DAY, 9, 30), et(DAY, 16))
            self.assertIn(path, session.calls[0]["url"], label)
        with self.assertRaises(MarketDataError):
            provider(FakeSession([])).get_bars("NVDA", "2h", et(DAY, 9, 30), et(DAY, 16))

    def test_derived_hour_and_day(self):
        days = [date(2026, 9, 21), date(2026, 9, 22), DAY]
        source = calendar_bars("NVDA", days, 30)
        p = provider(FakeSession(route=aggregates_route({30: source})), now=et(DAY, 14, 10))
        hours = p.get_bars("NVDA", "1h", et(days[0], 0), et(DAY, 23))
        self.assertEqual(hours[-1].timestamp, et(DAY, 12, 30))  # 13:30 bucket still forming at 14:10.
        self.assertEqual(len(hours), 7 + 7 + 4)
        daily = p.get_bars("NVDA", "1d", et(days[0], 0), et(DAY, 0) + timedelta(days=1))
        self.assertEqual([b.timestamp.date() for b in daily], days[:2])  # Today's session has not closed.

    def test_pagination_follows_next_url_on_same_host_only(self):
        bars = calendar_bars("META", [DAY], 5)
        first, second = to_results(bars[:40]), to_results(bars[40:])
        session = FakeSession([ok(first, next_url="https://api.polygon.io/v2/aggs/cursor/abc"), ok(second)])
        got = provider(session).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
        self.assertEqual(len(got), 78)
        self.assertEqual((session.calls[1]["url"], session.calls[1]["params"]), ("https://api.polygon.io/v2/aggs/cursor/abc", None))
        self.assertEqual(session.calls[1]["headers"]["Authorization"], f"Bearer {TEST_KEY}")
        evil = FakeSession([ok(first, next_url="https://evil.example/v2/aggs/cursor/abc")])
        with self.assertRaises(ProviderError):
            provider(evil).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
        self.assertEqual(len(evil.calls), 1)

    def test_malformed_and_invalid_data_is_rejected_not_repaired(self):
        good = to_results(calendar_bars("META", [DAY], 5)[:3])
        cases = dict(
            duplicate=[good[0], good[0], good[1]], out_of_order=[good[1], good[0]],
            invalid_ohlc=[dict(good[0], h=float(good[0]["l"]) - 1)], negative_volume=[dict(good[0], v=-5)],
            negative_fractional_volume=[dict(good[0], v=-0.5)], string_volume=[dict(good[0], v="10")],
            null_volume=[dict(good[0], v=None)], missing_field=[{k: v for k, v in good[0].items() if k != "c"}],
            string_price=[dict(good[0], o="740")], float_timestamp=[dict(good[0], t=good[0]["t"] + 0.5)],
            holiday=[dict(good[0], t=int(et(date(2026, 9, 7), 10).timestamp() * 1000))],
            misaligned=[dict(good[0], t=good[0]["t"] + 60_000)])
        for name, results in cases.items():
            with self.subTest(case=name), self.assertRaises(MarketDataError):
                provider(FakeSession([ok(results)])).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
        for body in (dict(status="ERROR", error="bad"), dict(status="OK", results={}), []):
            with self.subTest(body=body), self.assertRaises(ProviderError):
                provider(FakeSession([FakeResponse(200, body)])).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))

    def test_fractional_volume_is_kept_exactly(self):
        """Live check (Phase 4C): vendor aggregates include fractional-share volume; it is valid and never rounded."""
        results = to_results(calendar_bars("META", [DAY], 5)[:3])
        results[0]["v"], results[1]["v"] = 12345.6789, 0.25
        session = FakeSession([FakeResponse(200, text=__import__("json").dumps(payload(results)))])
        p = provider(session)
        bars = p.get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
        self.assertEqual([b.volume for b in bars[:2]], [Decimal("12345.6789"), Decimal("0.25")])
        self.assertEqual(bars[2].volume, results[2]["v"])
        diag = p.diagnostics[-1]
        self.assertEqual((diag["fractional_volumes"], diag["max_fractional_part"]), (2, "0.6789"))
        self.assertEqual(diag["volume_json_types"], ["Decimal", "int"])
        nan = FakeSession([FakeResponse(200, text='{"status": "OK", "results": [{"t": %d, "o": 1, "h": 1, "l": 1, '
                                              '"c": 1, "v": NaN}]}' % results[0]["t"])])
        with self.assertRaises(MarketDataError):
            provider(nan).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))

    def overnight_payload(self, extra):
        """A regular 5m day (09:30-15:55) plus vendor bars at the given ET times, in timestamp order."""
        bars = calendar_bars("SPY", [DAY], 5, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        results = to_results(bars)
        template = dict(results[0])
        for hour, minute, day in extra:
            stamp = int(et(day, hour, minute).timestamp() * 1000)
            results.append(dict(template, t=stamp))
        return sorted(results, key=lambda r: r["t"]), bars

    def test_overnight_vendor_bars_are_excluded_and_counted(self):
        results, bars = self.overnight_payload([(20, 0, DAY), (23, 30, DAY), (3, 55, DAY + timedelta(days=1))])
        p = provider(FakeSession([ok(results)]), MARKET_DATA_INCLUDE_EXTENDED_HOURS="true")
        got = p.get_bars("SPY", "5m", et(DAY, 0), et(DAY + timedelta(days=1), 12))
        self.assertEqual(got, bars)  # The series stays valid: every supported-session bar, nothing else.
        diag = p.diagnostics[-1]
        self.assertEqual((diag["excluded_overnight_bars"], diag["raw_bars"]), (3, len(bars)))
        self.assertTrue(all(time(4) <= b.timestamp.time() < time(20) for b in got))

    def test_session_boundaries_are_explicit(self):
        bars = calendar_bars("SPY", [DAY], 5, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        times = {b.timestamp.time() for b in bars}
        self.assertIn(time(4, 0), times)                 # 04:00 is the first pre-market bar.
        self.assertIn(time(19, 55), times)               # 19:55 is the last post-market bar.
        results, _ = self.overnight_payload([(20, 0, DAY)])
        p = provider(FakeSession([ok(results)]), MARKET_DATA_INCLUDE_EXTENDED_HOURS="true")
        got = p.get_bars("SPY", "5m", et(DAY, 0), et(DAY + timedelta(days=1), 0))
        self.assertEqual((got[0].timestamp.time(), got[0].session), (time(4, 0), Session.PRE))
        self.assertEqual((got[-1].timestamp.time(), got[-1].session), (time(19, 55), Session.POST))
        self.assertEqual(p.diagnostics[-1]["excluded_overnight_bars"], 1)  # 20:00 itself is overnight.

    def test_contract_violations_inside_supported_hours_still_rejected(self):
        good = to_results(calendar_bars("SPY", [DAY], 5)[:3])
        cases = dict(
            holiday=[dict(good[0], t=int(et(date(2026, 9, 7), 10).timestamp() * 1000))],
            weekend=[dict(good[0], t=int(et(date(2026, 9, 26), 10).timestamp() * 1000))],
            half_day_after_close=[dict(good[0], t=int(et(date(2026, 11, 27), 17, 30).timestamp() * 1000))],
            off_grid=[dict(good[0], t=good[0]["t"] + 60_000)],
            duplicate_overnight=[dict(good[0], t=int(et(DAY, 21).timestamp() * 1000))] * 2)
        for name, results in cases.items():
            with self.subTest(case=name), self.assertRaises(MarketDataError):
                provider(FakeSession([ok(results)]), MARKET_DATA_INCLUDE_EXTENDED_HOURS="true").get_bars(
                    "SPY", "5m", et(date(2026, 9, 1), 0), et(date(2026, 12, 1), 0))

    def test_no_overnight_bars_means_no_change(self):
        from market_data.providers.polygon import exclude_unsupported_sessions
        bars = calendar_bars("META", [DAY, date(2026, 9, 24)], 5, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        kept, excluded = exclude_unsupported_sessions(bars)
        self.assertEqual((kept, excluded), (bars, 0))
        daily = aggregate(bars, "1d", CAL)
        self.assertEqual(exclude_unsupported_sessions(daily), (daily, 0))  # Daily bars are never session-filtered.
        p = provider(FakeSession([ok(to_results(bars))]))
        self.assertEqual(p.get_bars("META", "5m", et(DAY, 0), et(date(2026, 9, 25), 0)),
                         [b for b in bars if b.session is Session.REGULAR])
        self.assertEqual(p.diagnostics[-1]["excluded_overnight_bars"], 0)

    def test_derived_intervals_never_see_overnight_bars(self):
        source = calendar_bars("SPY", [DAY], 30, sessions=(Session.PRE, Session.REGULAR, Session.POST))
        results = sorted(to_results(source) + [dict(to_results(source)[0],
                                                     t=int(et(DAY, 20, 30).timestamp() * 1000))], key=lambda r: r["t"])
        p = provider(FakeSession([ok(results)]), MARKET_DATA_INCLUDE_EXTENDED_HOURS="true")
        hours = p.get_bars("SPY", "1h", et(DAY, 0), et(DAY + timedelta(days=1), 0))
        self.assertEqual(hours, aggregate(source, "1h", CAL))
        self.assertEqual(p.diagnostics[-1]["excluded_overnight_bars"], 1)

    def test_timezone_normalization(self):
        bars = calendar_bars("META", [date(2026, 3, 6), date(2026, 3, 9)], 30)
        got = provider(FakeSession([ok(to_results(bars))])).get_bars("META", "30m", et(date(2026, 3, 6), 0),
                                                                     et(date(2026, 3, 10), 0))
        opens = [b.timestamp.astimezone(timezone.utc).time() for b in got if b.timestamp.astimezone(EXCHANGE_TZ).time() == time(9, 30)]
        self.assertEqual(opens, [time(14, 30), time(13, 30)])  # EST then EDT.

    def test_completed_bars_only_and_latest(self):
        bars = calendar_bars("META", [DAY], 5)
        now = et(DAY, 10, 32)
        got = provider(FakeSession([ok(to_results(bars))]), now=now).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
        self.assertEqual(got[-1].timestamp, et(DAY, 10, 25))
        delayed = provider(FakeSession([ok(to_results(bars))]), now=now, MARKET_DATA_DELAY_SECONDS="900")
        self.assertEqual(delayed.get_bars("META", "5m", et(DAY, 0), et(DAY, 23))[-1].timestamp, et(DAY, 10, 10))
        latest = provider(FakeSession([ok(to_results(bars))]), now=now).get_latest_bars("META", "5m", 3,
                                                                                         lookback=timedelta(days=1))
        self.assertEqual([b.timestamp for b in latest], [et(DAY, 10, 15), et(DAY, 10, 20), et(DAY, 10, 25)])

    def test_errors_propagate_and_logs_are_safe(self):
        with self.assertRaises(ProviderError) as caught:
            provider(FakeSession([FakeResponse(401)])).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
        self.assertEqual(caught.exception.kind, "auth")
        bars = calendar_bars("META", [DAY], 5)
        with self.assertLogs("market_data", "INFO") as logs:
            provider(FakeSession([FakeResponse(503), ok(to_results(bars))])).get_bars("META", "5m", et(DAY, 0), et(DAY, 23))
        self.assertNotIn(TEST_KEY, "\n".join(logs.output))
        self.assertIn("event=market_data_fetch provider=polygon symbol=META interval=5m bars=78", "\n".join(logs.output))

    def test_requires_key_and_aware_range(self):
        settings = load_market_data_settings(ENV)
        with self.assertRaises(MarketDataError):
            PolygonProvider(type(settings)(**{**settings.__dict__, "api_key": None}), calendar=CAL)
        with self.assertRaises(MarketDataError):
            provider(FakeSession([])).get_bars("META", "5m", datetime(2026, 9, 23), et(DAY, 23))
        self.assertIsInstance(build_provider(settings, calendar=CAL, session=FakeSession([])), PolygonProvider)
        self.assertIs(Interval.parse("1h"), Interval.H1)


if __name__ == "__main__":
    unittest.main()
