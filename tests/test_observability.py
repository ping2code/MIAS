"""Phase 15: mias-api observability (structured logs, OpenTelemetry traces and metrics).

In-process only: in-memory span exporter and metric reader, TestClient, temporary artifact store. No network, except
one test that points the real OTLP exporter at a closed local port to prove export failure is harmless.
"""
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from api import observability as obs
from api.app import create_app
from api.observability import JsonFormatter, ObservabilitySettings, Telemetry, load_observability_settings
from api.settings import ApiConfigurationError, ApiSettings
from artifact_store import store
from tests.test_artifact_store import raw, samples

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "obs-test-read-token-0123456789abcdef-XYZ"
ENABLED = ObservabilitySettings(enabled=True, log_json=True, endpoint="http://127.0.0.1:4318")
ALLOWED_SPAN_ATTRIBUTES = {"http.request.method", "http.route", "http.response.status_code", "mias.request_id",
                           "mias.artifact.kind", "mias.artifact.operation", "mias.artifact.result",
                           "mias.index.refresh.result"}


class Capture(logging.Handler):
    def __init__(self, formatter):
        super().__init__(logging.DEBUG)
        self.setFormatter(formatter)
        self.lines = []

    def emit(self, record):
        if record.name.split(".")[0] in ("httpx", "httpcore", "httpx2", "httpcore2"):     # the test client's own request log, not the API
            return
        self.lines.append(self.format(record))


class ObsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "store")
        os.mkdir(self.root)
        self.objects = samples()
        for obj in self.objects["market-intelligence"]:
            store.publish(self.root, "market-intelligence", raw(obj))
        self.spans, self.reader = InMemorySpanExporter(), InMemoryMetricReader()
        self.capture = Capture(JsonFormatter("mias-api", "test-build"))
        root = logging.getLogger()
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(setattr, root, "level", root.level)
        root.addHandler(self.capture)
        root.setLevel(logging.INFO)

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, enabled=True, **settings):
        telemetry = Telemetry(ENABLED if enabled else ObservabilitySettings(), "test-build",
                              span_exporter=self.spans, metric_reader=self.reader) if enabled else Telemetry()
        settings = dict(dict(artifact_root=self.root, read_token=TOKEN), **settings)
        app = create_app(ApiSettings(**settings), refresh_interval=0, telemetry=telemetry)
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def logs(self):
        return [json.loads(line) for line in self.capture.lines]

    def metric_points(self):
        out = {}
        data = self.reader.get_metrics_data()
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    out[metric.name] = [(dict(p.attributes), p) for p in metric.data.data_points]
        return out


