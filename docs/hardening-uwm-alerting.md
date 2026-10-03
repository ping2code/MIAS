# Hardening Task 1: MIAS alerting in User Workload Monitoring

MIAS now has alert rules: PrometheusRule **`mias-alerts`** in namespace `mias`, evaluated by the OpenShift User
Workload Monitoring (UWM) Thanos Ruler and sent to the platform Alertmanager. It's additive: no workload, ConfigMap,
Route, NetworkPolicy or Secret changed, and no pod restarted.

## Why alerting came first

The read-only hardening assessment found **no alerting at all**, the largest operational blind spot. Alerting is
purely additive: no pod template, no restart, no architecture decision. It also covers the later hardening steps,
which do mutate the platform.

## Where it lives

- **Template:** `deploy/helm/mias/templates/prometheusrule.yaml`, gated by `monitoring.alerts.enabled` (default
  `true`).
- **Thresholds:** fixed in the template, with no values per threshold in this first version.
- **UI alert:** `MiasUiUnavailable` is rendered only when `ui.enabled`, so a UI rollback leaves no orphaned UI alert.
- **Evaluation scope:** the default, which is the **UWM Thanos Ruler**. The `leaf-prometheus` scope is deliberately
  not used, because several rules need platform kube-state-metrics and kubelet series for the namespace.
- **Chart version:** stays **0.3.0**. Bumping it would restart mias-api, because its `checksum/config` still hashes
  labels; that's the next hardening task. The rendered diff against the live release was exactly one new object.

## Metrics used (verified live before the rules were written)

| Source | Series (label shape) |
|---|---|
| kube-state-metrics (platform) | `kube_deployment_status_replicas_available{namespace,deployment}`, `kube_deployment_spec_replicas{namespace,deployment}`, `kube_pod_status_ready{namespace,pod,condition}`, `kube_pod_container_status_restarts_total{namespace,pod,container}`, `kube_pod_container_status_waiting_reason{namespace,pod,container,reason}` (series exist only while a container is waiting) |
| kubelet (platform) | `kubelet_volume_stats_used_bytes` and `kubelet_volume_stats_capacity_bytes` `{namespace,persistentvolumeclaim="mias-artifacts"}` |
| collector scrape target (UWM) | `up{namespace="mias",job="otel-collector"}` |
| mias-api via the collector's Prometheus exporter (UWM) | `mias_artifact_index_healthy`, `mias_artifact_index_last_success_age_seconds`, `http_server_request_duration_seconds_count{http_route,http_response_status_code,http_request_method}`, all with `job="otel-collector"` and `exported_job="mias/mias-api"` |

**How these differ from the assessment sketch:**
- mias-api metrics carry `exported_job="mias/mias-api"` (`job` is the collector), so the rules select on
  `exported_job`.
- `/api/v1/alerts/{artifact_id}/deliveries` returns **503 `dependency_unavailable`** by design in this deployment (no
  receipt store), and every alert detail view calls it, so the 5xx ratio subtracts that one route and code.
- The collector's own metrics (`otelcol_*`) are disabled. The rules use the scrape target, the deployment and the
  absence of MIAS metrics instead.

## Alerts

`ns` is the release namespace (`mias`). Every alert has the labels `severity`, `component` and `part_of: mias`, and
the annotations `summary`, `description` and `runbook_hint`. There are no URLs and no secrets.

| Alert | Expression | For | Severity | Component |
|---|---|---|---|---|
| MiasApiUnavailable | `kube_deployment_status_replicas_available{namespace="mias",deployment="mias-api"} < 1` | 5m | critical | api |
| MiasApiNotReady | `max(kube_pod_status_ready{namespace="mias",pod=~"mias-api-.*",condition="true"}) == 0` | 5m | critical | api |
| MiasUiUnavailable *(only when `ui.enabled`)* | `kube_deployment_status_replicas_available{namespace="mias",deployment="mias-ui"} < 1` | 5m | warning | ui |
| MiasCollectorDown | `(up{namespace="mias",job="otel-collector"} == 0) or (absent(up{namespace="mias",job="otel-collector"}) == 1) or (kube_deployment_status_replicas_available{namespace="mias",deployment="otel-collector"} < 1)` | 10m | warning | collector |
| MiasPodRestarting | `(increase(kube_pod_container_status_restarts_total{namespace="mias"}[30m]) > 2) or (max by (namespace,pod,container) (kube_pod_container_status_waiting_reason{namespace="mias",reason="CrashLoopBackOff"}) == 1)` | 10m | warning | workload |
| MiasPublisherLeftRunning | `kube_deployment_spec_replicas{namespace="mias",deployment="mias-publisher"} > 0` | 2h | info | publisher |
| MiasArtifactIndexUnhealthy | `mias_artifact_index_healthy{namespace="mias",exported_job="mias/mias-api"} == 0` | 10m | critical | artifact-index |
| MiasArtifactIndexStale | `mias_artifact_index_last_success_age_seconds{namespace="mias",exported_job="mias/mias-api"} > 300` | 10m | warning | artifact-index |
| MiasArtifactPvcFilling | `kubelet_volume_stats_used_bytes{…,persistentvolumeclaim="mias-artifacts"} / kubelet_volume_stats_capacity_bytes{…} > 0.80` | 30m | warning | storage |
| MiasApi5xxRatio | see below | 10m | warning | api |
| MiasTelemetryPipelineStalled | `absent_over_time(mias_artifact_index_healthy{namespace="mias",exported_job="mias/mias-api"}[15m]) == 1` | — | warning | telemetry |

