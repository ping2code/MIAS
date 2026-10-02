"""Phase 13B: the API service foundation (app factory, settings, closed errors, request ids, auth, health, version).
Everything runs in-process through FastAPI's TestClient: no server, network, Redis, PostgreSQL, provider or .env."""
import ast
import inspect
import io
import logging
import os
import pkgutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from fastapi import Depends
from fastapi.testclient import TestClient

import api
from api import __main__ as api_main, auth, errors, request_context
from api.app import create_app
from api.errors import ERRORS, ApiError
from api.readiness import ReadinessCheck
from api.routes import meta
from api.settings import ApiConfigurationError, ApiSettings, load_api_settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READ_TOKEN = "read-token-canary-0123456789abcdef-XYZ"
OPERATOR_TOKEN = "operator-token-canary-0123456789abcdef"
API_MODULES = sorted(["api"] + [m.name for m in pkgutil.walk_packages(api.__path__, "api.")])


def client(**settings):
    return TestClient(create_app(ApiSettings(**settings)))


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args) + " " + repr(record.__dict__))


class AppFactoryTests(unittest.TestCase):
    def test_factory_and_route_table(self):
        with self.assertRaises(TypeError):
            create_app({"host": "127.0.0.1"})
        def table(app):                    # documented routes (in registration order) plus the framework doc routes
            spec = app.openapi()["paths"]
            return ([(path, sorted(spec[path])) for path in spec],
                    [r.path for r in app.routes if getattr(r, "path", None)])
        on, again = create_app(ApiSettings()), create_app(ApiSettings())
        self.assertEqual(table(on), table(again))
        self.assertEqual(table(on)[0][:3], [("/health/live", ["get"]), ("/health/ready", ["get"]),
                                            ("/api/v1/version", ["get"])])      # Phase 13C read routes follow
        self.assertEqual(table(on)[1], ["/openapi.json", "/docs", "/docs/oauth2-redirect"])
        off = create_app(ApiSettings(docs_enabled=False))
        self.assertEqual(table(off), (table(on)[0], []))
        self.assertFalse(on.debug)
        self.assertIsNot(on.state.settings, None)
        with self.assertRaises(ValueError):
            create_app(ApiSettings(), readiness_checks=[ReadinessCheck("a", lambda: True), ReadinessCheck("a", lambda: True)])

    def test_imports_have_no_side_effects(self):
        code = ("import sys\n"
                f"for m in {API_MODULES!r}: __import__(m)\n"
                "bad = {'dotenv', 'shared', 'collector', 'analyzer', 'openai', 'requests', 'redis', 'sqlalchemy', 'psycopg',"
                " 'persistence', 'orchestrator', 'options_data', 'setup_evaluation', 'evidence', 'evidence_packet', 'uvicorn',"
                " 'technical', 'market_context', 'live_validation', 'pandas', 'numpy', 'exchange_calendars'}\n"
                "loaded = sorted({n.split('.')[0] for n in sys.modules} & bad)\n"
                "providers = sorted(n for n in sys.modules if n.startswith(('market_data.provider', 'market_data.http',"
                " 'alert_engine.delivery.telegram', 'alert_engine.delivery.guard', 'alert_engine.telegram',"
                " 'alert_engine.state_store', 'alert_engine.runner')))\n"
                "print(loaded, providers)")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.stdout.strip(), "[] []", result.stderr[-400:])

    def test_source_boundaries(self):
        for name in API_MODULES:
            module = sys.modules.get(name) or __import__(name, fromlist=["_"])
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
                    for imported in names:
                        self.assertFalse(imported.split(".")[0] in {"subprocess", "socket", "shutil", "dotenv", "shared",
                                                                    "requests", "redis", "sqlalchemy", "openai"},
                                         (name, imported))
                if isinstance(node, ast.Call):
                    func = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                    self.assertNotIn(func, {"open", "getenv", "system", "popen", "load_dotenv", "listdir", "scandir"},
                                     (name, func))
                if isinstance(node, ast.Attribute) and node.attr == "environ":
                    self.assertEqual(name, "api.__main__", "only the process entry point reads os.environ")

    def test_domain_packages_stay_framework_free(self):
        framework = {"fastapi", "starlette", "pydantic", "uvicorn", "httpx"}
        for package in ("evidence_packet", "evidence_synthesis", "market_intelligence", "options_intelligence",
                        "options_data", "trade_setup", "setup_evaluation", "alert_engine", "market_data", "technical",
                        "market_context", "live_validation", "persistence", "orchestrator", "shared", "collector"):
            for path, _, files in os.walk(os.path.join(ROOT, package)):
                for file in files:
                    if file.endswith(".py"):
                        tree = ast.parse(open(os.path.join(path, file), encoding="utf-8").read())
                        for node in ast.walk(tree):
                            if isinstance(node, ast.Import):
                                self.assertFalse({a.name.split(".")[0] for a in node.names} & framework, (path, file))
                            elif isinstance(node, ast.ImportFrom) and node.module:
                                self.assertNotIn(node.module.split(".")[0], framework, (path, file))


