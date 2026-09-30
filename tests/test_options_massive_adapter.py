"""Phase 9C: Massive options adapter, provider interface and snapshot runner (synthetic responses; no live API)."""
import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

from options_data import massive, provider as provider_module, runner
from options_data.canonical import canonical_json
from options_data.massive import MassiveOptionsProvider, epoch_instant, map_contract
from options_data.provider import ChainScope, FixtureOptionsProvider, OptionsDataProvider, OptionsProviderError
from options_data.validation import validated_snapshot
from tests.test_options_massive_check import FakeResponse, FakeSession, FixedCalendar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANARY = "OPTIONS-9C-KEY-CANARY-51e2"
NOW = datetime(2026, 9, 30, 14, 45, tzinfo=timezone.utc)
FRESH_NS = int((NOW.timestamp() - 27) * 10 ** 9)
OLD_NS = int(datetime(2026, 9, 21, 18, 1, 2, tzinfo=timezone.utc).timestamp() * 10 ** 9)
ENV = dict(OPTIONS_DATA_PROVIDER="massive", OPTIONS_DATA_API_KEY=CANARY, OPTIONS_DATA_MIN_REQUEST_INTERVAL_SECONDS="0")
BASE = "https://api.massive.com"
CHAIN = "/v3/snapshot/options/META"


def live_item(strike=700, kind="call", expiration="2026-10-16", root="META", *, iv=True, greeks=True, oi=True,
              updated=FRESH_NS, **extra):
    """Shaped exactly like the Phase 9A regular-session META chain items (numbers as the client parses them)."""
    ticker = f"O:{root}{expiration.replace('-', '')[2:]}{'C' if kind == 'call' else 'P'}{int(strike * 1000):08d}"
    item = dict(details=dict(contract_type=kind, exercise_style="american", expiration_date=expiration,
                             shares_per_contract=100, strike_price=Decimal(str(strike)), ticker=ticker),
                day=dict(change=Decimal("0.45"), change_percent=Decimal("4.59"), close=Decimal("10.25"),
                         high=Decimal("11.2"), last_updated=updated, low=Decimal("9.5"), open=Decimal("10"),
                         previous_close=Decimal("9.8"), volume=1200, vwap=Decimal("10.4")),
                underlying_asset=dict(ticker="META"))
    if oi:
        item["open_interest"] = 15000
    if iv:
        item["implied_volatility"] = Decimal("0.4123")
    if greeks:
        item["greeks"] = dict(delta=Decimal("0.55"), gamma=Decimal("0.012"), theta=Decimal("-0.31"),
                              vega=Decimal("0.92"))
    item.update(extra)
    return item