`MiasApi5xxRatio` (non-health traffic, 10-minute window):

```promql
(
  ( sum(rate(http_server_request_duration_seconds_count{namespace="mias",exported_job="mias/mias-api",http_route!~"/health/.*",http_response_status_code=~"5.."}[10m]))
    - (sum(rate(http_server_request_duration_seconds_count{namespace="mias",exported_job="mias/mias-api",http_route="/api/v1/alerts/{artifact_id}/deliveries",http_response_status_code="503"}[10m])) or vector(0)) )
  / sum(rate(http_server_request_duration_seconds_count{namespace="mias",exported_job="mias/mias-api",http_route!~"/health/.*"}[10m]))
) > 0.05
and sum(rate(http_server_request_duration_seconds_count{namespace="mias",exported_job="mias/mias-api",http_route!~"/health/.*"}[10m])) > 0.05
```

- **Ratio:** above 5% of non-health requests.
- **Traffic floor:** more than 0.05 req/s.
- **Zero traffic:** returns nothing, because the `and` floor removes the divide-by-zero case.
- **No 5xx at all:** the numerator is empty, so the rule returns nothing.

**Not added:** `MiasUiSyntheticFailing`, since no `probe_success` exists yet. Synthetic monitoring is a later
hardening task.

## Overlaps and duplicates (expected)

- **API down:** `MiasApiUnavailable`, `MiasApiNotReady` (while a pod exists) and, after about 20 minutes,
  `MiasTelemetryPipelineStalled` can all fire together. The collector's exporter expires the stale series after about
  5 minutes, then the 15-minute absence window applies.
- **Collector down:** `MiasCollectorDown`, then `MiasTelemetryPipelineStalled`.
- **Index broken:** `MiasArtifactIndexUnhealthy`, then `MiasApiNotReady` (readiness includes `artifact_index`), and
  possibly `MiasArtifactIndexStale`.
- **Upgrades:** a normal Recreate rollout of mias-api is far shorter than 5 minutes, and new pods start new restart
  series at 0, so upgrades don't trip these rules.
- **Suppressing duplicates:** Alertmanager inhibition rules (for example "API down suppresses telemetry stalled")
  could do this later. They are **not** configured here, because that's receiver-side configuration.

## Validation results

- **PromQL:** every rendered expression was executed read-only against Thanos Querier before deployment. All 11
  parse, return instant vectors, and returned **0 series** (not firing) in the healthy steady state.
- **Selectors bind to real data:** each rule's operands were queried without the threshold, so "not firing" is
  meaningful and not a mismatched label:

  | Operand | Value |
  |---|---|
  | api, ui and collector available | 1 |
  | api ready max | 1 |
  | collector `up` | 1 |
  | restart increase over 30m | 0 |
  | publisher spec | 0 |
  | index healthy | 1 |
  | index age | about 1 s |
  | PVC ratio | 0.00005 |
  | non-health request rate | 0.0035/s |
  | 5xx rate | 0 |
  | deliveries-503 rate | 0 |
  | index samples in 15m | 31 |

- **Deployment:**
  - `helm upgrade --reset-values --dry-run=server` showed no conflicts, so `--force-conflicts` wasn't used.
  - `helm upgrade --reset-values --wait` produced **revision 21** (chart 0.3.0).
  - The render diff against revision 20 was only `PrometheusRule/mias-alerts` added.
- **No restarts and no other changes:**
  - Pod names, UIDs, start times and restart counts are identical before and after.
  - The resourceVersions of every Route, NetworkPolicy, Deployment, ConfigMap and Service are unchanged.
  - Secrets: only Helm's own release-history records changed (v21 added; v11 aged out of the 10-revision history).
