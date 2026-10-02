"""Phase 12D: deterministic rendering, the provider-neutral delivery contract, the hardened Telegram adapter, the
Redis delivery guard, and the narrow legacy Telegram hardening. Every provider call is stubbed: no Telegram, OpenAI
or network traffic. Redis is an in-memory fake, or the disposable test Redis when MIAS_PHASE2J_REDIS_URL is set."""
import ast
from contextlib import redirect_stdout
import importlib
import inspect
import io
import logging
import os
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

import redis
import requests

from alert_engine import rendering
from alert_engine.builder import market_pattern_changed, setup_available, setup_invalidated
from alert_engine.delivery import base, guard as dg, telegram as tg
from alert_engine.delivery.base import DELIVERED, FAILED, DeliveryRequest, DeliveryResult
from alert_engine.delivery.guard import (ALREADY_DELIVERED, IN_PROGRESS, SENT, DeliveryStateUnavailable,
                                         RedisDeliveryGuard)
from alert_engine.delivery.telegram import TelegramAdapter
from alert_engine.rendering import delivery_request, render
from alert_engine.state_store import RedisAlertStateStore
from alert_engine.validation import AlertInputError
from tests import setup_evaluation_cases as sc
from tests import trade_setup_cases as ts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "123456:SECRET-token-canary-AbCdEf"
CHAT = "-100987654321"


def alerts():
    return dict(setup_available=setup_available(sc.setup()).to_dict(),
                setup_invalidated=setup_invalidated(sc.check()).to_dict(),
                market_pattern_changed=market_pattern_changed(ts.market_intelligence("all_bullish"),
                                                              ts.later_mi("higher_aligned_5m_opposed")).to_dict())


GOLDEN = {
    "setup_available": "\n".join([
        "MIAS phase12 alert (phase12-render-v1)", "Event: setup available", "Symbol: META", "Market bias: bullish",
        "Eligible side: call", "Candidate count: 5", "Setup: sha256:b78e8f7a75cc03ae",
        "As of: 2026-09-30T14:45:00+00:00", "Alert: sha256:767998d5e92120b7"]),
    "setup_invalidated": "\n".join([
        "MIAS phase12 alert (phase12-render-v1)", "Event: setup invalidated", "Symbol: META", "Side: call",
        "Required pattern: all_bullish", "Observed pattern: all_bearish", "Observed technical status: available",
        "Setup: sha256:b78e8f7a75cc03ae", "As of: 2026-09-24T20:05:00+00:00", "Alert: sha256:071395d3e3ad78ef"]),
    "market_pattern_changed": "\n".join([
        "MIAS phase12 alert (phase12-render-v1)", "Event: market pattern changed", "Symbol: META",
        "Previous pattern: all_bullish", "Current pattern: opposed", "Elapsed seconds: 86400",
        "As of: 2026-09-24T20:05:00+00:00", "Alert: sha256:4bd0845d97ee3d69"]),
}


class RenderingTests(unittest.TestCase):
    def test_golden_and_repeatable(self):
        for code, alert in alerts().items():
            with self.subTest(code=code):
                self.assertEqual(render(alert), GOLDEN[code])
                self.assertEqual({render(alert) for _ in range(20)}, {GOLDEN[code]})

                def rev(v):
                    if isinstance(v, dict):
                        return {k: rev(v[k]) for k in reversed(list(v))}
                    return [rev(x) for x in v] if isinstance(v, list) else v
                self.assertEqual(render(rev(alert)), GOLDEN[code])

    def test_facts_only(self):
        assessment = sc.setup()
        banned = ("buy", "sell", "strong", "weak", "best", "confidence", "severity", "score", "profit", "loss",
                  "target", "stop", "strike", "expiration", "recommend", "rank", "predict", "http", "www", TOKEN)
        for code, alert in alerts().items():
            text = render(alert).lower()
            with self.subTest(code=code):
                for word in banned:
                    self.assertNotIn(word.lower(), text)
        text = render(alerts()["setup_available"])
        for candidate in assessment["candidates"]:
            for value in (candidate["source"]["contract_id"], candidate["source"]["provider_symbol"],
                          candidate["source"]["expiration"]):
                self.assertNotIn(value, text)

    def test_fails_closed(self):
        tampered = alerts()["setup_available"]
        tampered["facts"]["candidate_count"] = 9
        with self.assertRaises(AlertInputError):
            render(tampered)

    def test_delivery_request(self):
        alert = alerts()["setup_invalidated"]
        request = delivery_request(alert, "telegram")
        self.assertEqual((request.alert_id, request.channel, request.render_version, request.text),
                         (alert["alert_id"], "telegram", "phase12-render-v1", GOLDEN["setup_invalidated"]))
        with self.assertRaises(ValueError):
            delivery_request(alert, "email")

    def test_fresh_processes(self):
        code = ("from tests.test_alert_delivery import alerts\nfrom alert_engine.rendering import render\n"
                "import hashlib\na = alerts()\n"
                "print(hashlib.sha256('\\x00'.join(render(a[k]) for k in sorted(a)).encode()).hexdigest())")
        import hashlib
        expected = hashlib.sha256("\x00".join(GOLDEN[k] for k in sorted(GOLDEN)).encode()).hexdigest()
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "9", "TZ": "Asia/Tokyo"},
                      {"PYTHONHASHSEED": "4", "LANG": "C", "LC_ALL": "C", "HOSTNAME": "elsewhere", "MIAS_UNRELATED": "1"}):
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


