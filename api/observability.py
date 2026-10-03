"""Observability for mias-api (Phase 15): structured logs, OpenTelemetry traces and metrics. Auxiliary by design.

**Configuration** (process environment only, read at the process boundary by ``python -m api``):

| Variable | Default | Meaning |
|---|---|---|
| ``MIAS_OBSERVABILITY_ENABLED`` | ``false`` | build the OpenTelemetry SDK (traces/metrics over OTLP/HTTP) |
| ``MIAS_LOG_FORMAT`` | ``text`` | ``json``: one JSON object per log line on stderr, with request/trace correlation |
| ``OTEL_SERVICE_NAME`` | ``mias-api`` | resource ``service.name`` |
| ``OTEL_RESOURCE_ATTRIBUTES`` | unset | extra resource attributes (standard SDK parsing, e.g. ``deployment.environment=lab``) |
| ``OTEL_EXPORTER_OTLP_ENDPOINT`` | none | OTLP/HTTP base URL (``http(s)://host:port``, no credentials); required when enabled |
| ``OTEL_TRACES_EXPORTER`` / ``OTEL_METRICS_EXPORTER`` | ``otlp`` | ``otlp`` or ``none`` |
| ``OTEL_METRIC_EXPORT_INTERVAL`` | ``60000`` | metric export interval in ms (10000–300000) |

Invalid configuration fails startup (exit 2), like the other settings. At **runtime** telemetry is strictly auxiliary:
exporters run in background threads with a 5 s timeout, export failures are logged by the SDK and dropped, and nothing
here can fail a request, liveness or readiness. With observability disabled, the OpenTelemetry *API* no-op objects
are used and the SDK/exporter are never imported.

**What is recorded** (low cardinality; never headers, tokens, cookies, bodies, artifact payloads or artifact ids):
- server span per non-health request: ``http.request.method``, ``http.route`` (template), ``http.response.status_code``,
  ``mias.request_id``; W3C ``traceparent`` from the caller is honoured;
- ``mias.artifact.lookup`` spans: ``mias.artifact.kind``, ``mias.artifact.operation``, ``mias.artifact.result``;
- ``mias.artifact.index.refresh`` spans: ``mias.index.refresh.result``;
- metrics: ``http.server.request.duration`` (histogram, s; method, route, status code), ``mias.artifact.index.refreshes``
  (counter; result), ``mias.artifact.index.artifacts`` (gauge; kind), ``mias.artifact.index.healthy`` (gauge),
  ``mias.artifact.index.last_success_age`` (gauge, s).
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import re
import time

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from api.request_context import current_request_id
from api.settings import ApiConfigurationError

ENDPOINT = re.compile(r"https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?/?")
SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"}
SCOPE = "mias.api"
EXPORT_TIMEOUT_SECONDS = 5
LOG_FIELDS = ("http.request.method", "http.route", "http.response.status_code", "duration_ms", "event",
              "mias.artifact.kind", "mias.index.refresh.result")


@dataclass(frozen=True)
class ObservabilitySettings:
    enabled: bool = False
    log_json: bool = False
    service_name: str = "mias-api"
    endpoint: str = None
    traces: bool = True
    metrics: bool = True
    metric_interval_ms: int = 60000

    def __post_init__(self):
        if not SERVICE_NAME.fullmatch(self.service_name or ""):
            raise ApiConfigurationError("OTEL_SERVICE_NAME is invalid")
        if self.enabled and (self.traces or self.metrics):
            if not (isinstance(self.endpoint, str) and ENDPOINT.fullmatch(self.endpoint)):
                raise ApiConfigurationError("OTEL_EXPORTER_OTLP_ENDPOINT must be http(s)://host[:port] without "
                                            "credentials, path or query")
        if not (isinstance(self.metric_interval_ms, int) and 10000 <= self.metric_interval_ms <= 300000):
            raise ApiConfigurationError("OTEL_METRIC_EXPORT_INTERVAL must be 10000-300000 ms")


def _flag(environ, name, default):
    raw = (environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw not in ("true", "false"):
        raise ApiConfigurationError(f"{name} must be true or false")
    return raw == "true"


def _exporter(environ, name):
    raw = (environ.get(name) or "otlp").strip().lower()
    if raw not in ("otlp", "none"):
        raise ApiConfigurationError(f"{name} must be otlp or none")
    return raw == "otlp"


def load_observability_settings(environ):
    log_format = (environ.get("MIAS_LOG_FORMAT") or "text").strip().lower()
    if log_format not in ("text", "json"):
        raise ApiConfigurationError("MIAS_LOG_FORMAT must be text or json")
    raw_interval = (environ.get("OTEL_METRIC_EXPORT_INTERVAL") or "60000").strip()
    if not raw_interval.isdigit():
        raise ApiConfigurationError("OTEL_METRIC_EXPORT_INTERVAL must be 10000-300000 ms")
    return ObservabilitySettings(
        enabled=_flag(environ, "MIAS_OBSERVABILITY_ENABLED", False) and not _flag(environ, "OTEL_SDK_DISABLED", False),
        log_json=log_format == "json", service_name=(environ.get("OTEL_SERVICE_NAME") or "mias-api").strip(),
        endpoint=(environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip().rstrip("/") or None,
        traces=_exporter(environ, "OTEL_TRACES_EXPORTER"), metrics=_exporter(environ, "OTEL_METRICS_EXPORTER"),
        metric_interval_ms=int(raw_interval))


# --- structured logging -----------------------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """One JSON object per record. Only allow-listed fields; no headers, tokens, bodies or tracebacks."""

    def __init__(self, service_name, service_version):
        super().__init__()
        self.service_name, self.service_version = service_name, service_version

    def format(self, record):
        out = {"timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
               "level": record.levelname, "logger": record.name, "message": record.getMessage(),
               "service.name": self.service_name, "service.version": self.service_version}
        request_id = current_request_id()
        if request_id:
            out["request_id"] = request_id
        context = otel_trace.get_current_span().get_span_context()
        if context.is_valid:
            out["trace_id"] = format(context.trace_id, "032x")
            out["span_id"] = format(context.span_id, "016x")
        fields = getattr(record, "mias_fields", None) or {}
        for key in LOG_FIELDS:
            if key in fields:
                out[key] = fields[key]
        if record.exc_info and record.exc_info[0] is not None:
            out["error.type"] = record.exc_info[0].__name__          # never the message or traceback
        return json.dumps(out, separators=(",", ":"), default=str)


def configure_logging(settings, service_version, stream=None):
    """Process-level logging setup (called by ``python -m api``). JSON mode replaces uvicorn's access log with the
    application's own sanitized access records."""
    import sys
    handler = logging.StreamHandler(stream or sys.stderr)
    if settings.log_json:
        handler.setFormatter(JsonFormatter(settings.service_name, service_version))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers, logger.propagate = [], True
    access = logging.getLogger("uvicorn.access")
    access.handlers, access.propagate, access.disabled = [], False, settings.log_json
    return handler