def _json_numbers(value):
    """Decimals as plain JSON numbers (the real client parses them back into Decimal, exactly as for Massive)."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_numbers(v) for v in value]
    return value


def page(items, next_cursor=None, host="api.massive.com", scheme="https"):
    body = dict(status="OK", results=_json_numbers(items))
    if next_cursor:
        body["next_url"] = f"{scheme}://{host}{CHAIN}?cursor={next_cursor}"
    return FakeResponse(200, body, headers={"X-RateLimit-Remaining": "99"})


def settings(**env):
    from options_data.config import load_options_data_settings
    return load_options_data_settings(dict(ENV, **env))


def fetch(pages, scope=None, max_requests=None):
    session = FakeSession({CHAIN: list(pages)})
    adapter = MassiveOptionsProvider(settings(), session=session, sleep=lambda _: None, max_requests=max_requests)
    return adapter.get_chain("META", scope or ChainScope(max_pages=5)), session


def run(argv, *, pages=None, provider=None, env=None, clock=None, calendar=None):
    out, calls = io.StringIO(), []

    def counted():
        calls.append(1)
        return NOW
    session = FakeSession({CHAIN: list(pages or [page([live_item()])])})
    code = runner.main(argv, environ=ENV if env is None else env, provider=provider, clock=clock or counted,
                       calendar=calendar or FixedCalendar(), out=out, session=session, sleep=lambda _: None)
    return code, out.getvalue(), calls, session


class ProviderInterfaceTests(unittest.TestCase):
    def test_interface_and_fixture_provider(self):
        self.assertTrue(issubclass(MassiveOptionsProvider, OptionsDataProvider))
        fixture = FixtureOptionsProvider([map_contract(live_item())])
        result = fixture.get_chain("META", ChainScope())
        self.assertEqual((result.provider, len(result.records), result.unavailable_groups, result.truncated),
                         ("fixture", 1, ("quote", "trade"), False))
        self.assertEqual(ChainScope(contract_types=("call",), page_size=50, max_pages=4).as_snapshot_scope(),
                         dict(contract_types=["call"], expiration_from=None, expiration_through=None,
                              provider_page_limit=50, provider_result_limit=200))


class MappingTests(unittest.TestCase):
    def test_live_shaped_item(self):
        record = map_contract(live_item())
        self.assertEqual({k: record[k] for k in ("provider_symbol", "option_type", "expiration", "underlying")},
                         dict(provider_symbol="O:META261016C00700000", option_type="call", expiration="2026-10-16",
                              underlying="META"))
        self.assertEqual(record["terms"], dict(exercise_style="american", shares_per_contract=100))
        self.assertEqual(record["day"]["observed_at"], epoch_instant(FRESH_NS))
        self.assertEqual(set(record["day"]) - {"observed_at"}, set(massive.DAY_FIELDS))
        self.assertEqual((record["open_interest"], record["implied_volatility"]),
                         (dict(value=15000), dict(value=Decimal("0.4123"))))
        self.assertEqual(set(record["greeks"]), {"delta", "gamma", "theta", "vega"})  # rho absent
        self.assertNotIn("quote", record)
        self.assertNotIn("trade", record)

    def test_missing_groups_and_rho(self):
        record = map_contract(live_item(iv=False, greeks=False, oi=False))
        self.assertFalse({"implied_volatility", "greeks", "open_interest"} & set(record))
        with_rho = live_item()
        with_rho["greeks"]["rho"] = Decimal("0.02")
        self.assertEqual(map_contract(with_rho)["greeks"]["rho"], Decimal("0.02"))

    def test_epoch_nanoseconds(self):
        self.assertEqual(epoch_instant(FRESH_NS), (NOW - timedelta(seconds=27)).replace(microsecond=0))
        for bad in (None, True, 1759243203, "1759243203000000000", -5):
            self.assertIsNone(epoch_instant(bad))

    def test_quote_and_trade_only_when_returned(self):
        item = live_item(last_quote=dict(bid=Decimal("10.1"), ask=Decimal("10.4"), bid_size=5, ask_size=7,
                                         last_updated=FRESH_NS),
                         last_trade=dict(price=Decimal("10.3"), size=2, sip_timestamp=FRESH_NS))
        record = map_contract(item)
        self.assertEqual((record["quote"]["bid"], record["trade"]["price"]), (Decimal("10.1"), Decimal("10.3")))
        self.assertEqual(record["quote"]["observed_at"], epoch_instant(FRESH_NS))

    def test_unmappable_items_become_malformed_records(self):
        for bad in ("junk", {}, dict(details="x")):
            self.assertEqual(map_contract(bad), dict(provider_symbol=None))


class PaginationTests(unittest.TestCase):
    def test_multiple_pages_complete(self):
        result, session = fetch([page([live_item(690)], "a"), page([live_item(700)], "b"), page([live_item(710)])])
        self.assertEqual((result.pages_fetched, result.truncated, len(result.records)), (3, False, 3))
        self.assertEqual(session.calls[0]["params"], dict(limit=250))
        self.assertIsNone(session.calls[1]["params"])  # next_url followed as-is, header auth only
        for call in session.calls:
            self.assertEqual(call["headers"]["Authorization"], f"Bearer {CANARY}")
            self.assertNotIn(CANARY, call["url"])
            self.assertFalse(call["allow_redirects"])

    def test_page_limit_truncation_is_recorded(self):
        result, _ = fetch([page([live_item(690)], "a"), page([live_item(700)], "b"), page([live_item(710)])],
                          scope=ChainScope(max_pages=2))
        self.assertEqual((result.pages_fetched, result.truncated, len(result.records)), (2, True, 2))

    def test_request_budget(self):
        result, _ = fetch([page([live_item(690)], "a"), page([live_item(700)], "b")], max_requests=1)
        self.assertEqual((result.pages_fetched, result.truncated), (1, True))
        with self.assertRaises(OptionsProviderError) as caught:
            fetch([page([live_item()])], max_requests=0)
        self.assertEqual(caught.exception.kind, "budget")

    def test_foreign_or_http_next_url_rejected(self):
        for kwargs in (dict(host="evil.example.com"), dict(scheme="http")):
            with self.subTest(**kwargs), self.assertRaises(OptionsProviderError) as caught:
                fetch([page([live_item()], "a", **kwargs), page([live_item(710)])])
            self.assertEqual(caught.exception.kind, "pagination")

    def test_http_errors(self):
        for response, kind in ((FakeResponse(401, {}), "auth"), (FakeResponse(403, {}), "auth"),
                               (requests.Timeout("slow"), "transport"), (FakeResponse(200, text="{nope"), "payload"),
                               (FakeResponse(200, {"status": "OK", "results": "x"}), "payload")):
            with self.subTest(kind=kind), self.assertRaises(OptionsProviderError) as caught:
                fetch([response])
            self.assertEqual(caught.exception.kind, kind)
            self.assertNotIn(CANARY, str(caught.exception))

    def test_unavailable_groups_follow_the_source(self):
        result, _ = fetch([page([live_item()])])
        self.assertEqual(result.unavailable_groups, ("quote", "trade"))
        result, _ = fetch([page([live_item(), live_item(710, last_quote=dict(bid=Decimal("1"),
                                                                              last_updated=FRESH_NS))])])
        self.assertEqual(result.unavailable_groups, ("trade",))
        self.assertEqual(result.underlying_price, dict(reason="not_supplied"))
        priced = live_item(underlying_asset=dict(ticker="META", price=Decimal("705.1"), last_updated=FRESH_NS))
        self.assertEqual(fetch([page([priced])])[0].underlying_price, dict(reason="not_used_in_v1"))


class ScopeTests(unittest.TestCase):
    def test_scope_sent_as_filters(self):
        scope = ChainScope(contract_types=("put",), expiration_from="2026-10-01", expiration_through="2026-10-31",
                           page_size=100)
        result, session = fetch([page([live_item(kind="put")])], scope=scope)
        self.assertEqual(session.calls[0]["params"], {"limit": 100, "contract_type": "put",
                                                      "expiration_date.gte": "2026-10-01",
                                                      "expiration_date.lte": "2026-10-31"})
        self.assertEqual(len(result.records), 1)

    def test_out_of_scope_results_fail(self):
        for scope, item in ((ChainScope(contract_types=("put",)), live_item(kind="call")),
                            (ChainScope(expiration_from="2026-11-01"), live_item(expiration="2026-10-16")),
                            (ChainScope(expiration_through="2026-10-01"), live_item(expiration="2026-10-16"))):
            with self.subTest(scope=scope), self.assertRaises(OptionsProviderError) as caught:
                fetch([page([item])], scope=scope)
            self.assertEqual(caught.exception.kind, "scope")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "META.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_snapshot_written_with_one_clock_read(self):
        pages = [page([live_item(690), live_item(700, iv=False, greeks=False), live_item(710, oi=False),
                       live_item(720, updated=OLD_NS)], "a"),
                 page([live_item(700, "put"), "junk"])]
        code, text, calls, _ = run(["--symbol", "META", "--output", self.path], pages=pages)
        report = json.loads(text)
        self.assertEqual((code, len(calls), report["result"]), (0, 1, "WRITTEN"))
        with open(self.path, encoding="utf-8") as handle:
            data = json.loads(handle.read())
        self.assertEqual(validated_snapshot(data)["snapshot_id"], report["snapshot_id"])
        self.assertEqual((report["contracts"], report["exclusions"], report["pages_fetched"], report["truncated"]),
                         (5, {"malformed_record": 1}, 2, False))
        self.assertEqual(report["group_statuses"]["quote"], {"unavailable": 5})
        self.assertEqual(report["group_statuses"]["implied_volatility"], {"missing": 1, "present": 4})
        self.assertEqual(report["group_statuses"]["open_interest"], {"missing": 1, "present": 4})
        self.assertEqual(report["underlying_price"], dict(status="unavailable", reason="not_supplied"))
        self.assertEqual((report["as_of"], report["configured_delay_seconds"]), (NOW.isoformat(), None))
        days = {c["identity"]["strike"]: c["day"] for c in data["contracts"] if c["identity"]["option_type"] == "call"}
        self.assertEqual(days["720"]["observed_at"], epoch_instant(OLD_NS).isoformat())  # its own (older) time
        for c in data["contracts"]:
            self.assertEqual((c["implied_volatility"]["time_basis"] if c["implied_volatility"]["status"] == "present"
                              else "provider_snapshot_unverified"), "provider_snapshot_unverified")
            self.assertEqual(c["trade"]["status"], "unavailable")
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), (canonical_json(data) + "\n").encode())

    def test_delay_setting(self):
        env = dict(ENV, OPTIONS_DATA_DELAY_SECONDS="900")
        code, text, _, _ = run(["--symbol", "META", "--output", self.path],
                               pages=[page([live_item(updated=FRESH_NS)])], env=env)
        report = json.loads(text)
        self.assertEqual((report["as_of"], report["configured_delay_seconds"]),
                         ((NOW - timedelta(seconds=900)).isoformat(), 900))
        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["contracts"][0]["day"]["status"], "excluded_after_as_of")  # 27 s old > 900 s cutoff
        self.assertEqual(data["exclusions"], [dict(reason="fact_after_as_of", count=1)])

    def test_scope_options_and_truncation_in_identity(self):
        pages = [page([live_item(kind="put", expiration="2026-10-16")], "a"),
                 page([live_item(710, kind="put", expiration="2026-10-16")])]
        argv = ["--symbol", "META", "--output", self.path, "--contract-type", "put", "--expiration-from",
                "2026-10-01", "--expiration-through", "2026-10-31", "--page-limit", "100", "--max-pages", "1"]
        code, text, _, session = run(argv, pages=pages)
        report = json.loads(text)
        self.assertEqual((code, report["truncated"], report["contracts"]), (0, True, 1))
        self.assertEqual(report["scope"], dict(contract_types=["put"], expiration_from="2026-10-01",
                                               expiration_through="2026-10-31", provider_page_limit=100,
                                               provider_result_limit=100))
        other = os.path.join(self.dir.name, "complete.json")
        code, text2, _, _ = run(argv[:3] + [other] + argv[4:-2] + ["--max-pages", "2"], pages=pages)
        self.assertNotEqual(json.loads(text2)["snapshot_id"], report["snapshot_id"])  # scope and truncation hashed

    def test_existing_output_and_overwrite(self):
        with open(self.path, "w") as handle:
            handle.write("keep")
        code, text, calls, session = run(["--symbol", "META", "--output", self.path])
        self.assertEqual((code, json.loads(text)["result"], calls, session.calls), (3, "NOT WRITTEN", [], []))
        with open(self.path) as handle:
            self.assertEqual(handle.read(), "keep")
        code, text, _, _ = run(["--symbol", "META", "--output", self.path, "--overwrite"])
        self.assertEqual(code, 0)
        with open(self.path) as handle:
            self.assertTrue(handle.read().startswith('{"as_of"'))

    def test_atomic_write_failure_leaves_nothing(self):
        with patch.object(runner.os, "fsync", side_effect=OSError("disk full")):
            code, text, _, _ = run(["--symbol", "META", "--output", self.path])
        self.assertEqual((code, json.loads(text)["reason"]), (3, "cannot write the output file"))
        self.assertEqual(os.listdir(self.dir.name), [])
        missing_dir = os.path.join(self.dir.name, "nope", "x.json")
        self.assertEqual(run(["--symbol", "META", "--output", missing_dir])[0], 3)

    def test_failures_and_usage(self):
        code, text, _, _ = run(["--symbol", "META", "--output", self.path], pages=[FakeResponse(403, {})])
        self.assertEqual((code, json.loads(text)["kind"]), (1, "auth"))
        self.assertFalse(os.path.exists(self.path))
        for argv, env in ((["--symbol", "meta!", "--output", self.path], ENV),
                          (["--symbol", "META", "--output", self.path, "--page-limit", "999"], ENV),
                          (["--symbol", "META", "--output", self.path, "--expiration-from", "2026-12-01",
                            "--expiration-through", "2026-10-01"], ENV),
                          (["--symbol", "META", "--output", self.path], dict(OPTIONS_DATA_PROVIDER="none")),
                          (["--symbol", "META"], ENV)):
            with self.subTest(argv=argv):
                code, text, calls, session = run(argv, env=env)
                self.assertEqual((code, calls, session.calls), (2, [], []))

    def test_fixture_provider_and_arbitrary_symbol(self):
        records = [map_contract(live_item(root="BRK.B", strike=450)) | dict(underlying="BRK.B")]
        fixture = FixtureOptionsProvider(records, truncated=True, pages_fetched=3)
        code, text, _, _ = run(["--symbol", "BRK.B", "--output", self.path], provider=fixture)
        report = json.loads(text)
        self.assertEqual((code, report["symbol"], report["contracts"], report["truncated"]), (0, "BRK.B", 1, True))
        self.assertEqual(fixture.calls[0][1].contract_types, ("call", "put"))


class SecurityAndBoundaryTests(unittest.TestCase):
    def test_no_secret_in_output_file_or_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "s.json")
            code, text, _, _ = run(["--symbol", "META", "--output", path],
                                   pages=[page([live_item()], "a"), page([live_item(710)])])
            with open(path) as handle:
                content = handle.read()
        for blob in (text, content):
            self.assertNotIn(CANARY, blob)
            self.assertNotIn("Bearer", blob)
            self.assertNotIn("cursor", blob)
            self.assertNotIn("api.massive.com", blob)

    def test_only_options_data_variables_are_read(self):
        from tests.test_options_massive_check import RecordingEnviron
        env = RecordingEnviron(ENV, MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY="stock-key",
                               MARKET_DATA_DELAY_SECONDS="900")
        with tempfile.TemporaryDirectory() as directory:
            code, text, _, _ = run(["--symbol", "META", "--output", os.path.join(directory, "s.json")], env=env)
        self.assertEqual(code, 0)
        self.assertTrue(all(key.startswith("OPTIONS_DATA_") for key in env.read), env.read)
        self.assertEqual(json.loads(text)["configured_delay_seconds"], None)  # no stock delay inherited

    def test_import_boundary(self):
        code = ("import sys, options_data.runner, options_data.massive\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'persistence', 'sqlalchemy', 'evidence', "
                "'evaluation', 'evidence_packet', 'evidence_synthesis', 'market_intelligence', "
                "'options_intelligence', 'openai', 'redis', 'telegram', 'analyzer', 'shared', 'market_context'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])
        for module in (massive, provider_module, runner):
            imported = {n.split(".")[0] for node in ast.walk(ast.parse(inspect.getsource(module)))
                        for n in ([a.name for a in node.names] if isinstance(node, ast.Import) else
                                  [node.module] if isinstance(node, ast.ImportFrom) else [])}
            self.assertLessEqual(imported - set(sys.stdlib_module_names), {"options_data", "market_data"},
                                 module.__name__)

    def test_forbidden_decision_concepts(self):
        forbidden = {"score", "confidence", "rank", "ranking", "recommend", "recommendation", "buy", "sell",
                     "signal", "prediction", "forecast", "probability", "target", "stop", "select", "selection",
                     "best", "cheap", "expensive", "liquidity", "moneyness", "itm", "otm", "atm", "dte", "sentiment"}
        for module in (massive, provider_module, runner):
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                names = [node.id] if isinstance(node, ast.Name) else [node.attr] if isinstance(node, ast.Attribute) \
                    else [node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) \
                    else [node.arg] if isinstance(node, ast.arg) else []
                for name in names:
                    self.assertFalse(set(re.split(r"[^a-z0-9]+", name.lower())) & forbidden,
                                     (module.__name__, name))


class ReplayTests(unittest.TestCase):
    def test_deterministic_and_order_independent(self):
        items = [live_item(s, k) for s in (690, 700, 710) for k in ("call", "put")]
        ids = set()
        with tempfile.TemporaryDirectory() as directory:
            for n, pages in enumerate(([page(items[:3], "a"), page(items[3:])],
                                       [page(list(reversed(items[3:])), "a"), page(list(reversed(items[:3])))],
                                       [page(items[:3], "a"), page(items[3:])])):
                code, text, _, _ = run(["--symbol", "META", "--output", os.path.join(directory, f"{n}.json")],
                                       pages=pages)
                ids.add(json.loads(text)["snapshot_id"])
            files = {open(os.path.join(directory, f"{n}.json"), "rb").read() for n in range(3)}
        self.assertEqual((len(ids), len(files)), (1, 1))

    def test_fixture_replay_matches_live_path(self):
        items = [live_item(690), live_item(700, "put")]
        with tempfile.TemporaryDirectory() as directory:
            live_code, live_text, _, _ = run(["--symbol", "META", "--output", os.path.join(directory, "a.json")],
                                             pages=[page(items)])
            fixture = FixtureOptionsProvider([map_contract(i) for i in items], provider="massive")
            fixture.get_chain = (lambda original: lambda u, s: original(u, s).__class__(
                **{**original(u, s).__dict__, "adapter_version": massive.ADAPTER_VERSION,
                   "endpoint_families": (massive.ENDPOINT_FAMILY,)}))(fixture.get_chain)
            code, text, _, _ = run(["--symbol", "META", "--output", os.path.join(directory, "b.json")],
                                   provider=fixture)
        self.assertEqual(json.loads(text)["snapshot_id"], json.loads(live_text)["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