class ContractTests(unittest.TestCase):
    def test_results(self):
        self.assertEqual(base.STATUSES, ("delivered", "failed"))
        self.assertEqual(DeliveryResult(DELIVERED, "42", 1, None).status, "delivered")
        self.assertEqual(DeliveryResult(FAILED, None, 3, "timeout").error_code, "timeout")
        for bad in ((DELIVERED, None, 1, None), (DELIVERED, "42", 1, "timeout"), (FAILED, "42", 1, "timeout"),
                    (FAILED, None, 1, None), (FAILED, None, 1, "boom"), ("queued", None, 1, None),
                    (DELIVERED, "42", 0, None), (DELIVERED, "42", True, None)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                DeliveryResult(*bad)

    def test_no_secret_fields(self):
        from dataclasses import fields
        names = {f.name for cls in (DeliveryRequest, DeliveryResult) for f in fields(cls)}
        self.assertEqual(names, {"alert_id", "channel", "render_version", "text", "status", "provider_message_id",
                                 "attempts", "error_code"})
        with self.assertRaises(ValueError):
            DeliveryRequest("not-an-id", "telegram", "phase12-render-v1", "x")


class Response:
    def __init__(self, status=200, body=None, invalid=False):
        self.status_code, self._body, self._invalid = status, body, invalid

    def json(self):
        if self._invalid:
            raise requests.JSONDecodeError("Expecting value", "<html>", 0)
        return self._body


class Session:
    """Stub for requests.Session: replays scripted responses or exceptions; records every call."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


OK = Response(200, {"ok": True, "result": {"message_id": 777}})
URL_ERROR = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


class TelegramAdapterTests(unittest.TestCase):
    def send(self, *outcomes, attempts=3):
        session, sleeps = Session(*outcomes), []
        adapter = TelegramAdapter(TOKEN, CHAT, session=session, max_attempts=attempts, sleep=sleeps.append)
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            result = adapter.send(delivery_request(alerts()["setup_available"], "telegram"))
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        self.assertNotIn(TOKEN, repr(result) + str(result) + repr(adapter) + logs.getvalue())
        self.assertNotIn(CHAT, repr(result) + repr(adapter))
        return result, session, sleeps

    def test_delivered(self):
        result, session, sleeps = self.send(OK)
        self.assertEqual((result.status, result.provider_message_id, result.attempts, result.error_code),
                         (DELIVERED, "777", 1, None))
        url, kwargs = session.calls[0]
        self.assertEqual((kwargs["json"]["chat_id"], kwargs["timeout"], kwargs["allow_redirects"]),
                         (CHAT, tg.DEFAULT_TIMEOUT, False))
        self.assertEqual(kwargs["json"]["text"], GOLDEN["setup_available"])
        self.assertEqual(sleeps, [])

    def test_not_confirmed(self):
        for label, response, code in (
                ("ok false", Response(200, {"ok": False, "description": "bad"}), "telegram_rejected"),
                ("missing message_id", Response(200, {"ok": True, "result": {}}), "invalid_response"),
                ("no result", Response(200, {"ok": True}), "invalid_response"),
                ("malformed json", Response(200, invalid=True), "invalid_response"),
                ("non-object json", Response(200, ["ok"]), "invalid_response"),
                ("400 rejected", Response(400, {"ok": False}), "telegram_rejected")):
            with self.subTest(case=label):
                result, session, sleeps = self.send(response)
                self.assertEqual((result.status, result.error_code, result.attempts, len(session.calls)),
                                 (FAILED, code, 1, 1))                         # never retried
                self.assertIsNone(result.provider_message_id)

    def test_retried_and_bounded(self):
        for label, failure, code in (("timeout", requests.Timeout(URL_ERROR), "timeout"),
                                     ("transport", requests.ConnectionError(URL_ERROR), "transport_error"),
                                     ("http 503", Response(503, {"ok": False}), "http_error"),
                                     ("http 429", Response(429, {"ok": False}), "http_error"),
                                     ("unexpected", RuntimeError(URL_ERROR), "transport_error")):
            with self.subTest(case=label):
                result, session, sleeps = self.send(failure, failure, failure)
                self.assertEqual((result.status, result.error_code, result.attempts, len(session.calls)),
                                 (FAILED, code, 3, 3))
                self.assertEqual(sleeps, [tg.DEFAULT_BACKOFF_SECONDS] * 2)
        result, session, sleeps = self.send(requests.Timeout(URL_ERROR), OK)
        self.assertEqual((result.status, result.attempts, result.provider_message_id), (DELIVERED, 2, "777"))

    def test_configuration_and_inputs(self):
        for args in (("", CHAT), (None, CHAT), (TOKEN, ""), (TOKEN, None)):
            with self.subTest(args=args), self.assertRaises(ValueError) as caught:
                TelegramAdapter(*args)
            self.assertNotIn(TOKEN, str(caught.exception))
        adapter = TelegramAdapter(TOKEN, CHAT, session=Session())
        long_request = DeliveryRequest("sha256:" + "a" * 64, "telegram", "phase12-render-v1", "x" * 5000)
        with self.assertRaises(ValueError) as caught:
            adapter.send(long_request)
        self.assertNotIn(TOKEN, str(caught.exception))
        with self.assertRaises(ValueError):
            TelegramAdapter(TOKEN, CHAT, max_attempts=0)


class FakeRedis:
    def __init__(self, fail=()):
        self.data, self.ttl, self.fail = {}, {}, set(fail)

    def _check(self, op):
        if op in self.fail:
            raise redis.ConnectionError("simulated outage")

    def exists(self, key):
        self._check("read")
        return int(key in self.data)

    def set(self, key, value, nx=False, ex=None):
        self._check("lease" if nx else "marker")
        if nx and key in self.data:
            return None
        self.data[key], self.ttl[key] = value, ex
        return True

    def eval(self, script, numkeys, key, token):
        self._check("release")
        assert script == dg.RELEASE
        if self.data.get(key) == token:
            del self.data[key]
            return 1
        return 0


class CountingAdapter:
    channel = "telegram"

    def __init__(self, result):
        self.result, self.calls = result, 0

    def send(self, request):
        self.calls += 1
        return self.result


DELIVERED_RESULT = DeliveryResult(DELIVERED, "777", 1, None)
FAILED_RESULT = DeliveryResult(FAILED, None, 3, "timeout")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.request = delivery_request(alerts()["setup_available"], "telegram")

    def test_first_send_marks_delivered(self):
        fake, adapter = FakeRedis(), CountingAdapter(DELIVERED_RESULT)
        g = RedisDeliveryGuard(fake)
        outcome = g.deliver(self.request, adapter)
        self.assertEqual((outcome.action, outcome.result, adapter.calls), (SENT, DELIVERED_RESULT, 1))
        delivered = g.key(self.request, "delivered")
        self.assertEqual((fake.data[delivered], fake.ttl[delivered]), ("777", None))   # persistent marker
        self.assertNotIn(g.key(self.request, "lease"), fake.data)                       # lease released
        self.assertEqual(g.deliver(self.request, adapter).action, ALREADY_DELIVERED)
        self.assertEqual(adapter.calls, 1)

    def test_failed_send_stays_retryable(self):
        fake, adapter = FakeRedis(), CountingAdapter(FAILED_RESULT)
        g = RedisDeliveryGuard(fake)
        self.assertEqual(g.deliver(self.request, adapter).result, FAILED_RESULT)
        self.assertEqual(fake.data, {})
        adapter.result = DELIVERED_RESULT
        self.assertEqual(g.deliver(self.request, adapter).action, SENT)
        self.assertEqual(adapter.calls, 2)

    def test_lease_blocks_and_expires(self):
        fake, adapter = FakeRedis(), CountingAdapter(DELIVERED_RESULT)
        g = RedisDeliveryGuard(fake, lease_seconds=120)
        fake.data[g.key(self.request, "lease")] = "other-sender"
        self.assertEqual((g.deliver(self.request, adapter).action, adapter.calls), (IN_PROGRESS, 0))
        del fake.data[g.key(self.request, "lease")]                                       # the TTL elapsed
        self.assertEqual((g.deliver(self.request, adapter).action, adapter.calls), (SENT, 1))
        self.assertEqual(fake.ttl[g.key(self.request, "lease")], 120)

    def test_fail_closed(self):
        for fail in ("read", "lease"):
            adapter = CountingAdapter(DELIVERED_RESULT)
            with self.subTest(fail=fail), self.assertRaises(DeliveryStateUnavailable):
                RedisDeliveryGuard(FakeRedis(fail={fail})).deliver(self.request, adapter)
            self.assertEqual(adapter.calls, 0)                                            # nothing sent
        fake, adapter = FakeRedis(fail={"marker"}), CountingAdapter(DELIVERED_RESULT)
        g = RedisDeliveryGuard(fake)
        with self.assertRaises(DeliveryStateUnavailable) as caught:
            g.deliver(self.request, adapter)
        self.assertIn("delivered but the delivered marker was not recorded", str(caught.exception))
        self.assertIn(g.key(self.request, "lease"), fake.data)                            # lease kept until its TTL
        self.assertEqual(g.deliver(self.request, adapter).action, IN_PROGRESS)            # no immediate re-send

    def test_keys_separate_from_phase12c(self):
        g, store = RedisDeliveryGuard(FakeRedis()), RedisAlertStateStore(FakeRedis())
        keys = {g.key(self.request, "lease"), g.key(self.request, "delivered")}
        hex_id = self.request.alert_id.split(":")[1]
        self.assertEqual(keys, {f"mias:phase12:delivery:telegram:{hex_id}:lease",
                                f"mias:phase12:delivery:telegram:{hex_id}:delivered"})
        self.assertNotIn(store.seen_key(self.request.alert_id), keys)
        with self.assertRaises(ValueError):
            g.deliver(self.request, SimpleNamespace(channel="email", send=lambda r: None))


class DeliverySemanticsTests(unittest.TestCase):
    """Pre-merge proofs of two documented delivery semantics (no behaviour change)."""

    def setUp(self):
        self.request = delivery_request(alerts()["setup_available"], "telegram")

    def test_already_delivered_short_circuits(self):
        fake = FakeRedis()
        g = RedisDeliveryGuard(fake)
        fake.data[g.key(self.request, "delivered")] = "777"
        session = Session()                                    # any provider call would fail: no scripted outcome
        adapter = TelegramAdapter(TOKEN, CHAT, session=session, sleep=lambda s: None)
        writes = []
        fake.set = lambda *a, **k: writes.append(("set", a, k))
        fake.eval = lambda *a, **k: writes.append(("eval", a, k))
        outcome = g.deliver(self.request, adapter)
        self.assertEqual((outcome.action, outcome.result), (ALREADY_DELIVERED, None))
        self.assertEqual((session.calls, writes), ([], []))   # no lease, no release, no send, no network

    def test_marker_failure_keeps_lease_then_allows_retry_after_expiry(self):
        fake, adapter = FakeRedis(fail={"marker"}), CountingAdapter(DELIVERED_RESULT)
        g = RedisDeliveryGuard(fake)
        lease = g.key(self.request, "lease")
        with self.assertRaises(DeliveryStateUnavailable):
            g.deliver(self.request, adapter)                   # confirmed send; marker write fails
        self.assertEqual((adapter.calls, lease in fake.data, g.key(self.request, "delivered") in fake.data),
                         (1, True, False))                     # lease intentionally kept
        self.assertEqual(g.deliver(self.request, adapter).action, IN_PROGRESS)
        self.assertEqual(adapter.calls, 1)                     # no other send before the lease expires
        fake.fail.discard("marker")
        del fake.data[lease]                                   # the lease TTL elapsed
        self.assertEqual(g.deliver(self.request, adapter).action, SENT)
        self.assertEqual(adapter.calls, 2)                     # the documented at-least-once duplicate send
        self.assertEqual(fake.data[g.key(self.request, "delivered")], "777")


@unittest.skipUnless(os.environ.get("MIAS_PHASE2J_REDIS_URL"), "disposable test Redis not configured")
class DisposableRedisGuardTests(unittest.TestCase):
    def setUp(self):
        self.client = redis.Redis.from_url(os.environ["MIAS_PHASE2J_REDIS_URL"], decode_responses=True, socket_timeout=5)
        self.request = delivery_request(alerts()["market_pattern_changed"], "telegram")
        self.guard = RedisDeliveryGuard(self.client, namespace=f"mias:phase12test:{uuid.uuid4().hex}", lease_seconds=1)

    def tearDown(self):
        self.client.delete(self.guard.key(self.request, "lease"), self.guard.key(self.request, "delivered"))

    def test_real_lease_and_marker(self):
        adapter = CountingAdapter(DELIVERED_RESULT)
        self.client.set(self.guard.key(self.request, "lease"), "other", nx=True, ex=1)
        self.assertEqual(self.guard.deliver(self.request, adapter).action, IN_PROGRESS)
        time.sleep(1.5)                                                                     # the lease TTL elapses
        self.assertEqual(self.guard.deliver(self.request, adapter).action, SENT)
        self.assertEqual(self.client.ttl(self.guard.key(self.request, "delivered")), -1)
        self.assertEqual(self.guard.deliver(self.request, adapter).action, ALREADY_DELIVERED)
        self.assertEqual(adapter.calls, 1)


class LegacyHardeningTests(unittest.TestCase):
    def test_notifier_does_not_load_dotenv(self):
        source = inspect.getsource(importlib.import_module("alert_engine.telegram_notifier"))
        self.assertNotIn("load_dotenv", source.split('"""', 2)[2])
        code = ("import dotenv\n"
                "def boom(*a, **k):\n    raise SystemExit('load_dotenv called')\n"
                "dotenv.load_dotenv = boom\nimport alert_engine.telegram_notifier\nprint('ok')")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "ok", result.stderr[-300:])

    def test_notifier_errors_and_collector_logs_have_no_token(self):
        from alert_engine import telegram_notifier as notifier
        from collector import rss_reader
        error = requests.HTTPError(f"401 Client Error: Unauthorized for url: {URL_ERROR}")
        with mock.patch.object(notifier, "BOT_TOKEN", TOKEN), mock.patch.object(notifier, "CHAT_ID", CHAT), \
                mock.patch.object(notifier.requests, "post", side_effect=error), \
                mock.patch.object(notifier.time, "sleep"):
            with self.assertRaises(RuntimeError) as caught:
                notifier.send_telegram_alert("x")
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertIn("(HTTPError)", str(caught.exception))
            feed = SimpleNamespace(bozo=False, feed={"title": "News"}, entries=[
                dict(title="NVIDIA earnings guidance partnership", summary="", link="https://example.com/story")])
            with mock.patch.object(rss_reader.feedparser, "parse", return_value=feed), \
                    mock.patch.object(rss_reader, "is_duplicate", return_value=False), \
                    mock.patch.object(rss_reader, "is_near_duplicate_headline", return_value=False), \
                    self.assertLogs("collector", "ERROR") as logs, redirect_stdout(io.StringIO()):
                rss_reader.read_feed("https://example.com/feed", enable_ai=False, send_alerts=True)
        self.assertTrue(any("Telegram alert failed" in line for line in logs.output))
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_importing_live_scripts_sends_nothing(self):
        from alert_engine import telegram_notifier as notifier
        with mock.patch.object(notifier, "send_telegram_alert", side_effect=AssertionError("live send")) as send, \
                redirect_stdout(io.StringIO()) as out:
            for name in ("tests.test_telegram", "tests.test_full_alert_flow"):
                sys.modules.pop(name, None)
                importlib.import_module(name)
        send.assert_not_called()
        self.assertEqual(out.getvalue(), "")


class BoundaryTests(unittest.TestCase):
    def imports(self, module):
        names = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module)
        return names

    def test_module_imports(self):
        self.assertEqual(self.imports(rendering), {"alert_engine", "alert_engine.delivery.base", "alert_engine.validation"})
        self.assertEqual(self.imports(base), {"dataclasses"})
        self.assertEqual(self.imports(tg), {"time", "requests", "alert_engine.delivery.base"})
        self.assertEqual(self.imports(dg), {"dataclasses", "uuid", "redis", "alert_engine.delivery.base"})
        for module in (rendering, base, tg, dg):
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                name = getattr(node, "id", None) or getattr(node, "attr", None)
                self.assertNotIn(name, ("getenv", "environ", "load_dotenv", "now", "utcnow", "random", "uniform"),
                                 module.__name__)

    def test_runtime_imports(self):
        code = ("import sys, alert_engine.rendering, alert_engine.delivery.base\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'requests', 'redis', 'dotenv', 'shared', 'sqlalchemy',"
                " 'collector', 'orchestrator', 'openai', 'socket'} | (set(sys.modules) & {'alert_engine.telegram_notifier',"
                " 'alert_engine.formatter', 'alert_engine.decision_engine'})))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])


if __name__ == "__main__":
    unittest.main()