# --- telemetry ----------------------------------------------------------------------------------------------------

class Telemetry:
    """Holds the tracer, meter and instruments. Disabled: OpenTelemetry API no-ops, nothing exported, no SDK import."""

    def __init__(self, settings=None, service_version="unknown", *, span_exporter=None, metric_reader=None):
        self.settings = settings or ObservabilitySettings()
        self.service_version = service_version
        # Test seams: in-memory exporter/reader instead of OTLP (production never passes these).
        self._span_exporter, self._metric_reader = span_exporter, metric_reader
        self._providers = []
        self.tracer = otel_trace.NoOpTracer()
        self.meter = otel_metrics.NoOpMeter(SCOPE)
        self._store = self._refresher = None
        self._propagator = TraceContextTextMapPropagator()
        self._last_refresh_result = None
        self.started = False
        self._instruments()

    @property
    def enabled(self):
        return self.settings.enabled

    def _instruments(self):
        self.request_duration = self.meter.create_histogram(
            "http.server.request.duration", unit="s", description="Duration of inbound HTTP requests")
        self.refreshes = self.meter.create_counter(
            "mias.artifact.index.refreshes", unit="{refresh}", description="Artifact index refresh attempts")

    def start(self, artifact_store=None, refresher=None):
        """Build the SDK pipelines (enabled only) and register the index gauges. Never raises."""
        self._store, self._refresher = artifact_store, refresher
        if not self.enabled or self.started:
            return
        try:
            from opentelemetry.sdk.resources import Resource
            resource = Resource.create({"service.name": self.settings.service_name,
                                        "service.version": self.service_version, "service.namespace": "mias"})
            if self.settings.traces:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
                provider = TracerProvider(resource=resource)
                if self._span_exporter is not None:
                    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
                    provider.add_span_processor(SimpleSpanProcessor(self._span_exporter))
                else:
                    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                        endpoint=f"{self.settings.endpoint}/v1/traces", timeout=EXPORT_TIMEOUT_SECONDS)))
                self._providers.append(provider)
                self.tracer = provider.get_tracer(SCOPE)
            if self.settings.metrics:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
                from opentelemetry.sdk.metrics import MeterProvider
                from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
                reader = self._metric_reader or PeriodicExportingMetricReader(OTLPMetricExporter(
                    endpoint=f"{self.settings.endpoint}/v1/metrics", timeout=EXPORT_TIMEOUT_SECONDS),
                    export_interval_millis=self.settings.metric_interval_ms)
                provider = MeterProvider(resource=resource, metric_readers=[reader])
                self._providers.append(provider)
                self.meter = provider.get_meter(SCOPE)
                self._instruments()
                self.meter.create_observable_gauge("mias.artifact.index.artifacts", [self._artifact_counts],
                                                   unit="{artifact}", description="Artifacts in the live index")
                self.meter.create_observable_gauge("mias.artifact.index.healthy", [self._healthy],
                                                   description="1 if the index is built and the last refresh succeeded")
                self.meter.create_observable_gauge("mias.artifact.index.last_success_age", [self._last_success_age],
                                                   unit="s", description="Seconds since the last successful refresh")
            self.started = True
        except Exception as error:                     # telemetry is auxiliary: keep serving without it
            logging.getLogger("mias.api").warning("event=telemetry_disabled reason=%s", type(error).__name__)
            self.tracer, self.meter = otel_trace.NoOpTracer(), otel_metrics.NoOpMeter(SCOPE)
            self._instruments()

    def shutdown(self):
        for provider in self._providers:
            try:
                provider.shutdown()
            except Exception:
                pass
        self._providers = []

    # gauge callbacks (run in the metric reader thread; never raise)
    def _artifact_counts(self, options):
        from opentelemetry.metrics import Observation
        try:
            counts = self._store.snapshot().counts() if self._store is not None else {}
        except Exception:
            return []
        return [Observation(count, {"mias.artifact.kind": kind}) for kind, count in counts.items()]

    def _healthy(self, options):
        from opentelemetry.metrics import Observation
        return [Observation(1 if (self._store is not None and self._store.healthy) else 0)]

    def _last_success_age(self, options):
        from opentelemetry.metrics import Observation
        last = getattr(self._refresher, "last_success", None)
        return [] if last is None else [Observation(max(0.0, time.monotonic() - last))]

    # instrumentation helpers (never raise into the caller)
    def server_span(self, method, headers):
        try:
            carrier = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers
                       if k.lower() in (b"traceparent", b"tracestate")}
            context = self._propagator.extract(carrier) if carrier else None
            return self.tracer.start_span(method if method in HTTP_METHODS else "HTTP", kind=SpanKind.SERVER,
                                          context=context)
        except Exception:
            return otel_trace.INVALID_SPAN

    def finish_request(self, span, method, route, status, duration):
        try:
            method = method if method in HTTP_METHODS else "_OTHER"
            attributes = {"http.request.method": method, "http.route": route, "http.response.status_code": status}
            self.request_duration.record(duration, attributes)
            if span is not otel_trace.INVALID_SPAN:
                span.update_name(f"{method} {route}")
                for key, value in attributes.items():
                    span.set_attribute(key, value)
                request_id = current_request_id()
                if request_id:
                    span.set_attribute("mias.request_id", request_id)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                span.end()
        except Exception:
            pass

    @contextmanager
    def artifact_span(self, kind, operation):
        """A child span around an artifact read; the result is the closed API outcome, never ids or payloads."""
        holder = {"result": "ok"}
        try:
            span = self.tracer.start_span("mias.artifact.lookup", attributes={
                "mias.artifact.kind": kind, "mias.artifact.operation": operation})
        except Exception:
            span = otel_trace.INVALID_SPAN
        with otel_trace.use_span(span, end_on_exit=False):
            try:
                yield holder
            except Exception as error:
                holder["result"] = getattr(error, "code", "error")
                raise
            finally:
                try:
                    span.set_attribute("mias.artifact.result", holder["result"])
                    span.end()
                except Exception:
                    pass

    def record_refresh(self, ok, started_ns):
        """Counter + span for every refresh; a log line only on failure or when the result changes (no 30 s noise)."""
        try:
            result = "success" if ok else "failure"
            self.refreshes.add(1, {"mias.index.refresh.result": result})
            span = self.tracer.start_span("mias.artifact.index.refresh", start_time=started_ns,
                                          attributes={"mias.index.refresh.result": result})
            span.end()
            if not ok or result != self._last_refresh_result:
                logging.getLogger("mias.api").log(
                    logging.INFO if ok else logging.WARNING, "artifact index refresh %s", result,
                    extra={"mias_fields": {"event": "artifact_index_refresh", "mias.index.refresh.result": result}})
            self._last_refresh_result = result
        except Exception:
            pass