class SettingsTests(unittest.TestCase):
    def test_defaults_and_environment(self):
        s = load_api_settings({})
        self.assertEqual((s.host, s.port, s.docs_enabled, s.read_token, s.operator_token, s.actions_enabled,
                          s.artifact_root, s.receipt_root, s.build_id),
                         ("127.0.0.1", 8080, True, None, None, False, None, None, "unknown"))
        s = load_api_settings({"MIAS_API_HOST": "0.0.0.0", "MIAS_API_PORT": "9000", "MIAS_API_DOCS_ENABLED": "FALSE",
                               "MIAS_API_READ_TOKEN": READ_TOKEN, "MIAS_API_OPERATOR_TOKEN": OPERATOR_TOKEN,
                               "MIAS_ARTIFACT_ROOT": "/srv/mias/artifacts", "MIAS_RECEIPT_ROOT": "/srv/mias/receipts",
                               "MIAS_BUILD_ID": "5e730ca", "UNRELATED_SECRET": "ignored"})
        self.assertEqual((s.host, s.port, s.docs_enabled, s.loopback, s.build_id), ("0.0.0.0", 9000, False, False, "5e730ca"))
        self.assertTrue(load_api_settings({"MIAS_API_HOST": "localhost"}).loopback)
        self.assertTrue(load_api_settings({"MIAS_API_HOST": "::1"}).loopback)

    def test_non_loopback_requires_read_auth(self):
        for host in ("0.0.0.0", "::", "10.0.0.5", "192.168.1.20", "2001:db8::1"):
            with self.subTest(host=host):
                with self.assertRaises(ApiConfigurationError) as caught:
                    load_api_settings({"MIAS_API_HOST": host})
                self.assertIn("MIAS_API_READ_TOKEN is required", str(caught.exception))
                with self.assertRaises(ApiConfigurationError):     # an operator token is not read auth
                    load_api_settings({"MIAS_API_HOST": host, "MIAS_API_OPERATOR_TOKEN": OPERATOR_TOKEN})
                self.assertEqual(load_api_settings({"MIAS_API_HOST": host, "MIAS_API_READ_TOKEN": READ_TOKEN}).host, host)
        run, err = mock.Mock(), io.StringIO()
        self.assertEqual(api_main.main({"MIAS_API_HOST": "0.0.0.0"}, run=run, err=err), 2)
        run.assert_not_called()                                   # refuses to start rather than serve open reads
        self.assertIn("MIAS_API_READ_TOKEN is required", err.getvalue())

    def test_invalid_values_never_echoed(self):
        cases = {"MIAS_API_HOST": ["example.com", "0.0.0.0 ", "1.2.3", "*"], "MIAS_API_PORT": ["0", "65536", "-1", "http", "80.5"],
                 "MIAS_API_DOCS_ENABLED": ["yes", "1"], "MIAS_API_ACTIONS_ENABLED": ["true", "maybe"],
                 "MIAS_API_READ_TOKEN": ["short-secret-canary", "x" * 31, "has space " + "y" * 30, "é" * 40, "z" * 257],
                 "MIAS_ARTIFACT_ROOT": ["relative/path-canary", "./x"], "MIAS_RECEIPT_ROOT": ["receipts-canary"],
                 "MIAS_BUILD_ID": ["bad id", "-x", "y" * 65]}
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value[:20]):
                    with self.assertRaises(ApiConfigurationError) as caught:
                        load_api_settings({name: value})
                    if value.strip():
                        self.assertNotIn(value.strip(), str(caught.exception))
        with self.assertRaises(ApiConfigurationError):
            ApiSettings(read_token=READ_TOKEN, operator_token=READ_TOKEN)
        with self.assertRaises(ApiConfigurationError):
            ApiSettings(port=True)

    def test_secrets_hidden_from_repr(self):
        s = ApiSettings(read_token=READ_TOKEN, operator_token=OPERATOR_TOKEN)
        self.assertNotIn(READ_TOKEN, repr(s))
        self.assertNotIn(OPERATOR_TOKEN, repr(s) + str(s))


