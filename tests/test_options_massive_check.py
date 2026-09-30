"""Phase 9A Massive Options contract check: synthetic provider responses only (no live API call)."""
from datetime import datetime, timezone
import io
import json
import unittest
from urllib.parse import urlsplit

import requests

from options_data import massive_check as check
from options_data.config import OptionsDataConfigError, load_options_data_settings

CANARY = "OPTIONS-KEY-CANARY-7f3a9c"
NOW = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)  # 11:00 ET, regular session.
NS = 10 ** 9
QUOTE_NS = int((NOW.timestamp() - 30) * NS)     # 30 s old
TRADE_NS = int((NOW.timestamp() - 90) * NS)     # 90 s old
ENV = dict(OPTIONS_DATA_PROVIDER="massive", OPTIONS_DATA_API_KEY=CANARY, OPTIONS_DATA_MIN_REQUEST_INTERVAL_SECONDS="0")
BASE = "https://api.massive.com"


def chain_item(ticker="O:META261016C00700000", *, greeks=True, iv=True, quote=True, oi=True, shares=100):
    item = dict(details=dict(ticker=ticker, contract_type="call", exercise_style="american",
                             expiration_date="2026-10-16", strike_price=700, shares_per_contract=shares),
                day=dict(volume=12, last_updated=QUOTE_NS),
                last_trade=dict(price="10.5", size=1, sip_timestamp=TRADE_NS),
                underlying_asset=dict(ticker="META", price="705.1", last_updated=QUOTE_NS))
    if quote:
        item["last_quote"] = dict(bid="10.1", ask="10.4", bid_size=5, ask_size=7, last_updated=QUOTE_NS)
    if greeks:
        item["greeks"] = dict(delta="0.55", gamma="0.01", theta="-0.3", vega="0.9")
    if iv:
        item["implied_volatility"] = "0.41"
    if oi:
        item["open_interest"] = 1500
    return item


class FakeResponse:
    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status_code = status
        self.text = text if text is not None else json.dumps(body if body is not None else {})
        self.headers = headers or {}


