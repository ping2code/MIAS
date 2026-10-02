"""Phase 13C: the ``/api/v1`` read routes over the artifact store (views, canonical bytes, latest, history, cursors),
the delivery receipt view, readiness, and the auth/error contract on these routes. In-process TestClient only."""
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from alert_engine import receipts as rc
from alert_engine.delivery.base import DELIVERED, FAILED, DeliveryResult
from alert_engine.rendering import delivery_request
from api.app import create_app
from api.settings import ApiSettings
from artifact_store import kinds, store
from artifact_store.service import ArtifactStore
from tests import trade_setup_cases as ts
from tests.test_artifact_store import ID_FIELD, raw, samples

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READ_TOKEN = "read-token-canary-0123456789abcdef-XYZ"
PATHS = {"market-intelligence": "market-intelligence", "options-intelligence": "options-intelligence",
         "trade-setup": "trade-setups", "invalidation-check": "invalidation-checks", "alert": "alerts"}
SERVED = datetime(2026, 10, 2, 13, 30, tzinfo=timezone.utc)
FORBIDDEN_TEXT = ("Traceback", "/tmp", "store/", ".json", "canary")


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "store")
        self.receipts = os.path.join(self.tmp.name, "receipts")
        os.mkdir(self.root)
        os.mkdir(self.receipts)
        self.objects = samples()
        for kind, objects in self.objects.items():
            for obj in objects:
                store.publish(self.root, kind, raw(obj))

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, **settings):
        settings = dict(dict(artifact_root=self.root, receipt_root=self.receipts), **settings)
        self.app = create_app(ApiSettings(**settings), refresh_interval=0, clock=lambda: SERVED)
        client = TestClient(self.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def file(self, kind, obj):
        return os.path.join(self.root, kind, obj[ID_FIELD[kind]][7:] + ".json")

    def assert_error(self, response, status, code):
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (status, code), response.text)
        self.assertEqual(response.json()["error"]["request_id"], response.headers["x-request-id"])
        for text in FORBIDDEN_TEXT:
            self.assertNotIn(text, response.text)