class HealthTests(unittest.TestCase):
    def test_liveness_touches_nothing(self):
        probe = mock.Mock(return_value=True)
        c = TestClient(create_app(ApiSettings(read_token=READ_TOKEN), readiness_checks=[ReadinessCheck("probe", probe)]))
        response = c.get("/health/live")
        self.assertEqual((response.status_code, response.json()), (200, {"status": "live"}))
        probe.assert_not_called()

    def test_readiness(self):
        # Phase 13C: an artifact root and a built index are required; see tests/test_artifact_read_api.py.
        response = client().get("/health/ready")
        self.assertEqual((response.status_code, response.json()),
                         (503, {"status": "not_ready", "checks": [{"name": "settings", "status": "pass"},
                                                                  {"name": "artifact_root", "status": "fail"},
                                                                  {"name": "artifact_index", "status": "fail"}]}))
        with tempfile.TemporaryDirectory() as root:
            with TestClient(create_app(ApiSettings(artifact_root=root, receipt_root=os.path.join(
                    root, "missing-receipts-canary")), refresh_interval=0)) as c:
                response = c.get("/health/ready")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["checks"], [{"name": "settings", "status": "pass"},
                                                         {"name": "artifact_root", "status": "pass"},
                                                         {"name": "artifact_index", "status": "pass"},
                                                         {"name": "receipt_root", "status": "fail"}])
            self.assertNotIn(root, response.text)
            self.assertNotIn("canary", response.text)

    def test_failing_and_raising_probes(self):
        def boom():
            raise RuntimeError("redis://:secret-password-canary@db/0 /etc/secret-path-canary")
        checks = [ReadinessCheck("ok", lambda: True), ReadinessCheck("raises", boom),
                  ReadinessCheck("truthy_not_true", lambda: "yes")]
        response = TestClient(create_app(ApiSettings(), readiness_checks=checks)).get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready", "checks": [
            {"name": "ok", "status": "pass"}, {"name": "raises", "status": "fail"},
            {"name": "truthy_not_true", "status": "fail"}]})
        self.assertNotIn("canary", response.text)
        with self.assertRaises(ValueError):
            ReadinessCheck("Bad Name", lambda: True)

    def test_health_is_open_under_read_auth(self):
        with tempfile.TemporaryDirectory() as root:
            with TestClient(create_app(ApiSettings(read_token=READ_TOKEN, artifact_root=root), refresh_interval=0)) as c:
                self.assertEqual(c.get("/health/live").status_code, 200)
                self.assertEqual(c.get("/health/ready").status_code, 200)


class VersionTests(unittest.TestCase):
    def test_schema_and_safe_content(self):
        body = client(build_id="5e730ca").get("/api/v1/version").json()
        self.assertEqual(set(body), {"service", "api_version", "build", "analytical_formats"})
        self.assertEqual((body["service"], body["api_version"], body["build"]), ("mias-api", "v1", "5e730ca"))
        self.assertEqual(body["analytical_formats"], [
            {"object": "market_intelligence", "format_version": "phase8-v1", "rules_version": "phase8-rules-v1"},
            {"object": "options_intelligence", "format_version": "phase9-v1", "rules_version": "phase9-rules-v1"},
            {"object": "options_intelligence", "format_version": "phase9-v2", "rules_version": "phase9-rules-v2"},
            {"object": "trade_setup_assessment", "format_version": "phase10-v1", "rules_version": "phase10-rules-v1"},
            {"object": "invalidation_check", "format_version": "phase10-invalidation-v1",
             "rules_version": "phase10-invalidation-rules-v1"},
            {"object": "alert_event", "format_version": "phase12-v1", "rules_version": "phase12-rules-v1"}])
        self.assertNotIn("setup_evaluation", str(body))
        self.assertEqual(client().get("/api/v1/version").json(), client().get("/api/v1/version").json())

    def test_follows_read_policy(self):
        c = client(read_token=READ_TOKEN)
        self.assertEqual(c.get("/api/v1/version").status_code, 401)
        self.assertEqual(c.get("/api/v1/version", headers={"Authorization": f"Bearer {READ_TOKEN}"}).status_code, 200)