class FakeSession:
    """Routes by URL path; a route is a response, a list of responses (served in order), or an exception."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(dict(url=url, params=params, headers=headers, allow_redirects=allow_redirects))
        route = self.routes.get(urlsplit(url).path)
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
        return route or FakeResponse(404, {"status": "NOT_FOUND"})


def ok(results, **extra):
    return FakeResponse(200, dict(status="OK", results=results, **extra),
                        headers={"X-RateLimit-Remaining": "99", "Authorization": f"Bearer {CANARY}"})


def standard_routes(chain_pages=None):
    contract = "O:META261016C00700000"
    return {
        "/v3/reference/options/contracts": ok([dict(ticker=contract, underlying_ticker="META", contract_type="call",
                                                   expiration_date="2026-10-16", strike_price=700,
                                                   shares_per_contract=100, exercise_style="american")]),
        "/v3/snapshot/options/META": chain_pages or [ok([chain_item(), chain_item("O:META261016P00700000")])],
        f"/v3/snapshot/options/META/{contract}": ok(chain_item()),
        f"/v3/quotes/{contract}": ok([dict(bid_price="10.1", ask_price="10.4", sip_timestamp=QUOTE_NS)]),
        f"/v3/trades/{contract}": ok([dict(price="10.5", size=1, sip_timestamp=TRADE_NS)]),
        f"/v2/aggs/ticker/{contract}/range/1/day/2026-08-30/2026-09-22": ok([dict(t=1756000000000, v=3)]),
    }


class FixedCalendar:
    def __init__(self, state="regular"):
        self.state = state

    def classify(self, _):
        return type("S", (), {"value": self.state})()


def run_check(routes=None, env=None, argv=None, calendar=None):
    session, out = FakeSession(routes if routes is not None else standard_routes()), io.StringIO()
    code = check.main(argv or ["--symbol", "META"], environ=ENV if env is None else env, session=session, clock=lambda: NOW,
                      sleep=lambda _: None, out=out, calendar=calendar or FixedCalendar())
    return code, out.getvalue(), session


class ConfigTests(unittest.TestCase):
    def test_defaults_and_separation_from_market_data(self):
        s = load_options_data_settings(dict(OPTIONS_DATA_PROVIDER="massive", OPTIONS_DATA_API_KEY=CANARY))
        self.assertEqual((s.base_url, s.delay_seconds, s.min_request_interval_seconds, s.max_pages),
                         ("https://api.massive.com", None, 1.0, 2))
        self.assertNotIn(CANARY, repr(s) + str(s) + json.dumps(s.public()))
        with self.assertRaises(OptionsDataConfigError):  # MARKET_DATA_* is never used for options.
            load_options_data_settings(dict(OPTIONS_DATA_PROVIDER="massive", MARKET_DATA_API_KEY=CANARY,
                                            MARKET_DATA_PROVIDER="massive_stocks"))
        self.assertEqual(load_options_data_settings({}).provider, "none")

    def test_invalid_values(self):
        for env in (dict(OPTIONS_DATA_PROVIDER="polygon"), dict(OPTIONS_DATA_BASE_URL="http://api.massive.com"),
                    dict(OPTIONS_DATA_BASE_URL="https://user:pw@api.massive.com"),
                    dict(OPTIONS_DATA_BASE_URL="https://api.massive.com/v3"), dict(OPTIONS_DATA_MAX_PAGES="9"),
                    dict(OPTIONS_DATA_DELAY_SECONDS="-1"), dict(OPTIONS_DATA_MIN_REQUEST_INTERVAL_SECONDS="x")):
            with self.subTest(env=env), self.assertRaises(OptionsDataConfigError) as caught:
                load_options_data_settings(env)
            self.assertNotIn(CANARY, str(caught.exception))


class RecordingEnviron(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)


class EnvironmentBoundaryTests(unittest.TestCase):
    def test_only_options_data_variables_are_read(self):
        env = RecordingEnviron(ENV, MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY="stock-key",
                               MARKET_DATA_DELAY_SECONDS="900")
        code, text, _ = run_check(env=env)
        self.assertEqual(code, 0)
        self.assertTrue(env.read)
        self.assertTrue(all(key.startswith("OPTIONS_DATA_") for key in env.read), env.read)
        self.assertNotIn("stock-key", text)


class NotExecutedTests(unittest.TestCase):
    def test_configuration_and_usage_errors_make_no_request(self):
        for env, argv in ((dict(), None), (dict(OPTIONS_DATA_PROVIDER="massive"), None),
                          (ENV, ["--symbol", "meta!"]), (ENV, ["--page-limit", "500"]), (ENV, ["--max-requests", "99"])):
            with self.subTest(env=env, argv=argv):
                code, text, session = run_check(env=env, argv=argv)
                self.assertEqual((code, json.loads(text)["live_validation"], session.calls), (2, "NOT EXECUTED", []))


class EndToEndTests(unittest.TestCase):
    def test_success_report(self):
        code, text, session = run_check()
        report = json.loads(text)
        self.assertEqual((code, report["live_validation"], report["check_version"]), (0, "COMPLETED", "phase9a-check-v1"))
        statuses = {k: v["status"] for k, v in report["endpoint_results"].items()}
        self.assertEqual(statuses["chain_snapshot"], "entitled")
        self.assertEqual(statuses["contract_snapshot"], "entitled")
        self.assertEqual(report["sample_contract"], "O:META261016C00700000")
        timing = report["timing"]
        self.assertEqual((timing["newest_quote_age_seconds"], timing["newest_trade_age_seconds"],
                          timing["delay_classification"]), (30, 90, "appears_realtime"))
        self.assertEqual(report["timestamp_capabilities"]["greeks"]["basis"], "time_basis_unverified")
        self.assertEqual(report["timestamp_capabilities"]["implied_volatility"]["basis"], "time_basis_unverified")
        self.assertEqual(report["open_interest_capability"]["semantics"], "uncertain_no_date_provided")
        self.assertEqual(report["adjusted_contract_observation"]["status"], "not_observed")
        self.assertEqual(report["pagination"]["chain_snapshot"]["complete"], True)
        self.assertEqual(report["rate_limit_metadata"]["headers_seen"], {"X-RateLimit-Remaining": "99"})
        for call in session.calls:  # Read-only, header auth, never redirects.
            self.assertFalse(call["allow_redirects"])
            self.assertEqual(call["headers"]["Authorization"], f"Bearer {CANARY}")
            self.assertTrue(call["url"].startswith(BASE))
            self.assertNotIn(CANARY, call["url"] + json.dumps(call["params"] or {}))

    def test_field_presence_counts_without_values(self):
        pages = [ok([chain_item(), chain_item("O:META261016P00700000", greeks=False, iv=False, quote=False, oi=False)])]
        report = json.loads(run_check(standard_routes(pages))[1])
        fields = report["field_presence_counts"]["chain_snapshot"]["fields"]
        self.assertEqual(report["field_presence_counts"]["chain_snapshot"]["items"], 2)
        self.assertEqual(fields["greeks.delta"], dict(present=1, null=0, types=["string"]))
        self.assertEqual(fields["last_quote.bid"]["present"], 1)
        self.assertEqual(fields["details.strike_price"], dict(present=2, null=0, types=["number"]))
        self.assertEqual(fields["open_interest"]["present"], 1)
        text = json.dumps(report)
        for value in ("10.1", "10.4", "705.1", "0.55", "0.41"):
            self.assertNotIn(f'"{value}"', text)  # No prices, Greeks or IV values.

    def test_greeks_own_timestamp_detected(self):
        item = chain_item()
        item["greeks"]["last_updated"] = QUOTE_NS
        report = json.loads(run_check(standard_routes([ok([item])]))[1])
        self.assertEqual(report["timestamp_capabilities"]["greeks"]["basis"], "own_timestamp")

    def test_missing_timestamps(self):
        item = chain_item()
        for group in ("last_quote", "day", "underlying_asset"):
            item[group].pop("last_updated")
        item["last_trade"].pop("sip_timestamp")
        report = json.loads(run_check(standard_routes([ok([item])]))[1])
        self.assertEqual((report["timing"]["newest_quote_age_seconds"], report["timing"]["delay_classification"]),
                         (None, "indeterminate_no_timestamps"))

    def test_outside_session_is_indeterminate(self):
        report = json.loads(run_check(calendar=FixedCalendar("closed"))[1])
        self.assertEqual(report["timing"]["delay_classification"], "indeterminate_outside_regular_session")

    def test_adjusted_contracts_observed(self):
        pages = [ok([chain_item("O:META1261016C00700000", shares=50)])]
        obs = json.loads(run_check(standard_routes(pages))[1])["adjusted_contract_observation"]
        self.assertEqual((obs["status"], obs["nonstandard_root_examples"], obs["shares_per_contract_values"]),
                         ("observed", ["O:META1261016C00700000"], {"100": 1, "50": 1}))  # + reference list


class FailureTests(unittest.TestCase):
    def test_401_403_other_families_continue(self):
        routes = standard_routes()
        routes["/v3/quotes/O:META261016C00700000"] = FakeResponse(403, {"status": "NOT_AUTHORIZED"})
        routes["/v3/trades/O:META261016C00700000"] = FakeResponse(401, {})
        code, text, _ = run_check(routes)
        results = json.loads(text)["endpoint_results"]
        self.assertEqual(code, 0)
        self.assertEqual((results["quotes"]["status"], results["quotes"]["http_status"]), ("not_entitled", 403))
        self.assertEqual(results["trades"]["status"], "not_entitled")
        self.assertEqual(results["contract_snapshot"]["status"], "entitled")

    def test_chain_not_entitled_fails(self):
        routes = standard_routes([FakeResponse(403, {})])
        code, text, _ = run_check(routes)
        report = json.loads(text)
        self.assertEqual((code, report["live_validation"], report["endpoint_results"]["chain_snapshot"]["status"]),
                         (1, "FAILED", "not_entitled"))
        self.assertEqual(report["sample_contract"], "O:META261016C00700000")  # Falls back to the reference list.

    def test_timeout_and_malformed_json(self):
        routes = standard_routes()
        routes["/v3/reference/options/contracts"] = requests.Timeout("slow")
        routes["/v3/trades/O:META261016C00700000"] = FakeResponse(200, text="{not json")
        results = json.loads(run_check(routes)[1])["endpoint_results"]
        self.assertEqual(results["contracts_reference"]["status"], "transport")
        self.assertEqual(results["trades"]["status"], "payload")

    def test_page_budget_truncates(self):
        page = lambda: ok([chain_item()], next_url=f"{BASE}/v3/snapshot/options/META?cursor=abc")
        report = json.loads(run_check(standard_routes([page(), page(), page(), page()]))[1])
        pagination = report["pagination"]["chain_snapshot"]
        self.assertEqual(report["endpoint_results"]["chain_snapshot"]["status"], "truncated_page_budget")
        self.assertEqual((pagination["pages_fetched"], pagination["complete"], pagination["next_url_host"],
                          pagination["next_url_query_parameter_names"], pagination["next_url_embeds_api_key"]),
                         (2, False, "api.massive.com", ["cursor"], False))

    def test_foreign_or_insecure_next_url_refused(self):
        for next_url in ("https://evil.example.com/v3/snapshot/options/META?cursor=x",
                         "http://api.massive.com/v3/snapshot/options/META?cursor=x"):
            with self.subTest(next_url=next_url):
                code, text, session = run_check(standard_routes([ok([chain_item()], next_url=next_url)]))
                self.assertEqual(json.loads(text)["endpoint_results"]["chain_snapshot"]["status"],
                                 "pagination_refused_foreign_host")
                self.assertFalse(any("evil" in c["url"] or c["url"].startswith("http:") for c in session.calls))

    def test_request_budget(self):
        code, text, session = run_check(argv=["--symbol", "META", "--max-requests", "2"])
        report = json.loads(text)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(report["endpoint_results"]["contract_snapshot"]["status"], "not_attempted_budget")


class SecretRedactionTests(unittest.TestCase):
    def test_key_never_in_output_even_if_echoed_by_provider(self):
        next_url = f"{BASE}/v3/snapshot/options/META?cursor=abc&apiKey={CANARY}"
        routes = standard_routes([ok([chain_item()], next_url=next_url), ok([chain_item("O:META261016P00700000")])])
        routes["/v3/quotes/O:META261016C00700000"] = FakeResponse(403, {"message": "bad key"})
        code, text, _ = run_check(routes)
        self.assertNotIn(CANARY, text)
        self.assertNotIn("Bearer", text)
        self.assertNotIn("Authorization", text)
        self.assertTrue(json.loads(text)["pagination"]["chain_snapshot"]["next_url_embeds_api_key"])

    def test_not_executed_messages_have_no_key(self):
        text = run_check(env=dict(OPTIONS_DATA_PROVIDER="bogus", OPTIONS_DATA_API_KEY=CANARY))[1]
        self.assertNotIn(CANARY, text)


if __name__ == "__main__":
    unittest.main()
