"""Phase 13E: end-to-end integration and contract lock for the Phase 13 API.

The flow uses only the public and operator boundaries: sealed fixtures, the ``artifact_store.runner`` CLI, the Phase
12E receipt writer, ``load_api_settings`` from an explicit environment, ``create_app`` with its real lifespan, and HTTP
through TestClient. A restart is replayed in a fresh subprocess from the same immutable files and must answer
identically, with no Redis, PostgreSQL, provider or network module loaded. Local temporary directories only.
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from fastapi.testclient import TestClient

from alert_engine import receipts as rc
from alert_engine.delivery.base import DELIVERED, FAILED, DeliveryResult
from alert_engine.rendering import delivery_request
from api import views
from api.app import create_app
from api.errors import CODES, ERRORS
from api.settings import ApiConfigurationError, load_api_settings
from artifact_store import kinds, runner
from tests import trade_setup_cases as ts
from tests.test_artifact_store import ID_FIELD, samples

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATHS = {"market-intelligence": "market-intelligence", "options-intelligence": "options-intelligence",
         "trade-setup": "trade-setups", "invalidation-check": "invalidation-checks", "alert": "alerts"}
READ_TOKEN = "placeholder-read-token-0123456789abcdef"

# The transcript queried before and after a restart (paths relative to the API; ids filled in at run time).
TRANSCRIPT_QUERIES = [f"/api/v1/{p}" for p in PATHS.values()] + [
    "/api/v1/market-intelligence?symbol=META&limit=1", "/api/v1/market-intelligence/latest?symbol=META",
    "/api/v1/market-intelligence/latest?symbol=META&as_of=2026-09-24T20:05:00Z",
    "/api/v1/alerts/latest?symbol=META", "/api/v1/invalidation-checks/latest?symbol=META",
    "/api/v1/options-intelligence/latest?symbol=META", "/api/v1/trade-setups/latest?symbol=META",
    "/api/v1/alerts?as_of_from=2026-09-24T00:00:00Z&as_of_to=2026-09-25T00:00:00Z"]

# Runs a "restarted" service in a fresh interpreter: new settings, new app, new store, new index.
RESTART_SCRIPT = textwrap.dedent("""
    import json, os, sys
    from fastapi.testclient import TestClient
    from api.app import create_app
    from api.settings import load_api_settings
    queries = json.loads(sys.argv[1])
    app = create_app(load_api_settings(os.environ), refresh_interval=0)
    with TestClient(app) as client:
        out = [[q, r.status_code, r.content.decode()] for q, r in ((q, client.get(q)) for q in queries)]
        ready = client.get("/health/ready").status_code
    banned = {"redis", "sqlalchemy", "psycopg", "persistence", "requests", "openai", "dotenv", "shared", "collector",
              "analyzer", "orchestrator", "options_data", "uvicorn"}
    loaded = sorted({n.split(".")[0] for n in sys.modules} & banned)
    print(json.dumps({"out": out, "ready": ready, "loaded": loaded}))