class RequestIdTests(unittest.TestCase):
    def test_generated_preserved_replaced(self):
        c = client()
        generated = c.get("/health/live").headers["x-request-id"]
        self.assertRegex(generated, r"^[0-9a-f]{32}$")
        self.assertNotEqual(generated, c.get("/health/live").headers["x-request-id"])
        good = "trace-ABC_123.x"
        self.assertEqual(c.get("/health/live", headers={"X-Request-ID": good}).headers["x-request-id"], good)
        self.assertEqual(c.get("/health/live", headers={"X-Request-ID": "a" * 64}).headers["x-request-id"], "a" * 64)
        for bad in ("short", "a" * 65, "has space here", "-leading-dash", "semi;colon;id", "", "ünïcode-request"):
            with self.subTest(bad=bad[:20]):
                returned = c.get("/health/live", headers={"X-Request-ID": bad.encode("utf-8")}).headers["x-request-id"]
                self.assertRegex(returned, r"^[0-9a-f]{32}$")
        twice = c.get("/health/live", headers=[("X-Request-ID", "first-id-123"), ("X-Request-ID", "second-id-456")])
        self.assertRegex(twice.headers["x-request-id"], r"^[0-9a-f]{32}$")

    def test_in_error_bodies(self):
        response = client().get("/missing", headers={"X-Request-ID": "client-supplied-1"})
        self.assertEqual(response.headers["x-request-id"], "client-supplied-1")
        self.assertEqual(response.json()["error"]["request_id"], "client-supplied-1")
        self.assertIsNone(request_context.current_request_id())       # never leaks out of the request


def app_with_test_routes(**settings):
    app = create_app(ApiSettings(**settings))

    def needs_int(n: int):
        return {"n": n}

    def explode():
        raise RuntimeError(f"boom {READ_TOKEN} redis://:secret-password-canary@host/0 /etc/secret-path-canary")

    def raises_api_error():
        raise ApiError("ambiguous_latest")

    def operate():
        return {"ok": True}
    app.add_api_route("/api/v1/test/int", needs_int, methods=["GET"])
    app.add_api_route("/api/v1/test/explode", explode, methods=["GET"])
    app.add_api_route("/api/v1/test/api-error", raises_api_error, methods=["GET"])
    app.add_api_route("/api/v1/test/operate", operate, methods=["POST"], dependencies=[Depends(auth.require_operate)])
    return app


