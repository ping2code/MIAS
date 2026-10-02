"""Phase 12E: the alert runner (build, replay, restore, run-once, deliver, verify), durable delivery receipts and
delivered-marker reconstruction. Every provider is stubbed: no Telegram, OpenAI or network traffic. Redis is an
in-memory fake, or the disposable test Redis when MIAS_PHASE2J_REDIS_URL is set (never the persistent instance)."""
import ast
from datetime import datetime, timedelta, timezone
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

import redis

from alert_engine import receipts as rc, runner as rn, state_store as ss
from alert_engine.builder import market_pattern_changed, setup_available, setup_invalidated
from alert_engine.canonical import canonical_json
from alert_engine.delivery import guard as dg
from alert_engine.delivery.base import DELIVERED, FAILED, DeliveryResult
from alert_engine.delivery.guard import RedisDeliveryGuard
from alert_engine.rendering import delivery_request
from alert_engine.state import replay
from tests import setup_evaluation_cases as sc
from tests import trade_setup_cases as ts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "123456:SECRET-token-canary-AbCdEf"
CHAT = "-100987654321"
REDIS_URL = "redis://:secret-password-canary@127.0.0.1:1/0"
ENV = {"MIAS_ALERT_REDIS_URL": REDIS_URL, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
NS = "mias:phase12"


def events():
    return dict(
        available=setup_available(sc.setup()).to_dict(),
        invalidated=setup_invalidated(sc.check("all_bearish", 86400)).to_dict(),
        invalidated_later=setup_invalidated(sc.check("higher_aligned_5m_opposed", 2 * 86400)).to_dict(),
        a_to_b=market_pattern_changed(ts.market_intelligence("all_bullish"), ts.later_mi("higher_aligned_5m_opposed")).to_dict(),
        b_to_a=market_pattern_changed(ts.later_mi("higher_aligned_5m_opposed"),
                                      ts.later_mi("all_bullish", seconds=2 * 86400)).to_dict())


class FakeRedis:
    """In-memory stand-in for every command the runner path uses (Phase 12C store, 12D guard, 12E restore)."""

    def __init__(self, fail=()):
        self.data, self.ttl, self.writes, self.fail = {}, {}, [], set(fail)

    def _check(self, op):
        if op in self.fail:
            raise redis.ConnectionError("simulated outage")

    def exists(self, key):
        self._check("read")
        return int(key in self.data)

    def get(self, key):
        self._check("read")
        return self.data.get(key)

    def set(self, key, value, nx=False, ex=None, px=None):
        self._check("lease" if ex else ("marker" if ":delivery:" in key else "write"))
        if nx and key in self.data:
            return None
        self.data[key], self.ttl[key] = value, ex
        self.writes.append(key)
        return True

    def eval(self, script, numkeys, *args):
        keys, argv = args[:numkeys], args[numkeys:]
        if script == dg.RELEASE:
            self._check("release")
            if self.data.get(keys[0]) == argv[0]:
                del self.data[keys[0]]
                return 1
            return 0
        self._check("write")
        if script == ss.COMMIT:
            if keys[0] in self.data:
                return 0
            if self.data.get(keys[1], "") != argv[0]:
                return -1
            self.data[keys[0]], self.data[keys[1]] = argv[2], argv[1]
            self.writes.extend(keys)
            return 1
        if script in (ss.RESTORE_SUBJECT, rn.RESTORE_DELIVERED):
            current = self.data.get(keys[0])
            if current is None:
                self.data[keys[0]] = argv[0]
                self.writes.append(keys[0])
                return 1
            allowed = argv[:1] if script == ss.RESTORE_SUBJECT else argv[1:]
            return 0 if current in allowed else -1
        raise AssertionError("unexpected script")


class Adapter:
    channel = "telegram"

    def __init__(self, *results):
        self.results, self.calls, self.requests = list(results), 0, []

    def send(self, request):
        self.calls += 1
        self.requests.append(request)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


OK_RESULT = DeliveryResult(DELIVERED, "777", 1, None)
FAIL_RESULT = DeliveryResult(FAILED, None, 3, "timeout")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 2, 13, 30, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.receipts = os.path.join(self.dir, "receipts")
        os.mkdir(self.receipts)
        self.fake, self.adapter, self.clock = FakeRedis(), Adapter(OK_RESULT), Clock()
        self.made_adapters = []
        self.ev = events()

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, value):
        with open(self.path(name), "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return self.path(name)

    def alert(self, key):
        return self.write(f"{key}.json", self.ev[key])

    def factory(self, channel, token, chat):
        self.made_adapters.append((channel, token, chat))
        return self.adapter

    def run_cli(self, *argv, environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = rn.main(list(argv), out=out, err=err, environ=ENV if environ is None else environ, clock=self.clock,
                       redis_factory=lambda url: self.fake, adapter_factory=self.factory)
        text = out.getvalue() or err.getvalue()
        for secret in (TOKEN, CHAT, "secret-password-canary"):
            self.assertNotIn(secret, out.getvalue() + err.getvalue())
        return code, json.loads(text.strip().splitlines()[-1]) if text.strip().startswith("{") else text

    def receipt_files(self):
        return sorted(os.listdir(self.receipts))

    def read_receipt(self, name):
        with open(os.path.join(self.receipts, name), encoding="utf-8") as handle:
            return json.load(handle)


class BuildTests(RunnerCase):
    def test_each_rule_builds_and_verifies(self):
        cases = [("available", ["--assessment", self.write("a.json", sc.setup())]),
                 ("invalidated", ["--invalidation-check", self.write("c.json", sc.check())]),
                 ("a_to_b", ["--previous-market-intelligence", self.write("p.json", ts.market_intelligence("all_bullish")),
                             "--current-market-intelligence", self.write("q.json", ts.later_mi("higher_aligned_5m_opposed"))])]
        for key, sources in cases:
            with self.subTest(key=key):
                output = self.path(f"out-{key}.json")
                code, summary = self.run_cli("build", *sources, "--output", output)
                self.assertEqual((code, summary["result"], summary["alert_id"]), (0, "WRITTEN", self.ev[key]["alert_id"]))
                with open(output, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), canonical_json(self.ev[key]) + "\n")
                code, summary = self.run_cli("verify-alert", "--alert", output, *sources)
                self.assertEqual((code, summary["result"]), (0, "VERIFIED"))

    def test_deterministic_bytes(self):
        source = self.write("a.json", sc.setup())
        texts = set()
        for i in range(3):
            self.run_cli("build", "--assessment", source, "--output", self.path(f"o{i}.json"))
            with open(self.path(f"o{i}.json"), "rb") as handle:
                texts.add(handle.read())
        self.assertEqual(len(texts), 1)

    def test_no_alert_is_explicit_and_writes_nothing(self):
        no_setup, _ = ts.screened([ts.record()], max_dte=10)
        unchanged = (ts.market_intelligence("all_bullish"), ts.later_mi("positive_market_return"))  # same pattern
        for sources in (["--assessment", self.write("n.json", no_setup.to_dict())],
                        ["--invalidation-check", self.write("h.json", sc.check("all_bullish"))],
                        ["--previous-market-intelligence", self.write("p.json", unchanged[0]),
                         "--current-market-intelligence", self.write("q.json", unchanged[1])]):
            with self.subTest(sources=sources[0]):
                code, summary = self.run_cli("build", *sources, "--output", self.path("none.json"))
                self.assertEqual((code, summary), (0, dict(command="build", exit_code=0, result="NO_ALERT")))
                self.assertFalse(os.path.exists(self.path("none.json")))

    def test_overwrite_and_fail_closed(self):
        source, output = self.write("a.json", sc.setup()), self.path("out.json")
        with open(output, "w") as handle:
            handle.write("keep")
        self.assertEqual(self.run_cli("build", "--assessment", source, "--output", output)[0], 5)
        with open(output) as handle:
            self.assertEqual(handle.read(), "keep")
        self.assertEqual(self.run_cli("build", "--assessment", source, "--output", output, "--overwrite")[0], 0)
        bad = sc.setup()
        bad["candidates"] = []
        cases = [["--assessment", self.write("bad.json", bad)],
                 ["--assessment", source, "--invalidation-check", self.write("c.json", sc.check())],
                 ["--previous-market-intelligence", self.write("p.json", ts.market_intelligence("all_bullish"))],
                 ["--assessment", self.path("missing.json")], []]
        for sources in cases:
            with self.subTest(sources=sources):
                self.assertEqual(self.run_cli("build", *sources, "--output", self.path("x.json"))[0], 2)
                self.assertFalse(os.path.exists(self.path("x.json")))
        with open(self.path("dup.json"), "w") as handle:
            handle.write('{"a": 1, "a": 2}')
        self.assertEqual(self.run_cli("build", "--assessment", self.path("dup.json"), "--output", self.path("x.json"))[0], 2)
        self.assertEqual(self.fake.writes, [])
        self.assertEqual(self.made_adapters, [])

    def test_verify_alert_rejects_tampering(self):
        tampered = dict(self.ev["available"], as_of="2026-09-30T14:46:00+00:00")
        code, _ = self.run_cli("verify-alert", "--alert", self.write("t.json", tampered),
                               "--assessment", self.write("a.json", sc.setup()))
        self.assertEqual(code, 2)
        code, _ = self.run_cli("verify-alert", "--alert", self.alert("available"),
                               "--invalidation-check", self.write("c.json", sc.check()))
        self.assertEqual(code, 2)


class ReplayTests(RunnerCase):
    def test_caller_order_is_authoritative(self):
        a, b = self.alert("invalidated"), self.alert("invalidated_later")
        self.run_cli("replay", "--alert", a, "--alert", b, "--output", self.path("ab.json"))
        self.run_cli("replay", "--alert", b, "--alert", a, "--output", self.path("ba.json"))
        ab, ba = (json.load(open(self.path(n))) for n in ("ab.json", "ba.json"))
        self.assertEqual(ab["alert_ids"], [self.ev["invalidated"]["alert_id"], self.ev["invalidated_later"]["alert_id"]])
        self.assertEqual(ab["state"]["subjects"][0]["terminal_alert_id"], self.ev["invalidated"]["alert_id"])
        self.assertEqual(ba["state"]["subjects"][0]["terminal_alert_id"], self.ev["invalidated_later"]["alert_id"])
        self.assertEqual([d["classification"] for d in ab["decisions"]], ["new", "terminal_suppressed"])

    def test_matches_pure_replay_and_manifest(self):
        keys = ["available", "a_to_b", "available", "invalidated", "b_to_a", "invalidated_later"]
        paths = [self.alert(k) for k in keys]
        code, summary = self.run_cli("replay", *sum((["--alert", p] for p in paths), []), "--output", self.path("r.json"))
        self.assertEqual(summary["classifications"], {"duplicate": 1, "new": 4, "terminal_suppressed": 1})
        expected = replay([self.ev[k] for k in keys])
        document = json.load(open(self.path("r.json")))
        self.assertEqual(document["state"], expected.state.to_dict())
        manifest = self.write("m.json", dict(manifest_format_version=rn.MANIFEST_FORMAT_VERSION,
                                             alerts=[os.path.basename(p) for p in paths]))
        self.run_cli("replay", "--manifest", manifest, "--output", self.path("rm.json"))
        with open(self.path("r.json"), "rb") as x, open(self.path("rm.json"), "rb") as y:
            self.assertEqual(x.read(), y.read())
        self.assertEqual(self.run_cli("verify-replay", "--replay", self.path("r.json"), "--manifest", manifest)[0], 0)
        reordered = self.write("m2.json", dict(manifest_format_version=rn.MANIFEST_FORMAT_VERSION,
                                               alerts=[os.path.basename(p) for p in reversed(paths)]))
        self.assertEqual(self.run_cli("verify-replay", "--replay", self.path("r.json"), "--manifest", reordered)[0], 2)
        self.assertEqual((self.fake.writes, self.made_adapters), ([], []))

    def test_manifest_and_inputs_fail_closed(self):
        good = os.path.basename(self.alert("available"))
        for manifest in (dict(alerts=[good]), dict(manifest_format_version="x", alerts=[good]),
                         dict(manifest_format_version=rn.MANIFEST_FORMAT_VERSION, alerts=good),
                         dict(manifest_format_version=rn.MANIFEST_FORMAT_VERSION, alerts=["missing.json"]),
                         dict(manifest_format_version=rn.MANIFEST_FORMAT_VERSION, alerts=[good], extra=1)):
            with self.subTest(manifest=manifest):
                self.assertEqual(self.run_cli("replay", "--manifest", self.write("m.json", manifest))[0], 2)
        tampered = dict(self.ev["available"], alert_code="setup_invalidated")
        self.assertEqual(self.run_cli("replay", "--alert", self.write("t.json", tampered))[0], 2)
        self.assertEqual(self.run_cli("replay", "--alert", self.alert("available"), "--overwrite")[0], 2)

    def test_replay_is_pure_at_runtime(self):
        path = self.alert("available")
        code = ("import sys\nfrom alert_engine import runner\n"
                f"rc = runner.main(['replay', '--alert', {path!r}], environ={{}})\n"
                "loaded = sorted({n.split('.')[0] for n in sys.modules} & {'redis', 'requests', 'dotenv', 'sqlalchemy',"
                " 'openai', 'urllib3', 'collector', 'orchestrator', 'shared'})\n"
                "print(rc, loaded, 'alert_engine.delivery.telegram' in sys.modules)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                                env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.stdout.strip().splitlines()[-1], "0 [] False", result.stderr[-300:])


class RestoreStateTests(RunnerCase):
    def test_restore_requires_confirm_and_matches_replay(self):
        keys = ["available", "a_to_b", "invalidated", "invalidated_later"]
        args = sum((["--alert", self.alert(k)] for k in keys), [])
        self.assertEqual(self.run_cli("restore-state", *args)[0], 2)
        self.assertEqual(self.fake.writes, [])
        code, summary = self.run_cli("restore-state", *args, "--confirm")
        self.assertEqual((code, summary["written_seen"], summary["written_subjects"]), (0, 3, 2))
        reference = FakeRedis()
        store = ss.RedisAlertStateStore(reference)
        for k in keys:
            store.record(self.ev[k])
        self.assertEqual(self.fake.data, reference.data)        # exactly the keys and values live recording makes
        code, summary = self.run_cli("restore-state", *args, "--confirm")
        self.assertEqual((code, summary["written_seen"], summary["written_subjects"]), (0, 0, 0))

    def test_conflict_and_outage_fail_closed(self):
        self.run_cli("restore-state", "--alert", self.alert("a_to_b"), "--confirm")
        before = dict(self.fake.data)
        code, _ = self.run_cli("restore-state", "--alert", self.alert("b_to_a"), "--confirm")
        self.assertEqual(code, 3)
        self.assertEqual(self.fake.data, before)
        self.fake.fail.add("read")
        self.assertEqual(self.run_cli("restore-state", "--alert", self.alert("available"), "--confirm")[0], 3)
        self.assertEqual(self.run_cli("restore-state", "--alert", self.alert("available"), "--confirm",
                                      environ={})[0], 2)


class LiveOnceTests(RunnerCase):
    def test_new_alert_is_sent_once_with_a_receipt(self):
        path = self.alert("available")
        code, summary = self.run_cli("run-once", "--alert", path, "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["classification"], summary["delivery"], summary["status"]), (0, "new", "sent", "delivered"))
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.adapter.requests[0], delivery_request(self.ev["available"], "telegram"))
        self.assertEqual(self.made_adapters, [("telegram", TOKEN, CHAT)])
        hex_id = self.ev["available"]["alert_id"].split(":")[1]
        self.assertEqual(self.fake.data[f"{NS}:delivery:telegram:{hex_id}:delivered"], "777")
        self.assertIn(f"{NS}:seen:{hex_id}", self.fake.data)
        [name] = self.receipt_files()
        receipt = self.read_receipt(name)
        self.assertEqual(name, f"telegram-{hex_id}-000001.json")
        self.assertEqual((receipt["status"], receipt["provider_message_id"], receipt["sequence"], receipt["attempts"]),
                         ("delivered", "777", 1, 1))
        code, summary = self.run_cli("run-once", "--alert", path, "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["classification"], self.adapter.calls), (0, "duplicate", 1))
        self.assertNotIn("delivery", summary)
        self.assertEqual(len(self.receipt_files()), 1)

    def test_terminal_suppressed_is_not_rendered_or_sent(self):
        self.run_cli("run-once", "--alert", self.alert("invalidated"), "--receipt-dir", self.receipts)
        with mock.patch("alert_engine.rendering.delivery_request") as render:
            code, summary = self.run_cli("run-once", "--alert", self.alert("invalidated_later"), "--receipt-dir", self.receipts)
            render.assert_not_called()
        self.assertEqual((code, summary["classification"], self.adapter.calls), (0, "terminal_suppressed", 1))
        code, summary = self.run_cli("run-once", "--alert", self.alert("available"), "--receipt-dir", self.receipts)
        self.assertEqual((summary["classification"], self.adapter.calls), ("terminal_suppressed", 1))

    def test_failed_delivery_then_delivery_only_retry(self):
        self.adapter = Adapter(FAIL_RESULT, OK_RESULT)
        path = self.alert("a_to_b")
        code, summary = self.run_cli("run-once", "--alert", path, "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["status"], summary["safe_error_code"]), (4, "failed", "timeout"))
        hex_id = self.ev["a_to_b"]["alert_id"].split(":")[1]
        self.assertNotIn(f"{NS}:delivery:telegram:{hex_id}:delivered", self.fake.data)
        self.assertNotIn(f"{NS}:delivery:telegram:{hex_id}:lease", self.fake.data)
        # Accepted is not delivered: a second run-once is a duplicate and sends nothing ...
        code, summary = self.run_cli("run-once", "--alert", path, "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["classification"], self.adapter.calls), (0, "duplicate", 1))
        # ... so the delivery-only path retries without needing the alert to be new.
        code, summary = self.run_cli("deliver", "--alert", path, "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["classification"], summary["status"], self.adapter.calls), (0, "accepted", "delivered", 2))
        self.assertEqual([self.read_receipt(n)["status"] for n in self.receipt_files()], ["failed", "delivered"])
        failed = self.read_receipt(self.receipt_files()[0])
        self.assertEqual((failed["provider_message_id"], failed["safe_error_code"], failed["attempts"]), (None, "timeout", 3))
        code, summary = self.run_cli("deliver", "--alert", path, "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["delivery"], self.adapter.calls), (0, "already_delivered", 2))
        self.assertEqual(len(self.receipt_files()), 2)

    def test_deliver_requires_acceptance(self):
        code, summary = self.run_cli("deliver", "--alert", self.alert("available"), "--receipt-dir", self.receipts)
        self.assertEqual((code, self.adapter.calls, self.fake.writes, self.receipt_files()), (2, 0, [], []))

    def test_state_unavailable_sends_nothing(self):
        for fail in ("read", "write", "lease"):
            with self.subTest(fail=fail):
                self.fake, self.adapter = FakeRedis(fail={fail}), Adapter(OK_RESULT)
                code, _ = self.run_cli("run-once", "--alert", self.alert("available"), "--receipt-dir", self.receipts)
                self.assertEqual((code, self.adapter.calls, self.receipt_files()), (3, 0, []))

    def test_preconditions_checked_before_state(self):
        path = self.alert("available")
        for environ in ({"MIAS_ALERT_REDIS_URL": REDIS_URL}, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}):
            self.assertEqual(self.run_cli("run-once", "--alert", path, "--receipt-dir", self.receipts, environ=environ)[0], 2)
        self.assertEqual(self.run_cli("run-once", "--alert", path, "--receipt-dir", self.path("missing"))[0], 5)
        with open(os.path.join(self.receipts, "stray.txt"), "w") as handle:
            handle.write("x")
        self.assertEqual(self.run_cli("run-once", "--alert", path, "--receipt-dir", self.receipts)[0], 5)
        self.assertEqual(self.run_cli("run-once", "--alert", self.write("bad.json", {"alert_id": "x"}),
                                      "--receipt-dir", self.receipts)[0], 2)
        self.assertEqual((self.fake.writes, self.adapter.calls), ([], 0))

    def test_in_progress_is_not_sent(self):
        request = delivery_request(self.ev["available"], "telegram")
        self.fake.data[RedisDeliveryGuard(self.fake).key(request, "lease")] = "other-sender"
        code, summary = self.run_cli("run-once", "--alert", self.alert("available"), "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["delivery"], self.adapter.calls), (3, "in_progress", 0))

    def test_marker_failure_keeps_the_receipt(self):
        self.fake.fail.add("marker")
        code, summary = self.run_cli("run-once", "--alert", self.alert("available"), "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["status"], summary["receipt_sequence"]), (3, "delivered", 1))
        self.assertIn("recorded in receipt 1", summary["error"])
        [name] = self.receipt_files()
        self.assertEqual(self.read_receipt(name)["status"], "delivered")
        # The receipt lets the operator rebuild the missing marker without sending again.
        self.fake.fail.clear()
        lease = RedisDeliveryGuard(self.fake).key(delivery_request(self.ev["available"], "telegram"), "lease")
        del self.fake.data[lease]                                # as if its TTL elapsed
        self.assertEqual(self.run_cli("restore-delivery", "--receipt-dir", self.receipts, "--confirm")[1]["written_markers"], 1)
        code, summary = self.run_cli("deliver", "--alert", self.alert("available"), "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["delivery"], self.adapter.calls), (0, "already_delivered", 1))

    def test_receipt_failure_does_not_hide_the_send(self):
        with mock.patch.object(rc, "append_receipt", side_effect=rc.ReceiptError("cannot write the delivery receipt")):
            code, summary = self.run_cli("run-once", "--alert", self.alert("available"), "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["status"], self.adapter.calls), (5, "delivered", 1))
        hex_id = self.ev["available"]["alert_id"].split(":")[1]
        self.assertEqual(self.fake.data[f"{NS}:delivery:telegram:{hex_id}:delivered"], "777")   # no re-send later

    def test_clock_failure_after_send_still_records_the_marker(self):
        calls = []

        def clock():
            calls.append(1)
            return datetime(2026, 10, 2, 13, 30, tzinfo=timezone.utc) if len(calls) == 1 else "not-a-time"
        self.clock = clock
        code, summary = self.run_cli("run-once", "--alert", self.alert("available"), "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["status"], self.receipt_files()), (5, "delivered", []))
        hex_id = self.ev["available"]["alert_id"].split(":")[1]
        self.assertEqual(self.fake.data[f"{NS}:delivery:telegram:{hex_id}:delivered"], "777")

    def test_oversized_text_is_refused_without_send(self):
        def refuse(request):
            raise ValueError("rendered text exceeds the Telegram message limit")
        self.adapter.send = refuse
        code, _ = self.run_cli("run-once", "--alert", self.alert("available"), "--receipt-dir", self.receipts)
        self.assertEqual((code, self.receipt_files()), (2, []))


class ReceiptTests(RunnerCase):
    def request(self, key="available"):
        return delivery_request(self.ev[key], "telegram")

    def append(self, result=OK_RESULT, key="available"):
        return rc.append_receipt(self.receipts, self.request(key), result, "2026-10-02T13:30:00.000000+00:00",
                                 "2026-10-02T13:30:01.000000+00:00")

    def test_closed_secret_free_fields(self):
        _, receipt = self.append()
        self.assertEqual(tuple(sorted(receipt)), tuple(sorted(rc.RECEIPT_FIELDS)))
        self.assertEqual((receipt["delivery_contract_version"], receipt["render_version"]),
                         ("phase12-delivery-v1", "phase12-render-v1"))
        text = open(os.path.join(self.receipts, self.receipt_files()[0])).read()
        for banned in (TOKEN, CHAT, "http", "MIAS phase12 alert", "chat", "token", "url", "header", "text"):
            self.assertNotIn(banned, text)
        _, failed = self.append(FAIL_RESULT)
        self.assertEqual((failed["sequence"], failed["provider_message_id"], failed["safe_error_code"]), (2, None, "timeout"))

    def test_append_only_and_race(self):
        self.append()
        first = open(os.path.join(self.receipts, self.receipt_files()[0]), "rb").read()
        hex_id = self.ev["available"]["alert_id"].split(":")[1]
        # A stale listing (a concurrent writer already took sequence 1): the no-clobber link refuses, the next wins.
        with mock.patch.object(rc, "_sequences", side_effect=[[], [1]]):
            _, receipt = self.append(FAIL_RESULT)
        self.assertEqual(receipt["sequence"], 2)
        self.assertEqual(open(os.path.join(self.receipts, f"telegram-{hex_id}-000001.json"), "rb").read(), first)
        self.assertEqual([n for n in os.listdir(self.receipts) if n.endswith(".tmp")], [])

    def test_history_is_order_independent(self):
        self.append(FAIL_RESULT)
        self.append()
        self.append(key="a_to_b")
        forward = rc.read_receipts(self.receipts)
        real = os.listdir
        with mock.patch.object(rc.os, "listdir", side_effect=lambda d: sorted(real(d), reverse=True)):
            backward = rc.read_receipts(self.receipts)
        self.assertEqual(forward, backward)
        self.assertEqual(rc.delivered_markers(forward)[(self.ev["available"]["alert_id"], "telegram")], ("777",))

    def test_malformed_history_fails_closed(self):
        _, receipt = self.append()
        name = self.receipt_files()[0]
        hex_id = self.ev["available"]["alert_id"].split(":")[1]
        bad_cases = {
            "gap": (f"telegram-{hex_id}-000003.json", dict(receipt, sequence=3)),
            "mismatch": (f"telegram-{hex_id}-000002.json", dict(receipt, sequence=5)),
            "secret field": (f"telegram-{hex_id}-000002.json", dict(receipt, sequence=2, chat_id=CHAT)),
            "delivered with error": (f"telegram-{hex_id}-000002.json", dict(receipt, sequence=2, safe_error_code="timeout")),
            "unknown code": (f"telegram-{hex_id}-000002.json", dict(receipt, sequence=2, status="failed",
                                                                     provider_message_id=None, safe_error_code="boom")),
            "naive time": (f"telegram-{hex_id}-000002.json", dict(receipt, sequence=2, attempted_at="2026-10-02T13:30:00")),
            "time order": (f"telegram-{hex_id}-000002.json", dict(receipt, sequence=2,
                                                                   attempted_at="2026-10-02T14:30:00.000000+00:00")),
            "unknown file": ("notes.txt", "x"),
        }
        for label, (bad_name, content) in bad_cases.items():
            with self.subTest(label=label):
                bad = os.path.join(self.receipts, bad_name)
                with open(bad, "w") as handle:
                    handle.write(content if isinstance(content, str) else json.dumps(content))
                with self.assertRaises(rc.ReceiptError):
                    rc.read_receipts(self.receipts)
                self.assertEqual(self.run_cli("restore-delivery", "--receipt-dir", self.receipts, "--confirm")[0], 5)
                os.unlink(bad)
        with open(os.path.join(self.receipts, f"telegram-{hex_id}-000002.json"), "w") as handle:
            handle.write('{"a": 1, "a": 1}')
        with self.assertRaises(rc.ReceiptError):
            rc.read_receipts(self.receipts)
        os.unlink(os.path.join(self.receipts, f"telegram-{hex_id}-000002.json"))
        with open(os.path.join(self.receipts, ".receipt-abc.tmp"), "w") as handle:
            handle.write("partial")
        self.assertEqual(len(rc.read_receipts(self.receipts)), 1)          # an interrupted write's leftover is ignored
        self.assertEqual(self.fake.writes, [])
        self.assertTrue(name)

    def test_runtime_timestamps_never_reach_alerts_or_state(self):
        path = self.alert("available")
        self.run_cli("run-once", "--alert", path, "--receipt-dir", self.receipts)
        for key, value in self.fake.data.items():
            self.assertNotIn("2026-10-02", value, key)
        self.assertEqual(json.load(open(path)), self.ev["available"])


class ReconstructionTests(RunnerCase):
    def deliver_all(self):
        self.adapter = Adapter(FAIL_RESULT, OK_RESULT)
        self.run_cli("run-once", "--alert", self.alert("available"), "--receipt-dir", self.receipts)   # failed
        self.adapter = Adapter(OK_RESULT)
        self.run_cli("run-once", "--alert", self.alert("a_to_b"), "--receipt-dir", self.receipts)      # delivered

    def test_rebuilds_only_delivered_markers(self):
        self.deliver_all()
        delivered = rn.delivered_key(NS, self.ev["a_to_b"]["alert_id"], "telegram")
        guard_key = RedisDeliveryGuard(self.fake).key(delivery_request(self.ev["a_to_b"], "telegram"), "delivered")
        self.assertEqual(delivered, guard_key)
        state_keys = {k: v for k, v in self.fake.data.items() if ":delivery:" not in k}
        self.fake = FakeRedis()                                  # the Redis cache was lost
        self.assertEqual(self.run_cli("restore-delivery", "--receipt-dir", self.receipts)[0], 2)
        code, summary = self.run_cli("restore-delivery", "--receipt-dir", self.receipts, "--confirm")
        self.assertEqual((code, summary["delivered"], summary["written_markers"]), (0, 1, 1))
        self.assertEqual(self.fake.data, {delivered: "777"})      # never Phase 12C state, never a failed alert
        self.assertEqual(self.fake.writes, [delivered])
        self.assertTrue(state_keys)
        code, summary = self.run_cli("restore-delivery", "--receipt-dir", self.receipts, "--confirm")
        self.assertEqual((code, summary["written_markers"]), (0, 0))
        self.assertEqual(self.adapter.calls, 1)

    def test_conflict_and_outage_fail_closed(self):
        self.deliver_all()
        self.fake = FakeRedis()
        other = rn.delivered_key(NS, self.ev["a_to_b"]["alert_id"], "telegram")
        self.fake.data[other] = "999"
        code, _ = self.run_cli("restore-delivery", "--receipt-dir", self.receipts, "--confirm")
        self.assertEqual((code, self.fake.data, self.fake.writes), (3, {other: "999"}, []))
        self.fake = FakeRedis(fail={"read"})
        self.assertEqual(self.run_cli("restore-delivery", "--receipt-dir", self.receipts, "--confirm")[0], 3)

    def test_duplicate_send_history_is_consistent(self):
        request = delivery_request(self.ev["available"], "telegram")
        for message_id in ("777", "778"):                       # an at-least-once duplicate send
            rc.append_receipt(self.receipts, request, DeliveryResult(DELIVERED, message_id, 1, None),
                              "2026-10-02T13:30:00.000000+00:00", "2026-10-02T13:30:01.000000+00:00")
        key = rn.delivered_key(NS, self.ev["available"]["alert_id"], "telegram")
        self.fake.data[key] = "777"
        self.assertEqual(self.run_cli("restore-delivery", "--receipt-dir", self.receipts, "--confirm")[0], 0)
        self.assertEqual(self.fake.data[key], "777")
        code, summary = self.run_cli("verify-receipts", "--receipt-dir", self.receipts, "--alert", self.alert("available"))
        self.assertEqual((code, summary["duplicate_sends"], summary["delivered"]), (0, 1, 1))


class VerifyReceiptsTests(RunnerCase):
    def test_relationship_to_alerts(self):
        self.adapter = Adapter(FAIL_RESULT)
        self.run_cli("run-once", "--alert", self.alert("available"), "--receipt-dir", self.receipts)
        writes = list(self.fake.writes)
        code, summary = self.run_cli("verify-receipts", "--receipt-dir", self.receipts, "--alert", self.alert("available"))
        self.assertEqual((code, summary["receipts"], summary["delivered"], summary["undelivered"]), (0, 1, 0, 1))
        code, _ = self.run_cli("verify-receipts", "--receipt-dir", self.receipts, "--alert", self.alert("a_to_b"))
        self.assertEqual(code, 2)
        self.assertEqual((self.fake.writes, self.adapter.calls), (writes, 1))


class BoundaryTests(unittest.TestCase):
    def imports(self, module):
        top = set()
        for node in ast.parse(inspect.getsource(module)).body:
            if isinstance(node, ast.Import):
                top |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                top.add(node.module)
        return top

    def test_module_level_imports(self):
        self.assertEqual(self.imports(rn), {"argparse", "datetime", "json", "os", "sys", "tempfile", "alert_engine",
                                            "alert_engine.canonical", "alert_engine.state", "alert_engine.validation"})
        self.assertEqual(self.imports(rc), {"datetime", "json", "os", "re", "tempfile", "alert_engine.canonical",
                                            "alert_engine.delivery.base", "alert_engine.rules"})

    def test_no_dotenv_and_no_unscoped_scans(self):
        for module in (rn, rc):
            source = inspect.getsource(module)
            for name in ("dotenv", "flushall", "flushdb", "scan_iter", ".scan(", "client.keys", ".keys(", "openai"):
                self.assertFalse(name in source.lower(), (module.__name__, name))
        source = inspect.getsource(rn)
        self.assertNotIn("orchestrator", self.imports(rn))
        self.assertNotIn("sqlalchemy", source)

    def test_import_is_lightweight(self):
        code = ("import sys, alert_engine.runner, alert_engine.receipts\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'redis', 'requests', 'dotenv', 'sqlalchemy',"
                " 'openai', 'collector', 'orchestrator', 'shared', 'persistence'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])

    def test_help_has_no_side_effects(self):
        result = subprocess.run([sys.executable, "-m", "alert_engine.runner", "--help"], cwd=ROOT, capture_output=True,
                                text=True, timeout=120, env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0)
        for command in rn.COMMANDS:
            self.assertIn(command, result.stdout)

    def test_default_redis_url_is_never_echoed(self):
        out, err = io.StringIO(), io.StringIO()
        code = rn.main(["restore-state", "--alert", "x.json", "--confirm", "--redis-url-env", "X"], out=out, err=err,
                       environ={"X": "not-a-url-secret-canary"})
        self.assertEqual(code, 2)
        self.assertNotIn("secret-canary", out.getvalue() + err.getvalue())


@unittest.skipUnless(os.environ.get("MIAS_PHASE2J_REDIS_URL"), "disposable test Redis not configured")
class DisposableRedisTests(RunnerCase):
    """Against the disposable phase2j Redis only, in a unique namespace; every key it touches is deleted by name."""

    def setUp(self):
        super().setUp()
        self.real = redis.Redis.from_url(os.environ["MIAS_PHASE2J_REDIS_URL"], decode_responses=True, socket_timeout=5)
        self.ns = f"mias:test:phase12e:{uuid.uuid4().hex}"

    def tearDown(self):
        keys = []
        for event in self.ev.values():
            hex_id = event["alert_id"].split(":")[1]
            keys += [f"{self.ns}:seen:{hex_id}", f"{self.ns}:delivery:telegram:{hex_id}:lease",
                     f"{self.ns}:delivery:telegram:{hex_id}:delivered"]
            keys.append(f"{self.ns}:subject:{rn.subject_key(event)}")
        self.real.delete(*keys)
        super().tearDown()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = rn.main([*argv, "--namespace", self.ns, "--redis-url-env", "MIAS_PHASE2J_REDIS_URL"], out=out, err=err,
                       environ={"MIAS_PHASE2J_REDIS_URL": os.environ["MIAS_PHASE2J_REDIS_URL"], "TELEGRAM_BOT_TOKEN": TOKEN,
                                "TELEGRAM_CHAT_ID": CHAT}, clock=self.clock, adapter_factory=self.factory)
        return code, json.loads((out.getvalue() or err.getvalue()).strip())

    def test_end_to_end_and_recovery(self):
        self.adapter = Adapter(FAIL_RESULT, OK_RESULT)
        path = self.alert("a_to_b")
        self.assertEqual(self.cli("run-once", "--alert", path, "--receipt-dir", self.receipts)[0], 4)
        self.assertEqual(self.cli("run-once", "--alert", path, "--receipt-dir", self.receipts)[1]["classification"], "duplicate")
        self.assertEqual(self.cli("deliver", "--alert", path, "--receipt-dir", self.receipts)[1]["status"], "delivered")
        hex_id = self.ev["a_to_b"]["alert_id"].split(":")[1]
        delivered = f"{self.ns}:delivery:telegram:{hex_id}:delivered"
        self.assertEqual((self.real.get(delivered), self.real.ttl(delivered)), ("777", -1))
        # Lose the cache, then recover: 12C state from sealed alerts, delivered markers from receipts.
        self.real.delete(delivered, f"{self.ns}:seen:{hex_id}", f"{self.ns}:subject:symbol:META")
        self.assertEqual(self.cli("restore-state", "--alert", path, "--confirm")[1]["written_seen"], 1)
        self.assertEqual(self.cli("restore-delivery", "--receipt-dir", self.receipts, "--confirm")[1]["written_markers"], 1)
        code, summary = self.cli("deliver", "--alert", path, "--receipt-dir", self.receipts)
        self.assertEqual((code, summary["delivery"], self.adapter.calls), (0, "already_delivered", 2))
        self.assertEqual(self.cli("run-once", "--alert", path, "--receipt-dir", self.receipts)[1]["classification"], "duplicate")

    def test_unreachable_redis_fails_closed(self):
        out, err = io.StringIO(), io.StringIO()
        code = rn.main(["run-once", "--alert", self.alert("available"), "--receipt-dir", self.receipts],
                       out=out, err=err, environ={"MIAS_ALERT_REDIS_URL": "redis://127.0.0.1:1/0", "TELEGRAM_BOT_TOKEN": TOKEN,
                                                  "TELEGRAM_CHAT_ID": CHAT}, clock=self.clock, adapter_factory=self.factory)
        self.assertEqual((code, self.adapter.calls, self.receipt_files()), (3, 0, []))


if __name__ == "__main__":
    unittest.main()