class SettingsTests(unittest.TestCase):
    def test_defaults_and_parsing(self):
        s = load_observability_settings({})
        self.assertEqual((s.enabled, s.log_json, s.service_name, s.endpoint, s.traces, s.metrics),
                         (False, False, "mias-api", None, True, True))
        s = load_observability_settings({"MIAS_OBSERVABILITY_ENABLED": "true", "MIAS_LOG_FORMAT": "json",
                                         "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector.mias.svc:4318/",
                                         "OTEL_TRACES_EXPORTER": "none", "OTEL_METRIC_EXPORT_INTERVAL": "30000"})
        self.assertEqual((s.enabled, s.log_json, s.endpoint, s.traces, s.metrics, s.metric_interval_ms),
                         (True, True, "http://otel-collector.mias.svc:4318", False, True, 30000))
        self.assertFalse(load_observability_settings({"MIAS_OBSERVABILITY_ENABLED": "true",
                                                      "OTEL_SDK_DISABLED": "true"}).enabled)

    def test_invalid_values_fail_without_echo(self):
        cases = [{"MIAS_OBSERVABILITY_ENABLED": "true"},                                      # no endpoint
                 {"MIAS_OBSERVABILITY_ENABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://user:pw-canary@h:4318"},
                 {"MIAS_OBSERVABILITY_ENABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://h:4318/v1?k=canary"},
                 {"MIAS_OBSERVABILITY_ENABLED": "yes"}, {"MIAS_LOG_FORMAT": "xml"},
                 {"OTEL_TRACES_EXPORTER": "jaeger"}, {"OTEL_METRIC_EXPORT_INTERVAL": "5"},
                 {"OTEL_SERVICE_NAME": "bad name canary"}]
        for environ in cases:
            with self.subTest(environ=list(environ)):
                with self.assertRaises(ApiConfigurationError) as caught:
                    load_observability_settings(environ)
                self.assertNotIn("canary", str(caught.exception))

    def test_entry_point_rejects_bad_observability_config(self):
        from api import __main__ as api_main
        from unittest import mock
        run, err = mock.Mock(), io.StringIO()
        self.assertEqual(api_main.main({"MIAS_OBSERVABILITY_ENABLED": "true"}, run=run, err=err), 2)
        run.assert_not_called()
        self.assertEqual(api_main.main({"MIAS_LOG_FORMAT": "json"}, run=run, err=io.StringIO()), 0)
        kwargs = run.call_args.kwargs
        self.assertEqual((kwargs["access_log"], kwargs["log_config"], kwargs["server_header"]), (False, None, False))


class NativeFastApiTelemetryTests(ObsCase):
    def test_fastapi_native_telemetry_is_off_and_no_global_providers(self):
        """FastAPI 0.142 auto-configures global OTel providers from OTEL_*; MIAS disables that explicitly."""
        code = ("import os, sys, tempfile\n"
                "os.environ.update(OTEL_EXPORTER_OTLP_ENDPOINT='http://127.0.0.1:1', OTEL_TRACES_EXPORTER='otlp',"
                " OTEL_METRICS_EXPORTER='otlp')\n"
                "from fastapi.testclient import TestClient\nfrom api.app import create_app\n"
                "from api.settings import ApiSettings\nfrom opentelemetry import trace, metrics\n"
                "app = create_app(ApiSettings(artifact_root=tempfile.mkdtemp()), refresh_interval=0)\n"
                "with TestClient(app) as c:\n    c.get('/health/live')\n"
                "print(type(trace.get_tracer_provider()).__name__, type(metrics.get_meter_provider()).__name__,"
                " app._telemetry['auto_configure'], app._telemetry['tracing'], app._telemetry['metrics'])")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                                env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.stdout.strip().splitlines()[-1],
                         "ProxyTracerProvider _ProxyMeterProvider False False False", result.stderr[-400:])