""")


def strip_runtime(body):
    """A response with its runtime metadata (request_id, served_at) removed, for before/after comparison."""
    if isinstance(body, dict) and "meta" in body:
        return {"data": body["data"], "meta": {k: v for k, v in body["meta"].items()
                                               if k not in ("request_id", "served_at")}}
    if isinstance(body, dict) and "error" in body:
        return {"error": {k: v for k, v in body["error"].items() if k != "request_id"}}
    return body


class Phase13Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "artifacts")
        self.receipts = os.path.join(self.tmp.name, "receipts")
        self.sources = os.path.join(self.tmp.name, "sources")
        for path in (self.root, self.receipts, self.sources):
            os.mkdir(path)
        self.environ = {"MIAS_ARTIFACT_ROOT": self.root, "MIAS_RECEIPT_ROOT": self.receipts,
                        "MIAS_BUILD_ID": "phase13e-test"}
        self.objects = samples()

    def tearDown(self):
        self.tmp.cleanup()

    def publish_cli(self, kind, obj, pretty=True):
        """Publish through the operator CLI from a (pretty-printed by default) source file."""
        path = os.path.join(self.sources, f"{kind}-{obj[ID_FIELD[kind]][7:19]}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, indent=2 if pretty else None)
        out, err = io.StringIO(), io.StringIO()
        code = runner.main(["publish", "--kind", kind, "--file", path], out=out, err=err, environ=self.environ)
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue())

    def publish_everything(self):
        return [self.publish_cli(kind, obj) for kind, objects in self.objects.items() for obj in objects]

    def write_receipts(self):
        """Phase 12E durable receipts for two alerts, through the 12E receipt writer."""
        first, second = self.objects["alert"][0], self.objects["alert"][2]
        when = "2026-10-02T13:30:00.000000+00:00"
        for alert, result in ((first, DeliveryResult(FAILED, None, 3, "timeout")),
                              (first, DeliveryResult(DELIVERED, "777", 1, None)),
                              (second, DeliveryResult(DELIVERED, "778", 1, None))):
            rc.append_receipt(self.receipts, delivery_request(alert, "telegram"), result, when, when)

    def app(self, environ=None, **kwargs):
        app = create_app(load_api_settings(environ or self.environ), **kwargs)
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return app, client

    def stored(self, kind, obj):
        with open(os.path.join(self.root, kind, obj[ID_FIELD[kind]][7:] + ".json"), "rb") as handle:
            return handle.read()


class EndToEndTests(Phase13Case):
    def test_publish_index_read_and_restart(self):
        # 1-3. An empty but valid store is ready.
        _, empty = self.app()
        self.assertEqual(empty.get("/health/ready").status_code, 200)
        self.assertEqual(empty.get("/api/v1/alerts").json()["data"], [])
        # 4-6. Publish every kind through the CLI; write receipts through the 12E writer.
        summaries = self.publish_everything()
        self.assertTrue(all(s["result"] == "PUBLISHED" and s["canonicalized"] for s in summaries))
        self.write_receipts()
        # 7-8. A real app (its lifespan builds the index) answers over HTTP.
        app, client = self.app()
        self.assertEqual(client.get("/health/ready").json()["status"], "ready")
        for kind, objects in self.objects.items():
            for obj in objects:
                artifact_id = obj[ID_FIELD[kind]]
                view = client.get(f"/api/v1/{PATHS[kind]}/{artifact_id}").json()      # 9. view
                self.assertEqual((view["data"][ID_FIELD[kind]], view["meta"]["view"]),
                                 (artifact_id, f"{kind}-summary-v1"))
                canonical = client.get(f"/api/v1/{PATHS[kind]}/{artifact_id}/canonical")  # 10. exact bytes
                self.assertEqual(canonical.content, self.stored(kind, obj))
                self.assertEqual(json.loads(canonical.content), obj)
        mis = self.objects["market-intelligence"]                                      # 11. latest
        self.assertEqual(client.get("/api/v1/market-intelligence/latest?symbol=META").json()["data"]["intelligence_id"],
                         mis[2]["intelligence_id"])
        history = client.get("/api/v1/market-intelligence?symbol=META").json()         # 12. history
        self.assertEqual([d["intelligence_id"] for d in history["data"]],
                         [mis[2]["intelligence_id"], mis[1]["intelligence_id"], mis[0]["intelligence_id"]])
        walked, cursor = [], None                                                      # 13. pagination
        while True:
            params = {"symbol": "META", "limit": "1"} | ({"cursor": cursor} if cursor else {})
            page = client.get("/api/v1/market-intelligence", params=params).json()
            walked += [d["intelligence_id"] for d in page["data"]]
            cursor = page["meta"]["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(walked, [d["intelligence_id"] for d in history["data"]])
        first = self.objects["alert"][0]                                               # 14. receipts
        deliveries = client.get(f"/api/v1/alerts/{first['alert_id']}/deliveries").json()["data"]
        self.assertEqual([(d["sequence"], d["status"], d["provider_message_id"]) for d in deliveries],
                         [(1, "failed", None), (2, "delivered", "777")])
        no_receipts = self.objects["alert"][1]["alert_id"]
        self.assertEqual(client.get(f"/api/v1/alerts/{no_receipts}/deliveries").json()["data"], [])
        # 15-16. Restart: a fresh interpreter with no retained state rebuilds from the same files.
        queries = TRANSCRIPT_QUERIES + [f"/api/v1/{PATHS[k]}/{o[ID_FIELD[k]]}" + suffix
                                        for k, objs in self.objects.items() for o in objs
                                        for suffix in ("", "/canonical")]
        queries += [f"/api/v1/alerts/{a['alert_id']}/deliveries" for a in self.objects["alert"]]
        before = [[q, r.status_code, r.content.decode()] for q, r in ((q, client.get(q)) for q in queries)]
        app.state.artifact_store = None                                                # discard this process's state
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", **self.environ}
        result = subprocess.run([sys.executable, "-c", RESTART_SCRIPT, json.dumps(queries)], cwd=ROOT,
                                capture_output=True, text=True, timeout=300, env=env)
        restarted = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual((restarted["ready"], restarted["loaded"]), (200, []), result.stderr[-500:])
        for (q, status, text), (q2, status2, text2) in zip(before, restarted["out"]):
            self.assertEqual((q, status), (q2, status2))
            if q.endswith("/canonical"):
                self.assertEqual(text, text2)                                          # identical bytes
            else:
                self.assertEqual(strip_runtime(json.loads(text)), strip_runtime(json.loads(text2)), q)

    def test_identical_republish_and_cli_through_subprocess(self):
        alert = self.objects["alert"][0]
        self.publish_cli("alert", alert)
        self.assertEqual(self.publish_cli("alert", alert, pretty=False)["result"], "ALREADY_PRESENT")
        source = os.path.join(self.sources, "cli.json")
        with open(source, "w") as handle:
            json.dump(self.objects["trade-setup"][0], handle, indent=4)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "MIAS_ARTIFACT_ROOT": self.root}
        result = subprocess.run([sys.executable, "-m", "artifact_store.runner", "publish", "--kind", "trade-setup",
                                 "--file", source], cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"], "PUBLISHED")
        result = subprocess.run([sys.executable, "-m", "artifact_store.runner", "verify"], cwd=ROOT,
                                capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(json.loads(result.stdout)["counts"]["trade-setup"], 1)


class RefreshTests(Phase13Case):
    def test_publish_refresh_corrupt_recover(self):
        self.publish_cli("market-intelligence", self.objects["market-intelligence"][0])
        app, client = self.app()
        store = app.state.artifact_store
        newer = self.objects["market-intelligence"][1]
        self.publish_cli("market-intelligence", newer)
        latest = "/api/v1/market-intelligence/latest?symbol=META"
        self.assertNotEqual(client.get(latest).json()["data"]["intelligence_id"], newer["intelligence_id"])
        self.assertTrue(store.refresh())
        self.assertEqual(client.get(latest).json()["data"]["intelligence_id"], newer["intelligence_id"])
        # Corruption (made by test setup only): the refresh fails, reads continue from the previous snapshot.
        bad = os.path.join(self.root, "market-intelligence", "0" * 64 + ".json")
        with open(bad, "wb") as handle:
            handle.write(b'{"not": "an artifact"}\n')
        self.assertFalse(store.refresh())
        self.assertEqual(client.get("/health/ready").status_code, 503)
        self.assertEqual(client.get(latest).json()["data"]["intelligence_id"], newer["intelligence_id"])
        os.unlink(bad)                                                                 # test cleanup, not an API
        self.assertTrue(store.refresh())
        self.assertEqual(client.get("/health/ready").status_code, 200)

    def test_startup_with_corrupt_store_never_serves(self):
        self.publish_everything()
        alert = self.objects["alert"][0]
        path = os.path.join(self.root, "alert", alert["alert_id"][7:] + ".json")
        os.chmod(path, 0o644)
        with open(path, "ab") as handle:
            handle.write(b"\n")
        _, client = self.app()
        self.assertEqual(client.get("/health/ready").status_code, 503)
        self.assertEqual(client.get("/api/v1/alerts").json()["error"]["code"], "dependency_unavailable")


class CanonicalIntegrityTests(Phase13Case):
    def test_pretty_source_becomes_exact_canonical_bytes(self):
        for kind, objects in self.objects.items():
            obj = objects[0]
            with self.subTest(kind=kind):
                summary = self.publish_cli(kind, obj, pretty=True)
                expected = (kinds.KINDS[kind].canonical_json(obj) + "\n").encode("ascii")
                self.assertEqual(self.stored(kind, obj), expected)
                self.assertEqual((summary["artifact_id"], summary["bytes"]), (obj[ID_FIELD[kind]], len(expected)))
                body = {k: v for k, v in obj.items() if k != ID_FIELD[kind]}
                recomputed = "sha256:" + hashlib.sha256(kinds.KINDS[kind].canonical_json(body).encode()).hexdigest()
                self.assertEqual(recomputed, obj[ID_FIELD[kind]])                  # the content id still holds
        _, client = self.app()
        for kind, objects in self.objects.items():
            obj = objects[0]
            response = client.get(f"/api/v1/{PATHS[kind]}/{obj[ID_FIELD[kind]]}/canonical")
            self.assertEqual(response.content, self.stored(kind, obj))
            self.assertEqual(response.headers["etag"], f'"{obj[ID_FIELD[kind]]}"')

    def test_post_index_tamper_is_never_served(self):
        alert = self.objects["alert"][0]
        self.publish_cli("alert", alert)
        _, client = self.app()
        path = os.path.join(self.root, "alert", alert["alert_id"][7:] + ".json")
        os.chmod(path, 0o644)
        with open(path, "r+b") as handle:
            handle.seek(10)
            handle.write(b"X")
        for suffix in ("/canonical", ""):
            response = client.get(f"/api/v1/alerts/{alert['alert_id']}{suffix}")
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (500, "artifact_invalid"))
            self.assertNotIn(b"X", response.content.replace(b"X-", b""))


# --- The locked Phase 13 surface -------------------------------------------------------------------------------

LOCKED_ROUTES = {("GET", "/health/live"), ("GET", "/health/ready"), ("GET", "/api/v1/version"),
                 ("GET", "/api/v1/alerts/{artifact_id}/deliveries")}
LOCKED_ROUTES |= {("GET", "/api/v1/options-intelligence/activity")}         # 2026-10-06 additive contract decision
for _path in PATHS.values():
    LOCKED_ROUTES |= {("GET", f"/api/v1/{_path}"), ("GET", f"/api/v1/{_path}/latest"),
                      ("GET", f"/api/v1/{_path}/{{artifact_id}}"), ("GET", f"/api/v1/{_path}/{{artifact_id}}/canonical")}

LOCKED_VIEWS = {
    "MarketIntelligenceView": ["intelligence_id", "intelligence_format_version", "rules_version", "symbol", "as_of",
                               "synthesis_id", "timeframe_pattern", "technical_status", "market_context_available",
                               "conflict_codes", "attention"],
    "OptionsIntelligenceView": ["options_intelligence_id", "options_intelligence_format_version", "rules_version",
                                "symbol", "as_of", "snapshot_id", "contract_count"],
    "OptionsIntelligenceActivityView": [
        "options_intelligence_id", "symbol", "as_of", "contract_count", "expiration_count", "call_volume", "put_volume",
        "put_call_volume_ratio", "put_call_volume_ratio_reason", "iv_median", "volume_gt_oi_count", "call_breadth",
        "put_breadth", "call_concentration", "put_concentration", "concentration_reason", "comparison",
        "call_volume_change", "call_volume_change_pct", "call_volume_change_reason", "put_volume_change",
        "put_volume_change_pct", "put_volume_change_reason", "volume_gt_oi_change", "call_breadth_change",
        "put_breadth_change", "activity_bias", "momentum_15m", "trend_summary"],
    "ActivityComparison": ["status", "prior_options_intelligence_id", "prior_as_of", "session_date"],
    "TradeSetupView": ["assessment_id", "assessment_format_version", "rules_version", "symbol", "as_of",
                       "outcome_status", "no_setup_reasons", "market_bias_state", "eligible_side", "candidate_count",
                       "market_intelligence_id", "options_intelligence_id", "policy_id"],
    "InvalidationCheckView": ["invalidation_id", "invalidation_format_version", "rules_version", "symbol", "as_of",
                              "result", "reason", "assessment_id", "side", "required_pattern", "observed_pattern",
                              "observed_technical_status", "market_intelligence_id"],
    "AlertView": ["alert_id", "alert_format_version", "rules_version", "alert_code", "symbol", "subject_kind",
                  "assessment_id", "transition", "as_of", "facts", "source_refs"],
    "DeliveryView": ["alert_id", "channel", "sequence", "status", "provider_message_id", "attempts", "safe_error_code",
                     "attempted_at", "completed_at", "delivery_contract_version", "render_version"],
    "AttentionItem": ["code", "category"], "Transition": ["previous", "current"],
    "SourceRefView": ["role", "object_kind", "id"],
    "Meta": ["api_version", "view", "request_id", "served_at"],
    "ListMeta": ["api_version", "view", "request_id", "served_at", "limit", "next_cursor"],
    "ErrorDetail": ["code", "message", "request_id"], "ErrorView": ["error"],
    "LivenessView": ["status"], "CheckView": ["name", "status"], "ReadinessView": ["status", "checks"],
    "VersionView": ["service", "api_version", "build", "analytical_formats"],
    "AnalyticalFormat": ["object", "format_version", "rules_version"],
}
LOCKED_VIEW_NAMES = {"market-intelligence": "market-intelligence-summary-v1",
                     "options-intelligence": "options-intelligence-summary-v1",
                     "trade-setup": "trade-setup-summary-v1", "invalidation-check": "invalidation-check-summary-v1",
                     "alert": "alert-summary-v1"}


class ContractLockTests(Phase13Case):
    def surface(self, docs):
        env = dict(self.environ, MIAS_API_DOCS_ENABLED="true" if docs else "false")
        app = create_app(load_api_settings(env), refresh_interval=0)
        documented = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops}
        framework = {r.path for r in app.routes if getattr(r, "path", None)}
        return documented, framework

    def test_route_surface(self):
        documented, framework = self.surface(docs=True)
        self.assertEqual(documented, LOCKED_ROUTES)
        self.assertEqual(framework, {"/openapi.json", "/docs", "/docs/oauth2-redirect"})
        documented, framework = self.surface(docs=False)
        self.assertEqual((documented, framework), (LOCKED_ROUTES, set()))
        _, client = self.app()
        alert_id = "sha256:" + "a" * 64
        for method, path in (("POST", f"/api/v1/alerts/{alert_id}/deliver"), ("POST", "/api/v1/alerts/run-once"),
                             ("POST", "/api/v1/replay"), ("POST", "/api/v1/restore-state"),
                             ("POST", "/api/v1/artifacts/publish"), ("GET", "/api/v1/setup-evaluations"),
                             ("POST", "/api/v1/alerts"), ("DELETE", f"/api/v1/alerts/{alert_id}"),
                             ("PUT", f"/api/v1/alerts/{alert_id}/canonical"), ("HEAD", "/api/v1/alerts")):
            with self.subTest(method=method, path=path):
                response = client.request(method, path)
                self.assertIn(response.status_code, (404, 405))
                if method != "HEAD":
                    self.assertIn(response.json()["error"]["code"], ("not_found", "method_not_allowed"))

    def test_view_schemas(self):
        for name, fields in LOCKED_VIEWS.items():
            with self.subTest(view=name):
                model = getattr(views, name)
                self.assertEqual(list(model.model_fields), fields)
                self.assertEqual(model.model_config.get("extra"), "forbid")
        from api.projections import DELIVERY_VIEW, VIEW_NAMES
        self.assertEqual((VIEW_NAMES, DELIVERY_VIEW), (LOCKED_VIEW_NAMES, "alert-deliveries-v1"))
        self.publish_everything()
        _, client = self.app()
        for kind, objects in self.objects.items():                     # every live response matches the lock exactly
            model = {"market-intelligence": "MarketIntelligenceView", "options-intelligence": "OptionsIntelligenceView",
                     "trade-setup": "TradeSetupView", "invalidation-check": "InvalidationCheckView",
                     "alert": "AlertView"}[kind]
            body = client.get(f"/api/v1/{PATHS[kind]}/{objects[0][ID_FIELD[kind]]}").json()
            self.assertEqual(list(body["data"]), LOCKED_VIEWS[model])
            self.assertEqual(list(body["meta"]), LOCKED_VIEWS["Meta"])
            listing = client.get(f"/api/v1/{PATHS[kind]}").json()
            self.assertEqual(list(listing["meta"]), LOCKED_VIEWS["ListMeta"])

    def test_error_codes_and_kinds_locked(self):
        self.assertEqual(CODES, ("invalid_request", "unauthorized", "forbidden", "not_found", "method_not_allowed",
                                 "ambiguous_latest", "conflict", "payload_too_large", "artifact_invalid",
                                 "dependency_unavailable", "internal"))
        self.assertEqual({c: s for c, (s, _) in ERRORS.items()},
                         dict(invalid_request=400, unauthorized=401, forbidden=403, not_found=404,
                              method_not_allowed=405, ambiguous_latest=409, conflict=409, payload_too_large=413,
                              artifact_invalid=500, dependency_unavailable=503, internal=500))
        self.assertEqual(kinds.KIND_NAMES, ("market-intelligence", "options-intelligence", "trade-setup",
                                            "invalidation-check", "alert"))
        self.assertEqual(kinds.STORE_FORMAT_VERSION, "artifact-store-v1")
        from api.artifacts import REFRESH_INTERVAL_SECONDS
        from artifact_store.index import DEFAULT_LIMIT, MAX_CURSOR_LENGTH, MAX_LIMIT
        self.assertEqual((REFRESH_INTERVAL_SECONDS, DEFAULT_LIMIT, MAX_LIMIT, MAX_CURSOR_LENGTH), (30, 50, 200, 512))

    def test_latest_tie_and_window_contracts(self):
        tie = ts.market_intelligence("positive_market_return")
        self.publish_everything()
        self.publish_cli("market-intelligence", tie)
        _, client = self.app()
        response = client.get("/api/v1/market-intelligence/latest",
                              params={"symbol": "META", "as_of": tie["synthesis_ref"]["as_of"]})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "ambiguous_latest"))
        self.assertEqual(client.get("/api/v1/trade-setups/latest?symbol=META").status_code, 409)   # sealed tie
        self.assertEqual(client.get("/api/v1/market-intelligence/latest?symbol=META").status_code, 200)


class AuthAndHealthTests(Phase13Case):
    def test_bind_safety_and_bearer_flow(self):
        with self.assertRaises(ApiConfigurationError):
            load_api_settings(dict(self.environ, MIAS_API_HOST="0.0.0.0"))
        self.publish_everything()
        env = dict(self.environ, MIAS_API_HOST="0.0.0.0", MIAS_API_READ_TOKEN=READ_TOKEN)
        _, client = self.app(env)
        self.assertEqual(client.get("/health/live").status_code, 200)
        self.assertEqual(client.get("/health/ready").status_code, 200)
        self.assertEqual(client.get("/api/v1/alerts").status_code, 401)
        self.assertEqual(client.get("/api/v1/alerts", headers={"Authorization": f"Bearer {READ_TOKEN}"}).status_code,
                         200)

    def test_readiness_needs_only_local_state(self):
        env = dict(self.environ, MIAS_ARTIFACT_ROOT=os.path.join(self.tmp.name, "missing"))
        _, client = self.app(env)
        body = client.get("/health/ready").json()
        self.assertEqual({c["name"]: c["status"] for c in body["checks"]},
                         {"settings": "pass", "artifact_root": "fail", "artifact_index": "fail", "receipt_root": "pass"})
        self.assertNotIn(self.tmp.name, json.dumps(body))

    def test_hardening(self):
        self.publish_everything()
        _, client = self.app()
        for params in ({"cursor": "A" * 513}, {"symbol": "M" * 11}, {"as_of_from": "2026-09-24T00:00:00Z" * 20},
                       {"limit": "50", "limit ": "1"}, {"symbol": "META\x00"}):
            response = client.get("/api/v1/alerts", params=params)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "invalid_request"))
        response = client.get("/api/v1/alerts?limit=1&limit=2")
        self.assertEqual(response.status_code, 400)
        response = client.get("/api/v1/nope")
        self.assertNotIn("server", {k.lower() for k in response.headers})
        self.assertNotIn("traceback", response.text.lower())


class ImportInertnessTests(unittest.TestCase):
    def test_imports_touch_no_store_and_load_nothing_forbidden(self):
        with tempfile.TemporaryDirectory() as sentinel:
            code = textwrap.dedent(f"""
                import os, sys
                sentinel = {sentinel!r}
                touched = []
                real_open, real_listdir, real_scandir = os.open, os.listdir, os.scandir
                def spy(real):
                    def wrapper(path=".", *a, **k):
                        if isinstance(path, str) and path.startswith(sentinel):
                            touched.append(path)
                        return real(path, *a, **k)
                    return wrapper
                os.open, os.listdir, os.scandir = spy(real_open), spy(real_listdir), spy(real_scandir)
                import api, api.app, api.routes.artifacts, artifact_store, artifact_store.runner, artifact_store.service
                banned = {{"dotenv", "shared", "collector", "analyzer", "openai", "requests", "redis", "sqlalchemy",
                          "psycopg", "persistence", "orchestrator", "options_data", "setup_evaluation", "uvicorn"}}
                loaded = sorted({{n.split(".")[0] for n in sys.modules}} & banned)
                adapters = sorted(n for n in sys.modules if n in ("alert_engine.delivery.telegram",
                                  "alert_engine.delivery.guard", "alert_engine.state_store", "alert_engine.runner"))
                print(loaded, adapters, touched)
            """)
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                   "MIAS_ARTIFACT_ROOT": sentinel, "MIAS_RECEIPT_ROOT": sentinel}
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                                    timeout=120, env=env)
        self.assertEqual(result.stdout.strip(), "[] [] []", result.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