- **UWM:**
  - The three groups (`mias.availability`, `mias.artifacts`, `mias.telemetry`) are loaded from
    `mias-mias-alerts-….yaml` with a 15 s interval.
  - **All 11 rules report health `ok`**, with no `lastError`.
  - Group evaluation takes 0.04–0.24 s.
  - `prometheus_rule_evaluation_failures_total` is 0 for the MIAS groups, and there are no MIAS errors or warnings in
    the Thanos Ruler logs.
  - **0 MIAS alerts pending or firing.**
- **Alertmanager path:**
  - `alertmanager-main` 2/2 `up` (cluster members 2).
  - Both Thanos Ruler replicas resolve `alertmanager-operated.openshift-monitoring.svc` to 2 Alertmanagers, with 0 DNS
    failures and 0 dropped alerts.
  - The UWM Prometheus also discovers 2 Alertmanagers.
- **Receiver delivery not verified:** the receiver configuration is Secret-backed and was not inspected. Whether
  alerts reach email, Slack or a pager is unknown; alerts are visible in the OpenShift console (Observe → Alerting).

## Operator response (summary of the `runbook_hint`s)

| Alert | First steps |
|---|---|
| MiasApiUnavailable / MiasApiNotReady | Check `oc get pods,events -n mias`, the rollout status and `/health/ready` checks (settings, artifact_root, artifact_index); the artifact volume mount; recent Helm revisions. |
| MiasUiUnavailable | Check the mias-ui rollout and `/healthz`; the API Route is independent. |
| MiasCollectorDown | Check the collector pod and health endpoint, the ServiceMonitor, and `otel-collector-ingress`. API serving is unaffected. |
| MiasPodRestarting | Run `oc logs --previous`; look at probe failures, OOM kills, and recent image or config changes. |
| MiasPublisherLeftRunning | Confirm no publish is in progress, then scale `mias-publisher` to 0. |
| MiasArtifactIndexUnhealthy / Stale | Check API logs for refresh errors, recently published artifacts and the volume mount. |
| MiasArtifactPvcFilling | Plan a PVC expansion (`thin-csi` allows expansion). Never delete artifacts. |
| MiasApi5xxRatio | Check API logs by `request_id` and `http.route`, `artifact_invalid` errors and recent deployments. |
| MiasTelemetryPipelineStalled | Check the API and collector alerts first, then API OTLP export errors and `mias-api-egress-telemetry`. |

## Known limitations

- Notification delivery isn't verified (Secret-backed receivers weren't inspected), and there's no inhibition.
- There's no synthetic or external check yet, so a Route, TLS or DNS failure with healthy pods won't alert.
- Collector self-metrics are off, so export errors show up only as missing metrics (`MiasTelemetryPipelineStalled`).
- `MiasApiNotReady` relies on `MiasApiUnavailable` when no API pod exists.
- If kube-state-metrics itself were absent, the kube-based alerts wouldn't fire. That's covered by platform alerts.
- At very low traffic, the 5xx rule needs sustained traffic above 0.05 req/s to fire.
- Thresholds are fixed in the template and not tuned yet against weeks of data.

## Rollback

Remove only the alert resource; no workload is involved:

```bash
helm upgrade mias deploy/helm/mias -n mias --reset-values --set monitoring.alerts.enabled=false --wait
```

The Helm tests prove the disabled render removes exactly `PrometheusRule/mias-alerts` and leaves every other object
identical. Setting the value back (or a plain upgrade from the defaults) restores it.

## Tests

`tests/test_helm_alerts.py` (7 tests) covers:
- identity, namespace and labels, and no `leaf-prometheus` scope;
- the exact 11-alert set, with the synthetic alert absent;
- severity, `for` and labels per alert;
- annotations present, with no URLs or secrets;
- that expressions use only the verified metric names, with every selector scoped to `namespace="mias"`;
- the 5xx, index, PVC, restart and telemetry expression semantics;
- the UI alert following `ui.enabled`;
- additivity: the disabled render differs only by the rule, contains no Secret or RBAC, and keeps chart 0.3.0.

`test_helm_mias.py` and `test_helm_ui.py` were updated for the new default object. The Phase 15 identity check now
also renders with `monitoring.alerts.enabled=false`.

**Results:**
- Helm and manifest suites: 45 passed.
- Focused (Helm, manifests, observability, API integration and foundation): 109 passed.
- `git diff --check`: clean.
- Phase 6 hashes verified; migration head `0007_technical_evidence_ledger`.
- `mias-postgres` and `mias-redis` unchanged.
