# Phase 15: Observability (OpenTelemetry)

mias-api now emits **structured JSON logs**, **OpenTelemetry traces** and **OpenTelemetry metrics**. They go to an
in-namespace OpenTelemetry Collector, whose metrics OpenShift **User Workload Monitoring** scrapes.

Application behaviour, scientific logic, artifact semantics and the Phase 14 security model are unchanged. Telemetry
is auxiliary: it can't fail a request, liveness or readiness.

## Cluster capabilities found (read-only)

| Component | Present? |
|---|---|
| Cluster monitoring (Prometheus, Thanos querier, Alertmanager) | yes |
| **User Workload Monitoring** | **enabled** (`enableUserWorkload: true`; `prometheus-user-workload` ×2, Thanos ruler) |
| ServiceMonitor / PodMonitor / PrometheusRule CRDs | yes |
| OpenTelemetry Operator | **no** (the only CSV installed is `packageserver`) |
| Tempo / any trace backend | **no** |
| OpenShift Logging / Loki | **no** |

## Architecture

```
mias-api ──OTLP/HTTP :4318──▶ otel-collector (namespace mias)
                                 ├─ metrics ─▶ Prometheus exporter :8889 ◀── ServiceMonitor ◀── UWM Prometheus
                                 └─ traces  ─▶ debug exporter (verbosity basic, sampled): no trace backend yet
mias-api stdout ──▶ JSON logs (container logs; no logging stack installed)
```

- **Vendor-neutral:** the API speaks OTLP only; the collector decides where data goes.
- **Explicit instrumentation:** manual OpenTelemetry instrumentation sits in the existing request-id middleware and
  the routes. The contrib FastAPI/ASGI instrumentation isn't used, which means fewer dependencies and full control
  over attributes.
- **FastAPI's native telemetry is disabled.** FastAPI 0.142 auto-configures *global* OpenTelemetry providers from
  `OTEL_*` and instruments requests itself. That produced duplicate request metrics, under
  `job="unknown_service:python"`, and attributes MIAS doesn't audit. `create_app` turns it off, and a test guards
  that.
- **Lazy loading:** the SDK and exporter load only when `MIAS_OBSERVABILITY_ENABLED=true`. Importing `api` stays
  inert.

## Configuration

**API**, set in the `mias-api-config` ConfigMap by the chart:

| Variable | Value |
|---|---|
| `MIAS_OBSERVABILITY_ENABLED` | `true` |
| `MIAS_LOG_FORMAT` | `json` |
| `OTEL_SERVICE_NAME` | `mias-api` |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=lab,k8s.namespace.name=mias` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector.mias.svc:4318` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` |
| `OTEL_TRACES_EXPORTER` / `OTEL_METRICS_EXPORTER` | `otlp` |
| `OTEL_LOGS_EXPORTER` | `none` |
| `OTEL_METRIC_EXPORT_INTERVAL` | `60000` |

These are all non-secret, and the collector needs no authentication. An invalid configuration exits 2 at startup;
for example, an endpoint with credentials, a path or a query is rejected.

**Chart values:** `observability.{enabled, serviceName, environment, logging.json, traces.enabled, metrics.enabled,
metrics.exportIntervalMs, otlp.protocol, otlp.endpoint, collector.*}`. With `observability.enabled=false` the chart
renders exactly the Phase 14 objects.

## Structured logs

There's one JSON object per line:
- **Fields:** `timestamp` (UTC, ms), `level`, `logger`, `message`, `service.name`, `service.version` (the build id),
  `request_id`, `trace_id`, `span_id`, and for access records `event=http_request`, `http.request.method`,
  `http.route` (the template), `http.response.status_code` and `duration_ms`.
- **Allow-listed only:** nothing else is emitted. There are no headers, Authorization, cookies, query strings, bodies,
  artifact payloads or artifact ids, and exceptions log only `error.type`.
- **uvicorn:** its own access log is replaced by the sanitized one, and its startup lines are JSON too.
- **Noise control:** `/health/*` success isn't logged (probe noise); failures are. Index refresh logs only on failure
  or when the result changes.

## Request id and trace id

- **Request id:** the existing `X-Request-ID` behaviour is unchanged. Each request gets a server span; its `trace_id`
  and `span_id` and the `request_id` appear on the same log line, and `mias.request_id` is a span attribute.
- **Incoming trace context:** a caller's W3C `traceparent` is honoured.

## Traces

| Span | Attributes |
|---|---|
| Server span `GET <route template>`, one per non-health request | `http.request.method`, `http.route`, `http.response.status_code`, `mias.request_id`; status ERROR on 5xx |
| `mias.artifact.lookup` (child) | `mias.artifact.kind`, `mias.artifact.operation` (history, latest, get, canonical), `mias.artifact.result` (`ok` or the closed error code) |
| `mias.artifact.index.refresh` | `mias.index.refresh.result` |

Resource attributes: `service.name`, `service.version`, `service.namespace`, `deployment.environment`,
`k8s.namespace.name`, plus the SDK identifiers. There are no symbols, ids, headers or payloads.

## Metrics (low cardinality)

| Metric (Prometheus name) | Type | Labels |
|---|---|---|
| `http.server.request.duration` (`http_server_request_duration_seconds_*`) | histogram, s | `http_request_method` (known methods or `_OTHER`), `http_route` (template or `unmatched`), `http_response_status_code` |
| `mias.artifact.index.refreshes` (`mias_artifact_index_refreshes_total`) | counter | `mias_index_refresh_result` (success, failure) |
| `mias.artifact.index.artifacts` | gauge | `mias_artifact_kind` (5 kinds) |
| `mias.artifact.index.healthy` | gauge | none |
| `mias.artifact.index.last_success_age` (`…_seconds`) | gauge, s | none |

The collector's Prometheus exporter adds `job="mias/mias-api"` and `instance`. No artifact id, request id, trace id,
token or timestamp is ever a label.

## Collector

- **Image:** `otel/opentelemetry-collector-contrib` **0.161.0**. Docker Hub index `sha256:b5cf9836…` was mirrored to
  `mias/otel-collector-contrib:0.161.0` (internal digest `sha256:2d756157…`, amd64) and pinned by digest.
- **Workload:** one replica, Recreate. It runs under restricted-v2 with an arbitrary UID, read-only root, all
  capabilities dropped, no privilege escalation and seccomp RuntimeDefault.
- **Identity:** ServiceAccount `otel-collector` with no token and no RBAC.
- **Resources:** requests 20m/64Mi, limits 300m/256Mi, `GOMEMLIMIT=200MiB`, `memory_limiter` at 80%.
- **Pipeline:** OTLP/HTTP receiver only (no gRPC) → `memory_limiter` → `batch` → Prometheus exporter for metrics,
  and a `debug` exporter for traces (verbosity `basic`, sampling 2 then 1/500). The collector's own metrics are off.
  The `health_check` extension serves the probes.
- **Service:** `otel-collector`, ClusterIP: `otlp-http` 4318 and `metrics` 8889. There's no Route.
- **ServiceMonitor:** `otel-collector`, port `metrics`, every 30 s, scraped by UWM.

## NetworkPolicy changes

`mias-default-deny` and `mias-api-allow-router` are unchanged. Added:
- **`mias-api-egress-telemetry`:** the API may reach only `otel-collector` pods on TCP 4318, plus cluster DNS
  (`openshift-dns` pods `dns.operator.openshift.io/daemonset-dns=default`, UDP/TCP 5353) to resolve the Service name.
- **`otel-collector-ingress`:** TCP 4318 only from `mias-api` pods, and TCP 8889 only from
  `openshift-user-workload-monitoring` pods labelled `app.kubernetes.io/name=prometheus`. The collector has no
  egress, under the default deny.

There are no `ipBlock` or `0.0.0.0/0` rules. The publisher is untouched: no egress at all.

## Validation (observed, lab)

| Check | Result |
|---|---|
| Logs | All API lines are JSON. `phase15-live-0001` correlates to its `trace_id`/`span_id`. No health-success records. |
| Metrics | Collector `/metrics`: request histograms by route and status (including 401 and 404), artifacts per kind, healthy=1, refresh counter, last-success age. **UWM**: `up{namespace="mias"}=1`; Thanos returns `http_server_request_duration_seconds_count{namespace="mias"}` and `mias_artifact_index_healthy=1`. |
| Traces | The collector received spans (sampled `Traces` summaries). A temporary `detailed` audit (revisions 14 → 15) saw only the allow-listed attributes. |
| Secret audit | The token value and `Bearer`, `Authorization`, cookie, query, `password` and `api_key` canaries: **0** in API logs, collector logs (including detailed spans), live ConfigMaps and image history/inspect. The Helm release has only structural `existingSecret`/`secretKeyRef` names. The git diff has only placeholder tokens in tests. |
| Collector outage | Collector scaled to 0 for more than 75 s under traffic: **69/69 requests returned 200**, readiness true, 0 API restarts. The SDK logged bounded "Transient error" retries (about 24 lines/min while it was down). Restored to 1. |
| Unreachable endpoint (Podman) | live, ready and version all 200; clean SIGTERM; all logs JSON |
| NetworkPolicy | API → collector:4318 open. API → collector:8889, kube API, DNS metrics port, registry, ingress VIP and internet: blocked. An unlabelled pod in `mias` is denied from the collector (4318 and 8889) and from the API. Publisher → collector, DNS and internet: blocked. |
| Overhead | Image +8.6 MB (194.6 → 203.2 MB of layers; venv 22 → 32 MB). API idle 5m CPU, memory 43 → 50 MiB. Collector 4m / 36 MiB. Route latency is within noise: health median 74 → 70 ms, canonical 43 → 48 ms. |

## Image and Helm upgrade procedure

| | Value |
|---|---|
| API image | `mias-api:259236684a74`, **`sha256:634c5372e95b6d2fc1a5e194d3ea841e60f6ae336fbc90f65f54f3e46d32b196`** |
| Built from | git `259236684a745d9e0f13fe4ff12208da9f755ae2` (OCI revision label) |
| Previous digest | `sha256:f915fb6c…`, still in the ImageStream |
| Chart | `mias` 0.2.0 |

Build and push followed the 14B/14C procedure: Podman, a temporary `oc port-forward` to the internal registry, and an
auth file from `oc whoami -t` piped to stdin and deleted afterwards. The deployment:

```bash
helm upgrade mias deploy/helm/mias -n mias --reset-values --force-conflicts --wait
```

`--force-conflicts` was required once, because the image field was still co-owned by the original kubectl apply.
Later default upgrades used `--reset-values` only.

Release history: **13** (Phase 15 deploy), **14** (temporary detailed trace audit), **15** (defaults, current).

## Gaps and limitations

- **No trace backend.** Traces stop at a low-verbosity debug exporter. The next step is to approve a backend, such as
  the Tempo Operator plus a TempoStack, or an external OTLP endpoint, then swap the exporter and add a narrow egress
  rule for the collector.
- **No logging stack.** Logs are container stdout; JSON is ready for Loki or OpenShift Logging.
- **Collector-down noise:** about 24 warning lines per minute while the collector is unreachable.
- **Single replicas:** a collector restart drops in-flight telemetry. The API is unaffected.
- **Unchanged lab limitations:** self-signed ingress certificate, a hosts-file DNS entry, and the insecure
  kubeconfig.

## Handoff to Phase 16 (UI)

- **Monitoring data:** dashboards can query UWM through Thanos, using `http_server_request_duration_seconds_*` by
  `http_route`/`http_response_status_code` and `mias_artifact_index_*`.
- **Correlation:** a UI can send `X-Request-ID` and a W3C `traceparent`, and both are honoured.
- **Logs:** any future UI service should follow the same JSON log shape and `OTEL_*` configuration.
- **Collector:** don't expose the collector outside the namespace.