class ErrorContractTests(unittest.TestCase):
    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertEqual(set(body["error"]), {"code", "message", "request_id"})
        self.assertEqual((body["error"]["code"], body["error"]["message"]), (code, ERRORS[code][1]))
        self.assertEqual(body["error"]["request_id"], response.headers["x-request-id"])
        return body

    def test_closed_codes(self):
        self.assertEqual(errors.CODES, ("invalid_request", "unauthorized", "forbidden", "not_found", "method_not_allowed",
                                        "ambiguous_latest", "conflict", "payload_too_large", "artifact_invalid",
                                        "dependency_unavailable", "internal"))
        with self.assertRaises(ValueError):
            ApiError("teapot")
        self.assertEqual({c: s for c, (s, _) in ERRORS.items()},
                         dict(invalid_request=400, unauthorized=401, forbidden=403, not_found=404, method_not_allowed=405,
                              ambiguous_latest=409, conflict=409, payload_too_large=413, artifact_invalid=500,
                              dependency_unavailable=503, internal=500))

    def test_framework_errors(self):
        c = TestClient(app_with_test_routes())
        self.assert_error(c.get("/no/such/route"), 404, "not_found")
        response = c.delete("/api/v1/version")
        self.assert_error(response, 405, "method_not_allowed")
        self.assertEqual(response.headers["allow"], "GET")
        body = self.assert_error(c.get("/api/v1/test/int?n=not-a-number-canary"), 400, "invalid_request")
        self.assertNotIn("canary", str(body))
        self.assert_error(c.get("/api/v1/test/int"), 400, "invalid_request")
        self.assert_error(c.get("/api/v1/test/api-error"), 409, "ambiguous_latest")

    def test_unexpected_exception_is_internal_and_sanitized(self):
        capture = LogCapture()
        logging.getLogger().addHandler(capture)
        try:
            c = TestClient(app_with_test_routes(), raise_server_exceptions=False)
            response = c.get("/api/v1/test/explode", headers={"X-Request-ID": "explode-request-1"})
        finally:
            logging.getLogger().removeHandler(capture)
        body = self.assert_error(response, 500, "internal")
        self.assertEqual(body["error"]["request_id"], "explode-request-1")
        for leak in ("boom", "canary", READ_TOKEN, "Traceback", "RuntimeError"):
            self.assertNotIn(leak, response.text)
        logged = "\n".join(capture.lines)
        self.assertIn("event=unhandled_exception", logged)
        self.assertIn("explode-request-1", logged)
        self.assertIn("/api/v1/test/explode", logged)
        for leak in ("boom", "canary", READ_TOKEN):
            self.assertNotIn(leak, logged)
        strict = TestClient(app_with_test_routes())                    # also contained when the client re-raises
        self.assert_error(strict.get("/api/v1/test/explode"), 500, "internal")