class ResourceTests(ApiCase):
    def test_every_kind_get_view_and_canonical(self):
        c = self.client()
        for kind, objects in self.objects.items():
            for obj in objects:
                with self.subTest(kind=kind):
                    artifact_id = obj[ID_FIELD[kind]]
                    response = c.get(f"/api/v1/{PATHS[kind]}/{artifact_id}", headers={"X-Request-ID": "req-view-0001"})
                    self.assertEqual(response.status_code, 200)
                    body = response.json()
                    self.assertEqual(set(body), {"data", "meta"})
                    self.assertEqual(body["meta"], {"api_version": "v1", "view": f"{kind}-summary-v1",
                                                    "request_id": "req-view-0001",
                                                    "served_at": "2026-10-02T13:30:00+00:00"})
                    self.assertEqual(body["data"][ID_FIELD[kind]], artifact_id)
                    self.assertEqual(body["data"]["symbol"], "META")
                    canonical = c.get(f"/api/v1/{PATHS[kind]}/{artifact_id}/canonical")
                    with open(self.file(kind, obj), "rb") as handle:
                        stored = handle.read()
                    self.assertEqual(canonical.content, stored)              # exact bytes
                    self.assertEqual(canonical.content, (kinds.KINDS[kind].canonical_json(obj) + "\n").encode())
                    self.assertEqual(canonical.headers["content-type"], "application/json")
                    self.assertEqual(canonical.headers["etag"], f'"{artifact_id}"')
                    self.assertEqual(canonical.headers["cache-control"], "private, max-age=31536000, immutable")
                    self.assertEqual(json.loads(canonical.content), obj)      # no API metadata added to the body
                    self.assertIn("x-request-id", canonical.headers)

    def test_views_are_projections(self):
        c = self.client()
        setup = self.objects["trade-setup"][0]
        data = c.get(f"/api/v1/trade-setups/{setup['assessment_id']}").json()["data"]
        self.assertEqual(data, dict(
            assessment_id=setup["assessment_id"], assessment_format_version="phase10-v1", rules_version="phase10-rules-v1",
            symbol="META", as_of=setup["inputs"]["assessment_as_of"], outcome_status=setup["outcome"]["status"],
            no_setup_reasons=setup["outcome"]["no_setup_reasons"], market_bias_state=setup["market_bias"]["state"],
            eligible_side=setup["market_bias"]["side"], candidate_count=len(setup["candidates"]),
            market_intelligence_id=setup["provenance"]["market_intelligence_id"],
            options_intelligence_id=setup["provenance"]["options_intelligence_id"],
            policy_id=setup["provenance"]["policy_id"]))
        alert = self.objects["alert"][2]
        data = c.get(f"/api/v1/alerts/{alert['alert_id']}").json()["data"]
        self.assertEqual((data["alert_code"], data["transition"], data["facts"], data["subject_kind"]),
                         ("market_pattern_changed", alert["transition"], alert["facts"], "symbol"))
        for kind, objects in self.objects.items():
            for obj in objects:
                text = json.dumps(c.get(f"/api/v1/{PATHS[kind]}/{obj[ID_FIELD[kind]]}").json()["data"]).lower()
                for banned in ("score", "rank", "best", "confidence", "recommend", "strike", "contract_id"):
                    self.assertNotIn(banned, text, (kind, banned))
        for kind, objects in self.objects.items():                      # API metadata never enters a body
            for obj in objects:
                canonical = c.get(f"/api/v1/{PATHS[kind]}/{obj[ID_FIELD[kind]]}/canonical").json()
                self.assertEqual(set(canonical), set(obj))
                self.assertFalse({"meta", "served_at", "request_id", "api_version"} & set(canonical))

    def test_malformed_and_unknown_ids(self):
        c = self.client()
        for bad in ("sha256:" + "A" * 64, "sha256:" + "a" * 63, "abc", "sha256:" + "a" * 64 + "x", "sha256:" + "a" * 64 + "%20"):
            for suffix in ("", "/canonical"):
                with self.subTest(bad=bad, suffix=suffix):
                    self.assert_error(c.get(f"/api/v1/alerts/{bad}{suffix}"), 400, "invalid_request")
        for traversal in ("/api/v1/alerts/../../etc/passwd", "/api/v1/alerts/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                          "/api/v1/alerts/%2e%2e/canonical"):
            self.assertIn(c.get(traversal).status_code, (400, 404))          # never a file read
        missing = "sha256:" + "0" * 64
        self.assert_error(c.get(f"/api/v1/alerts/{missing}"), 404, "not_found")
        self.assert_error(c.get(f"/api/v1/alerts/{missing}/canonical"), 404, "not_found")
        setup_id = self.objects["trade-setup"][0]["assessment_id"]
        self.assert_error(c.get(f"/api/v1/alerts/{setup_id}"), 404, "not_found")       # ids belong to their kind
        self.assert_error(c.get(f"/api/v1/alerts/{missing}?x=1"), 400, "invalid_request")

    def test_post_index_tamper_detection(self):
        c = self.client()
        alert = self.objects["alert"][0]
        path = self.file("alert", alert)
        os.chmod(path, 0o644)
        with open(path, "r+b") as handle:
            handle.write(b"[")
        for suffix in ("", "/canonical"):
            self.assert_error(c.get(f"/api/v1/alerts/{alert['alert_id']}{suffix}"), 500, "artifact_invalid")
        os.unlink(path)
        self.assert_error(c.get(f"/api/v1/alerts/{alert['alert_id']}/canonical"), 500, "artifact_invalid")
        self.assert_error(c.get("/api/v1/alerts?symbol=META"), 500, "artifact_invalid")


class QueryTests(ApiCase):
    def test_latest(self):
        c = self.client()
        mis = self.objects["market-intelligence"]
        response = c.get("/api/v1/market-intelligence/latest?symbol=META")
        self.assertEqual(response.json()["data"]["intelligence_id"], mis[2]["intelligence_id"])
        cutoff = mis[1]["synthesis_ref"]["as_of"].replace("+00:00", "Z")
        response = c.get("/api/v1/market-intelligence/latest", params={"symbol": "META", "as_of": cutoff})
        self.assertEqual(response.json()["data"]["intelligence_id"], mis[1]["intelligence_id"])
        response = c.get("/api/v1/market-intelligence/latest", params={"symbol": "META",
                                                                       "as_of": "2026-09-24T15:05:00-05:00"})
        self.assertEqual(response.json()["data"]["intelligence_id"], mis[1]["intelligence_id"])
        self.assert_error(c.get("/api/v1/market-intelligence/latest", params={"symbol": "META",
                                                                              "as_of": "2000-01-01T00:00:00Z"}),
                          404, "not_found")
        for params in ({}, {"symbol": "meta"}, {"symbol": "META", "as_of": "2026-09-24T20:05:00"},
                       {"symbol": "META", "as_of": "2026-09-24"}, {"symbol": "META", "limit": "5"},
                       {"symbol": "META,NVDA"}, {"symbol": "../META"}):
            with self.subTest(params=params):
                self.assert_error(c.get("/api/v1/market-intelligence/latest", params=params), 400, "invalid_request")
        repeated = c.get("/api/v1/market-intelligence/latest?symbol=META&symbol=NVDA")
        self.assert_error(repeated, 400, "invalid_request")
        self.assert_error(c.get("/api/v1/alerts/latest?symbol=NVDA"), 404, "not_found")

    def test_latest_tie_is_ambiguous(self):
        tie = ts.market_intelligence("positive_market_return")
        store.publish(self.root, "market-intelligence", raw(tie))
        c = self.client()
        as_of = tie["synthesis_ref"]["as_of"]
        self.assert_error(c.get("/api/v1/market-intelligence/latest", params={"symbol": "META", "as_of": as_of}),
                          409, "ambiguous_latest")
        self.assertEqual(c.get("/api/v1/market-intelligence/latest?symbol=META").status_code, 200)

    def test_history_pagination(self):
        c = self.client()
        response = c.get("/api/v1/market-intelligence")
        body = response.json()
        self.assertEqual((body["meta"]["limit"], body["meta"]["next_cursor"], body["meta"]["view"]),
                         (50, None, "market-intelligence-summary-v1"))
        ids = [d["intelligence_id"] for d in body["data"]]
        expected = [m["intelligence_id"] for m in sorted(self.objects["market-intelligence"],
                                                         key=lambda m: m["synthesis_ref"]["as_of"], reverse=True)]
        self.assertEqual(ids, expected)
        first = c.get("/api/v1/market-intelligence?symbol=META&limit=2").json()
        self.assertEqual(([d["intelligence_id"] for d in first["data"]], first["meta"]["limit"]), (expected[:2], 2))
        cursor = first["meta"]["next_cursor"]
        self.assertRegex(cursor, r"^[A-Za-z0-9_-]+$")
        second = c.get("/api/v1/market-intelligence", params={"symbol": "META", "limit": "2", "cursor": cursor}).json()
        self.assertEqual(([d["intelligence_id"] for d in second["data"]], second["meta"]["next_cursor"]),
                         (expected[2:], None))
        self.assertEqual(c.get("/api/v1/market-intelligence?limit=200").status_code, 200)
        for params in ({"limit": "0"}, {"limit": "201"}, {"limit": "-1"}, {"limit": "abc"}, {"limit": "1e2"},
                       {"cursor": "not-a-cursor"}, {"cursor": cursor},     # bound to the symbol=META query
                       {"as_of_from": "2026-09-30T00:00:00Z", "as_of_to": "2026-09-01T00:00:00Z"},
                       {"as_of_from": "2026-09-01"}, {"sort": "score"}, {"page": "2"}):
            with self.subTest(params=params):
                self.assert_error(c.get("/api/v1/market-intelligence", params=params), 400, "invalid_request")
        window = c.get("/api/v1/market-intelligence", params={"as_of_from": expected and "2026-09-24T00:00:00Z",
                                                                "as_of_to": "2026-09-25T00:00:00Z"}).json()
        self.assertTrue(all("2026-09-24" in d["as_of"] for d in window["data"]))
        self.assertEqual(c.get("/api/v1/market-intelligence?symbol=NVDA").json()["data"], [])


class DeliveryViewTests(ApiCase):
    def add(self, alert, result, when="2026-10-02T13:30:00.000000+00:00"):
        rc.append_receipt(self.receipts, delivery_request(alert, "telegram"), result, when, when)

    def test_no_receipts_and_histories(self):
        c = self.client()
        alert, other = self.objects["alert"][0], self.objects["alert"][1]
        url = f"/api/v1/alerts/{alert['alert_id']}/deliveries"
        body = c.get(url).json()
        self.assertEqual((body["data"], body["meta"]["view"]), ([], "alert-deliveries-v1"))
        self.add(alert, DeliveryResult(FAILED, None, 3, "timeout"))
        self.add(other, DeliveryResult(DELIVERED, "999", 1, None))
        self.add(alert, DeliveryResult(DELIVERED, "777", 1, None))
        data = c.get(url).json()["data"]
        self.assertEqual([(d["sequence"], d["status"], d["provider_message_id"], d["safe_error_code"], d["attempts"])
                          for d in data], [(1, "failed", None, "timeout", 3), (2, "delivered", "777", None, 1)])
        self.assertEqual(set(data[0]), {"alert_id", "channel", "sequence", "status", "provider_message_id", "attempts",
                                        "safe_error_code", "attempted_at", "completed_at", "delivery_contract_version",
                                        "render_version"})
        text = c.get(url).text
        for banned in ("token", "chat", "http", "MIAS phase12 alert", "receipt_format_version"):
            self.assertNotIn(banned, text)

    def test_fails_closed(self):
        alert = self.objects["alert"][0]
        url = f"/api/v1/alerts/{alert['alert_id']}/deliveries"
        self.add(alert, DeliveryResult(DELIVERED, "777", 1, None))
        with open(os.path.join(self.receipts, "stray.txt"), "w") as handle:
            handle.write("x")
        c = self.client()
        self.assert_error(c.get(url), 500, "artifact_invalid")
        self.assertEqual(c.get("/health/ready").status_code, 503)
        os.unlink(os.path.join(self.receipts, "stray.txt"))
        self.assertEqual(c.get(url).status_code, 200)
        self.assert_error(c.get(f"/api/v1/alerts/{'sha256:' + '0' * 64}/deliveries"), 404, "not_found")
        unset = self.client(receipt_root=None)
        self.assert_error(unset.get(url), 503, "dependency_unavailable")
        self.assert_error(c.get(f"/api/v1/trade-setups/{alert['alert_id']}/deliveries"), 404, "not_found")


class ReadinessTests(ApiCase):
    def checks(self, client):
        response = client.get("/health/ready")
        return response.status_code, {c["name"]: c["status"] for c in response.json()["checks"]}, response.text

    def test_states(self):
        status, checks, text = self.checks(self.client())
        self.assertEqual((status, checks), (200, {"settings": "pass", "artifact_root": "pass", "artifact_index": "pass",
                                                  "receipt_root": "pass"}))
        self.assertNotIn(self.root, text)
        empty = os.path.join(self.tmp.name, "empty")
        os.mkdir(empty)
        self.assertEqual(self.checks(self.client(artifact_root=empty, receipt_root=None))[0], 200)
        status, checks, _ = self.checks(self.client(artifact_root=os.path.join(self.tmp.name, "absent")))
        self.assertEqual((status, checks["artifact_root"], checks["artifact_index"]), (503, "fail", "fail"))
        status, checks, _ = self.checks(self.client(artifact_root=None))
        self.assertEqual((status, checks["artifact_root"]), (503, "fail"))
        status, checks, _ = self.checks(self.client(receipt_root=os.path.join(self.tmp.name, "nope")))
        self.assertEqual((status, checks["receipt_root"]), (503, "fail"))
        self.assertEqual(set(checks), {"settings", "artifact_root", "artifact_index", "receipt_root"})

    def test_corruption_fails_readiness_and_recovers(self):
        alert = self.objects["alert"][0]
        os.chmod(self.file("alert", alert), 0o644)
        with open(self.file("alert", alert), "ab") as handle:
            handle.write(b" ")
        c = self.client()
        status, checks, text = self.checks(c)
        self.assertEqual((status, checks["artifact_index"]), (503, "fail"))
        self.assertNotIn(".json", text)
        self.assert_error(c.get("/api/v1/alerts"), 503, "dependency_unavailable")   # never served unvalidated
        with open(self.file("alert", alert), "wb") as handle:
            handle.write(raw(alert))
        self.app.state.artifact_store.refresh()
        self.assertEqual(self.checks(c)[0], 200)

    def test_failed_refresh_keeps_serving_previous_snapshot(self):
        c = self.client()
        with open(os.path.join(self.root, "alert", "unexpected.txt"), "w") as handle:
            handle.write("x")
        self.assertFalse(self.app.state.artifact_store.refresh())
        self.assertEqual(self.checks(c)[0], 503)
        self.assertEqual(c.get("/api/v1/alerts").status_code, 200)

    def test_new_artifacts_appear_after_refresh(self):
        c = self.client()
        later = ts.later_mi("all_bullish", 5 * 86400)
        store.publish(self.root, "market-intelligence", raw(later))
        self.assertNotEqual(c.get("/api/v1/market-intelligence/latest?symbol=META").json()["data"]["intelligence_id"],
                            later["intelligence_id"])
        self.app.state.artifact_store.refresh()
        self.assertEqual(c.get("/api/v1/market-intelligence/latest?symbol=META").json()["data"]["intelligence_id"],
                         later["intelligence_id"])

    def test_background_refresher_runs_and_stops(self):
        app = create_app(ApiSettings(artifact_root=self.root), refresh_interval=0.05)
        with mock.patch.object(ArtifactStore, "refresh", wraps=app.state.artifact_store.refresh) as refresh:
            with TestClient(app):
                import time
                deadline = time.monotonic() + 5
                while refresh.call_count < 3 and time.monotonic() < deadline:
                    time.sleep(0.02)
            calls = refresh.call_count
        self.assertGreaterEqual(calls, 3)
        import threading
        self.assertFalse(any(t.name == "mias-artifact-refresh" and t.is_alive() for t in threading.enumerate()))


class AuthAndIsolationTests(ApiCase):
    def test_read_auth_applies_to_every_route(self):
        c = self.client(read_token=READ_TOKEN)
        alert_id = self.objects["alert"][0]["alert_id"]
        urls = [f"/api/v1/{p}" for p in PATHS.values()] + [f"/api/v1/{p}/latest?symbol=META" for p in PATHS.values()]
        urls += [f"/api/v1/alerts/{alert_id}", f"/api/v1/alerts/{alert_id}/canonical",
                 f"/api/v1/alerts/{alert_id}/deliveries"]
        for url in urls:
            with self.subTest(url=url):
                self.assert_error(c.get(url), 401, "unauthorized")
                # trade-setups/latest is 409: the two sample setups share one sealed as_of (a tie, not a pick)
                expected = 409 if url.startswith("/api/v1/trade-setups/latest") else 200
                self.assertEqual(c.get(url, headers={"Authorization": f"Bearer {READ_TOKEN}"}).status_code, expected)

    def test_only_get_routes_and_no_actions(self):
        c = self.client()
        alert_id = self.objects["alert"][0]["alert_id"]
        for method in ("post", "put", "delete", "patch"):
            for url in ("/api/v1/alerts", f"/api/v1/alerts/{alert_id}", f"/api/v1/alerts/{alert_id}/deliveries"):
                self.assert_error(getattr(c, method)(url), 405, "method_not_allowed")
        self.assert_error(c.post(f"/api/v1/alerts/{alert_id}/deliver"), 404, "not_found")
        paths = c.get("/openapi.json").json()["paths"]
        self.assertTrue(all(set(ops) == {"get"} for ops in paths.values()))
        for banned in ("setup-evaluation", "replay", "restore", "run-once", "publish", "deliver/"):
            self.assertFalse(any(banned in p for p in paths), banned)

    def test_unexpected_errors_are_sanitized(self):
        c = TestClient(create_app(ApiSettings(artifact_root=self.root), refresh_interval=0), raise_server_exceptions=False)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        with mock.patch("api.routes.artifacts._view", side_effect=RuntimeError(f"boom {self.root} canary")):
            response = c.get(f"/api/v1/alerts/{self.objects['alert'][0]['alert_id']}")
        self.assert_error(response, 500, "internal")

    def test_api_imports_stay_isolated(self):
        code = ("import sys, api.app\n"
                "bad = {'dotenv', 'shared', 'collector', 'analyzer', 'openai', 'requests', 'redis', 'sqlalchemy',"
                " 'persistence', 'orchestrator', 'options_data', 'setup_evaluation', 'evidence', 'uvicorn'}\n"
                "extra = sorted(n for n in sys.modules if n.startswith(('alert_engine.delivery.telegram',"
                " 'alert_engine.delivery.guard', 'alert_engine.state_store', 'alert_engine.runner', 'market_data.provider')))\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & bad), extra)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                                env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.stdout.strip(), "[] []", result.stderr[-300:])

    def test_reads_never_modify_the_store(self):
        before = {}
        for dirpath, _, names in os.walk(self.root):
            for name in names:
                with open(os.path.join(dirpath, name), "rb") as handle:
                    before[os.path.join(dirpath, name)] = hashlib.sha256(handle.read()).hexdigest()
        c = self.client()
        for kind, objects in self.objects.items():
            c.get(f"/api/v1/{PATHS[kind]}")
            c.get(f"/api/v1/{PATHS[kind]}/latest?symbol=META")
            for obj in objects:
                c.get(f"/api/v1/{PATHS[kind]}/{obj[ID_FIELD[kind]]}")
                c.get(f"/api/v1/{PATHS[kind]}/{obj[ID_FIELD[kind]]}/canonical")
        after = {}
        for dirpath, _, names in os.walk(self.root):
            for name in names:
                with open(os.path.join(dirpath, name), "rb") as handle:
                    after[os.path.join(dirpath, name)] = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