class DisabledTests(ObsCase):
    def test_disabled_loads_no_sdk_or_exporter(self):
        code = ("import sys\nfrom fastapi.testclient import TestClient\nfrom api.app import create_app\n"
                "from api.settings import ApiSettings\nimport tempfile\n"
                "d = tempfile.mkdtemp()\n"
                "with TestClient(create_app(ApiSettings(artifact_root=d), refresh_interval=0)) as c:\n"
                "    c.get('/health/live'); c.get('/api/v1/version')\n"
                "print(sorted(n for n in sys.modules if n.startswith(('opentelemetry.sdk', 'opentelemetry.exporter',"
                " 'urllib3', 'google.protobuf', 'requests'))))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                                env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.stdout.strip().splitlines()[-1], "[]", result.stderr[-400:])

    def test_disabled_behaviour_unchanged(self):
        c = self.client(enabled=False)
        self.assertEqual(c.get("/health/live").status_code, 200)
        self.assertEqual(c.get("/health/ready").status_code, 200)
        self.assertEqual(c.get("/api/v1/version").status_code, 401)
        self.assertEqual(c.get("/api/v1/version", headers={"Authorization": f"Bearer {TOKEN}"}).status_code, 200)
        self.assertEqual(self.spans.get_finished_spans(), ())


class StructuredLoggingTests(ObsCase):
    def test_access_records_are_structured_and_correlated(self):
        c = self.client()
        response = c.get("/api/v1/market-intelligence?symbol=META", headers={"Authorization": f"Bearer {TOKEN}",
                                                                              "X-Request-ID": "obs-request-0001",
                                                                              "Cookie": "session=cookie-canary"})
        self.assertEqual(response.status_code, 200)
        access = [r for r in self.logs() if r.get("event") == "http_request"]
        self.assertEqual(len(access), 1)
        record = access[0]
        self.assertEqual({k: record[k] for k in ("level", "logger", "service.name", "service.version", "request_id",
                                                  "http.request.method", "http.route", "http.response.status_code")},
                         {"level": "INFO", "logger": "mias.api.access", "service.name": "mias-api",
                          "service.version": "test-build", "request_id": "obs-request-0001",
                          "http.request.method": "GET", "http.route": "/api/v1/market-intelligence",
                          "http.response.status_code": 200})
        self.assertRegex(record["trace_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(record["span_id"], r"^[0-9a-f]{16}$")
        self.assertIsInstance(record["duration_ms"], float)
        self.assertRegex(record["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00$")
        server = [s for s in self.spans.get_finished_spans() if s.kind.name == "SERVER"][0]
        self.assertEqual(record["trace_id"], format(server.context.trace_id, "032x"))   # log <-> trace correlation

    def test_no_sensitive_values_in_logs(self):
        c = self.client()
        for path in ("/api/v1/version", "/api/v1/market-intelligence?symbol=META&cursor=query-canary",
                     "/api/v1/market-intelligence/sha256:" + "0" * 64, "/nope"):
            c.get(path, headers={"Authorization": f"Bearer {TOKEN}", "Cookie": "c=cookie-canary"})
        c.get("/api/v1/version", headers={"Authorization": "Bearer wrong-canary"})
        text = "\n".join(self.capture.lines)
        for secret in (TOKEN, "Bearer", "Authorization", "cookie-canary", "wrong-canary", "query-canary",
                       "sha256:" + "0" * 64):
            self.assertNotIn(secret, text)
        allowed = {"timestamp", "level", "logger", "message", "service.name", "service.version", "request_id",
                   "trace_id", "span_id", "error.type"} | set(obs.LOG_FIELDS)
        for record in self.logs():
            self.assertLessEqual(set(record), allowed)

    def test_health_success_not_logged_failure_logged(self):
        c = self.client()
        for _ in range(5):
            c.get("/health/live")
            c.get("/health/ready")
        self.assertFalse([r for r in self.logs() if r.get("http.route", "").startswith("/health")])
        bad = self.client(artifact_root=os.path.join(self.tmp.name, "missing"))
        self.assertEqual(bad.get("/health/ready").status_code, 503)
        self.assertEqual([r["http.response.status_code"] for r in self.logs()
                          if r.get("http.route") == "/health/ready"], [503])

    def test_unexpected_exception_logs_type_only(self):
        from unittest import mock
        c = self.client()
        with mock.patch("api.routes.artifacts._view", side_effect=RuntimeError("boom-canary " + TOKEN)):
            response = c.get(f"/api/v1/market-intelligence/{self.objects['market-intelligence'][0]['intelligence_id']}",
                             headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(response.status_code, 500)
        text = "\n".join(self.capture.lines)
        self.assertIn("unhandled_exception", text)
        self.assertNotIn("boom-canary", text)
        self.assertNotIn(TOKEN, text)


class TracingTests(ObsCase):
    def test_server_and_artifact_spans(self):
        c = self.client()
        mi = self.objects["market-intelligence"][0]["intelligence_id"]
        auth = {"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "trace-req-0001"}
        c.get(f"/api/v1/market-intelligence/{mi}/canonical", headers=auth)
        c.get("/api/v1/market-intelligence/sha256:" + "1" * 64, headers=auth)
        spans = self.spans.get_finished_spans()
        servers = [s for s in spans if s.kind.name == "SERVER"]
        self.assertEqual([s.name for s in servers], ["GET /api/v1/market-intelligence/{artifact_id}/canonical",
                                                     "GET /api/v1/market-intelligence/{artifact_id}"])
        self.assertEqual(dict(servers[0].attributes), {
            "http.request.method": "GET", "http.route": "/api/v1/market-intelligence/{artifact_id}/canonical",
            "http.response.status_code": 200, "mias.request_id": "trace-req-0001"})
        lookups = [s for s in spans if s.name == "mias.artifact.lookup"]
        self.assertEqual([dict(s.attributes) for s in lookups], [
            {"mias.artifact.kind": "market-intelligence", "mias.artifact.operation": "canonical",
             "mias.artifact.result": "ok"},
            {"mias.artifact.kind": "market-intelligence", "mias.artifact.operation": "get",
             "mias.artifact.result": "not_found"}])
        self.assertEqual(lookups[0].parent.span_id, servers[0].context.span_id)   # child of the request span
        for span in spans:
            self.assertLessEqual(set(span.attributes), ALLOWED_SPAN_ATTRIBUTES)
            text = json.dumps(dict(span.attributes)) + span.name
            for secret in (TOKEN, "Bearer", mi, "sha256:" + "1" * 64):
                self.assertNotIn(secret, text)

    def test_health_has_no_spans_and_refresh_span_exists(self):
        c = self.client()
        c.get("/health/live")
        c.get("/health/ready")
        names = [s.name for s in self.spans.get_finished_spans()]
        self.assertFalse([n for n in names if "health" in n])
        refresh = [s for s in self.spans.get_finished_spans() if s.name == "mias.artifact.index.refresh"]
        self.assertEqual(dict(refresh[0].attributes), {"mias.index.refresh.result": "success"})

    def test_incoming_traceparent_is_continued(self):
        c = self.client()
        trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        c.get("/api/v1/version", headers={"Authorization": f"Bearer {TOKEN}",
                                          "traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"})
        server = [s for s in self.spans.get_finished_spans() if s.kind.name == "SERVER"][-1]
        self.assertEqual(format(server.context.trace_id, "032x"), trace_id)

    def test_server_error_marks_span(self):
        from unittest import mock
        c = self.client()
        with mock.patch("api.routes.artifacts._view", side_effect=RuntimeError("x")):
            c.get(f"/api/v1/market-intelligence/{self.objects['market-intelligence'][0]['intelligence_id']}",
                  headers={"Authorization": f"Bearer {TOKEN}"})
        server = [s for s in self.spans.get_finished_spans() if s.kind.name == "SERVER"][-1]
        self.assertEqual((server.attributes["http.response.status_code"], server.status.status_code.name),
                         (500, "ERROR"))


class MetricsTests(ObsCase):
    def test_request_duration_and_index_metrics(self):
        c = self.client()
        c.get("/health/live")
        c.get("/api/v1/version")
        c.get("/api/v1/version", headers={"Authorization": f"Bearer {TOKEN}"})
        c.get("/api/v1/market-intelligence/latest?symbol=META", headers={"Authorization": f"Bearer {TOKEN}"})
        points = self.metric_points()
        durations = {(a["http.route"], a["http.response.status_code"]): p.count
                     for a, p in points["http.server.request.duration"]}
        self.assertEqual(durations, {("/health/live", 200): 1, ("/api/v1/version", 401): 1,
                                     ("/api/v1/version", 200): 1, ("/api/v1/market-intelligence/latest", 200): 1})
        for attributes, _ in points["http.server.request.duration"]:
            self.assertEqual(set(attributes), {"http.request.method", "http.route", "http.response.status_code"})
        self.assertEqual([(a, p.value) for a, p in points["mias.artifact.index.refreshes"]],
                         [({"mias.index.refresh.result": "success"}, 1)])
        counts = {a["mias.artifact.kind"]: p.value for a, p in points["mias.artifact.index.artifacts"]}
        self.assertEqual(counts["market-intelligence"], len(self.objects["market-intelligence"]))
        self.assertEqual(set(counts), {"market-intelligence", "options-intelligence", "trade-setup",
                                       "invalidation-check", "alert"})
        self.assertEqual([p.value for _, p in points["mias.artifact.index.healthy"]], [1])
        self.assertLess([p.value for _, p in points["mias.artifact.index.last_success_age"]][0], 60)

    def test_cardinality_is_bounded(self):
        c = self.client()
        for i in range(20):
            c.get(f"/api/v1/market-intelligence/sha256:{i:064x}", headers={"Authorization": f"Bearer {TOKEN}"})
            c.get(f"/random-{i}")
            c.request("BREW", "/api/v1/version")
        points = self.metric_points()["http.server.request.duration"]
        routes = {a["http.route"] for a, _ in points}
        self.assertEqual(routes, {"/api/v1/market-intelligence/{artifact_id}", "unmatched", "/api/v1/version"})
        self.assertLessEqual({a["http.request.method"] for a, _ in points}, {"GET", "_OTHER"})


class FailureTests(ObsCase):
    def test_exporter_failure_never_breaks_the_api(self):
        """Real OTLP/HTTP exporters pointed at a closed local port: requests, liveness and readiness still succeed."""
        telemetry = Telemetry(ObservabilitySettings(enabled=True, endpoint="http://127.0.0.1:1",
                                                    metric_interval_ms=10000), "test-build")
        app = create_app(ApiSettings(artifact_root=self.root, read_token=TOKEN), refresh_interval=0,
                         telemetry=telemetry)
        started = time.monotonic()
        with TestClient(app) as c:
            for _ in range(5):
                self.assertEqual(c.get("/health/live").status_code, 200)
                self.assertEqual(c.get("/health/ready").status_code, 200)
                self.assertEqual(c.get("/api/v1/version", headers={"Authorization": f"Bearer {TOKEN}"}).status_code,
                                 200)
        self.assertTrue(telemetry.started)
        self.assertLess(time.monotonic() - started, 60)      # shutdown with an unreachable collector is bounded

    def test_telemetry_internal_errors_are_contained(self):
        from unittest import mock
        telemetry = Telemetry(ENABLED, "test-build", span_exporter=self.spans, metric_reader=self.reader)
        app = create_app(ApiSettings(artifact_root=self.root, read_token=TOKEN), refresh_interval=0,
                         telemetry=telemetry)
        with TestClient(app) as c:
            with mock.patch.object(telemetry.request_duration, "record", side_effect=RuntimeError("metrics down")):
                self.assertEqual(c.get("/api/v1/version", headers={"Authorization": f"Bearer {TOKEN}"}).status_code,
                                 200)
            with mock.patch.object(telemetry, "tracer") as tracer:
                tracer.start_span.side_effect = RuntimeError("tracer down")
                self.assertEqual(c.get("/api/v1/market-intelligence?symbol=META",
                                       headers={"Authorization": f"Bearer {TOKEN}"}).status_code, 200)

    def test_sdk_setup_failure_falls_back_to_noop(self):
        from unittest import mock
        telemetry = Telemetry(ENABLED, "test-build")
        with mock.patch("opentelemetry.sdk.resources.Resource.create", side_effect=RuntimeError("bad")):
            telemetry.start()
        self.assertFalse(telemetry.started)
        app = create_app(ApiSettings(artifact_root=self.root, read_token=TOKEN), refresh_interval=0,
                         telemetry=telemetry)
        with TestClient(app) as c:
            self.assertEqual(c.get("/health/ready").status_code, 200)


if __name__ == "__main__":
    unittest.main()