class AuthTests(unittest.TestCase):
    def test_loopback_open_reads(self):
        self.assertEqual(client().get("/api/v1/version").status_code, 200)

    def test_read_token(self):
        c = client(read_token=READ_TOKEN, operator_token=OPERATOR_TOKEN)
        for headers in ({}, {"Authorization": "Bearer wrong-token"}, {"Authorization": f"Basic {READ_TOKEN}"},
                        {"Authorization": READ_TOKEN}, {"Authorization": f"Bearer {READ_TOKEN} extra"},
                        {"Authorization": f"Bearer {OPERATOR_TOKEN}"}, {"Authorization": f"Bearer {READ_TOKEN[:-1]}"}):
            with self.subTest(headers=str(headers)[:40]):
                response = c.get("/api/v1/version", headers=headers)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (401, "unauthorized"))
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                self.assertNotIn(READ_TOKEN, response.text)
        self.assertEqual(c.get("/api/v1/version", headers={"Authorization": f"bearer {READ_TOKEN}"}).status_code, 200)
        doubled = c.get("/api/v1/version", headers=[("Authorization", f"Bearer {READ_TOKEN}"),
                                                    ("Authorization", f"Bearer {READ_TOKEN}")])
        self.assertEqual(doubled.status_code, 401)

    def test_constant_time_comparison(self):
        c = client(read_token=READ_TOKEN)
        with mock.patch.object(auth.hmac, "compare_digest", wraps=auth.hmac.compare_digest) as compare:
            c.get("/api/v1/version", headers={"Authorization": "Bearer guess"})
            c.get("/api/v1/version", headers={"Authorization": f"Bearer {READ_TOKEN}"})
        self.assertEqual(compare.call_count, 2)
        self.assertFalse(auth.token_matches("\udcff-invalid-surrogate", READ_TOKEN))

    def test_operate_scope(self):
        no_operator = TestClient(app_with_test_routes(read_token=READ_TOKEN))
        response = no_operator.post("/api/v1/test/operate", headers={"Authorization": f"Bearer {READ_TOKEN}"})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "forbidden"))
        c = TestClient(app_with_test_routes(read_token=READ_TOKEN, operator_token=OPERATOR_TOKEN))
        self.assertEqual(c.post("/api/v1/test/operate").status_code, 401)
        self.assertEqual(c.post("/api/v1/test/operate", headers={"Authorization": f"Bearer {READ_TOKEN}"}).status_code, 401)
        self.assertEqual(c.post("/api/v1/test/operate", headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"}).status_code, 200)

    def test_authorization_never_logged_or_echoed(self):
        capture = LogCapture()
        root = logging.getLogger()
        root.addHandler(capture)
        level = root.level
        root.setLevel(logging.DEBUG)
        try:
            c = TestClient(app_with_test_routes(read_token=READ_TOKEN), raise_server_exceptions=False)
            texts = [c.get(path, headers={"Authorization": f"Bearer {READ_TOKEN}"}).text
                     for path in ("/api/v1/version", "/api/v1/test/explode", "/nope", "/health/ready")]
            texts.append(c.get("/api/v1/version", headers={"Authorization": f"Bearer wrong-{READ_TOKEN}"}).text)
        finally:
            root.removeHandler(capture)
            root.setLevel(level)
        self.assertNotIn(READ_TOKEN, "\n".join(texts + capture.lines))


class DocsTests(unittest.TestCase):
    def test_enabled(self):
        c = client(read_token=READ_TOKEN, operator_token=OPERATOR_TOKEN)
        self.assertEqual(c.get("/docs").status_code, 200)
        spec = c.get("/openapi.json")
        self.assertEqual(spec.status_code, 200)
        paths = sorted(spec.json()["paths"])
        self.assertTrue({"/api/v1/version", "/health/live", "/health/ready"} <= set(paths))
        self.assertTrue(all(p.startswith(("/health/", "/api/v1/")) for p in paths))   # 13C adds read routes only
        self.assertEqual([p for p in paths if "deliver" in p], ["/api/v1/alerts/{artifact_id}/deliveries"])
        for secret in (READ_TOKEN, OPERATOR_TOKEN):
            self.assertNotIn(secret, spec.text)
        self.assertEqual(c.get("/redoc").status_code, 404)

    def test_disabled(self):
        c = client(docs_enabled=False)
        for path in ("/docs", "/openapi.json", "/redoc"):
            self.assertEqual(c.get(path).status_code, 404)

    def test_no_cors_by_default(self):
        response = client().options("/api/v1/version", headers={"Origin": "https://evil.example",
                                                                 "Access-Control-Request-Method": "GET"})
        self.assertNotIn("access-control-allow-origin", response.headers)
        response = client().get("/api/v1/version", headers={"Origin": "https://evil.example"})
        self.assertNotIn("access-control-allow-origin", response.headers)


class EntryPointTests(unittest.TestCase):
    def test_main(self):
        run, err = mock.Mock(), io.StringIO()
        self.assertEqual(api_main.main({"MIAS_API_READ_TOKEN": "too-short-canary"}, run=run, err=err), 2)
        run.assert_not_called()
        self.assertIn("MIAS_API_READ_TOKEN", err.getvalue())
        self.assertNotIn("too-short-canary", err.getvalue())
        self.assertEqual(api_main.main({"MIAS_API_PORT": "8181"}, run=run, err=io.StringIO()), 0)
        kwargs = run.call_args.kwargs
        self.assertEqual((kwargs["host"], kwargs["port"], kwargs["server_header"], kwargs["proxy_headers"]),
                         ("127.0.0.1", 8181, False, False))

    def test_module_import_does_not_serve(self):
        code = "import api.__main__, sys; print('uvicorn' in sys.modules)"
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "False", result.stderr[-300:])


class DomainNonMutationTests(unittest.TestCase):
    def test_version_reads_constants_only(self):
        from alert_engine import rules as alert_rules
        before = (alert_rules.ALERT_FORMAT_VERSION, alert_rules.RULES_VERSION)
        client().get("/api/v1/version")
        self.assertEqual((alert_rules.ALERT_FORMAT_VERSION, alert_rules.RULES_VERSION), before)
        self.assertIsInstance(meta.ANALYTICAL_FORMATS, tuple)

    def test_readiness_never_reads_files(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "secret-content.json"), "w") as handle:
                handle.write("file-content-canary")
            app = create_app(ApiSettings(artifact_root=root), refresh_interval=0)
            with mock.patch("builtins.open", side_effect=AssertionError("no file reads")):
                response = TestClient(app).get("/health/ready")       # no lifespan: the index is never built
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("file-content-canary", response.text)


if __name__ == "__main__":
    unittest.main()
