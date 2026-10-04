# Hardening Task 2: Helm restart hygiene and router cookie cleanup

This task delivered three chart fixes in chart **0.3.1** (appVersion unchanged, `259236684a74`):
1. mias-api restarts only when its configuration data changes.
2. otel-collector restarts only when its configuration data changes.
3. Both Routes stop setting the OpenShift router's sticky cookie.

Deployed as Helm **revision 22**. As planned, the checksum change rolled mias-api and otel-collector **once**. mias-ui
was not restarted, and the publisher stayed at 0.

## 1. API checksum

**Root cause.** `templates/deployment-api.yaml` set:

```yaml
checksum/config: {{ include (print $.Template.BasePath "/configmap-api.yaml") . | sha256sum }}
```

That hashes the **entire rendered ConfigMap**, including `metadata.labels`. Because `helm.sh/chart: mias-<version>` is
a label, every chart-version bump changed the pod template and restarted the API. This happened at 16F revision 16
(0.2.0 → 0.3.0).

**New behaviour:**

```yaml
checksum/config: {{ (include (print $.Template.BasePath "/configmap-api.yaml") . | fromYaml).data | toJson | sha256sum }}
```

This hashes only the ConfigMap's `data`, as Helm `toJson` (sorted keys). Metadata, labels, annotations and the chart
version aren't hashed. Every real configuration value still is: docs, artifact root, OTLP endpoint, exporters,
intervals, resource attributes.

## 2. Collector checksum

**Root cause.** `templates/otel-collector.yaml` set `checksum/config: {{ toJson .Values.observability | sha256sum }}`.
That hashes **every** observability value, including:
- API-only ones (`traces.enabled`, `metrics.exportIntervalMs`, `otlp.endpoint`, `serviceName`, `environment`);
- ServiceMonitor settings;
- collector resources.

None of those change the collector's ConfigMap, so they restarted it needlessly.

**New behaviour.** The collector configuration (the ConfigMap's only key, `config.yaml`) is now a named template,
`mias.collectorConfig` in `_helpers.tpl`. The ConfigMap renders it **byte-identically**: the live data equalled the
revision-21 data after the upgrade. The checksum hashes exactly the ConfigMap's data value:

```yaml
checksum/config: {{ dict "config.yaml" (printf "%s\n" (include "mias.collectorConfig" .)) | toJson | sha256sum }}
```

The `printf "%s\n"` reproduces the trailing newline of the YAML `|` block, so the checksum equals
`sha256(toJson(ConfigMap.data))` exactly. A test enforces this.

## 3. Router cookies

Both `mias-api` (`templates/route.yaml`) and `mias-ui` (`templates/ui.yaml`) now carry:

```yaml
annotations:
  haproxy.router.openshift.io/disable_cookies: "true"
```

**Why:**
- both backends are stateless: the API uses per-request bearer auth, and the UI is static with its session in browser
  memory;
- both are single replica, so stickiness has no effect;
- the router cookie was `HttpOnly; Secure; SameSite=None` and tracking-like;
- future replicas still don't need stickiness, unless per-pod session state is ever introduced (for example an
  oauth-proxy without a shared cookie secret).

**Unchanged:** host, edge TLS, the Redirect policy, target, port, wildcard policy, timeouts and headers.

## Test proof

`tests/test_helm_restart_cookie.py` (8 tests):

| Test | Proves |
|---|---|
| checksums hash ConfigMap data only | mias-api and otel-collector `checksum/config` = `sha256(toJson(ConfigMap.data))` |
| chart metadata change restarts nothing | a copy of the chart with version `9.9.9` renders **identical pod templates** for mias-api, otel-collector and mias-ui (the label changed, checksums didn't) |
| real API config changes roll the API | `api.docsEnabled`, `observability.otlp.endpoint`, `observability.metrics.exportIntervalMs` and `observability.traces.enabled` change the API checksum, and **not** the collector's or UI's |
| unrelated values don't roll the API | `ui.route.host`, `ui.replicaCount`, `monitoring.alerts.enabled`, the ServiceMonitor interval, `route.host` and `publisher.replicaCount` change neither the API nor the collector checksum |
| collector config change rolls only the collector | `observability.collector.traceDebugVerbosity` changes the collector checksum only |
| collector ConfigMap unchanged | content checks, plus a single trailing newline |
| both Routes disable the cookie and keep their spec | annotations are exactly `{disable_cookies: "true"}`; TLS edge with Redirect, port, wildcard, host and target unchanged |
| Route toggles still work | `ui.enabled=false` drops only the UI Route; `ui.route.enabled=false` and `route.enabled=false` behave independently |

**Updated tests:**
- **`test_helm_ui.py`:** the Phase 15 identity check now compares **parsed objects** of the real Phase 15 chart
  (`30455ea`) against the current chart, with UI and alerts off. It normalises only three things: each pod template's
  derived `checksum/config` (the algorithm changed), the `helm.sh/chart` label, and the new `disable_cookies`
  annotation. All four value sets match.
- **`test_helm_mias.py`:** chart 0.3.1; the API Route's annotations are exactly the cookie setting.
- **`test_helm_alerts.py`:** chart 0.3.1.

## Deployment

- **Render diff against live revision 21:** exactly the planned changes (plus the chart label on every object):
  - mias-api `checksum/config`: `37b853c0…` → `a9669fc1…` (one-time, the algorithm changed);
  - otel-collector `checksum/config`: `6bfa8786…` → `71fd5ce5…` (one-time);
  - `disable_cookies` on both Routes.

  No objects were added or removed, and all ConfigMap data was unchanged.
- **Server dry run:** `helm upgrade --reset-values --dry-run=server` showed no conflicts, so `--force-conflicts` wasn't
  used.
- **Upgrade:** `helm upgrade mias deploy/helm/mias -n mias --reset-values --wait` produced **revision 22**, chart
  `mias-0.3.1`.

**Restart proof:**

| Workload | Before | After |
|---|---|---|
| mias-api | `mias-api-68cd5d5878-jnx8f` (uid `83a20318…`) | `mias-api-7598db4d79-jrxfc` (uid `59c3015e…`), Ready, 0 restarts; one new ReplicaSet, generation +1 |
| otel-collector | `otel-collector-bc58d66bf-p75xv` (uid `51e3249e…`) | `otel-collector-9b8c77dd9-bq52c` (uid `3a0db7e7…`), Ready, 0 restarts; generation 5 → 6 |
| mias-ui | `mias-ui-d48744d9b-8rdnc` (uid `741b404d…`) | **unchanged** (same uid and start time, generation 2) |
| mias-publisher | 0 replicas, generation 22 | 0 replicas, generation 22 |

There were no rollout loops: one ReplicaSet each with replicas, and observed generation equal to generation.

**Other objects:** every Route, NetworkPolicy, ConfigMap, Service, the PrometheusRule and the PVC got a new
resourceVersion only because of the chart label (0.3.0 → 0.3.1):
- the ConfigMap data equals revision 21;
- the four baseline NetworkPolicy specs equal the 16F baseline, and the three UI policy spec hashes are unchanged;
- the PVC UID is unchanged.

## Live verification

- **Router cookie:** both Routes return **0 `Set-Cookie` headers** on `/`, `/health/live` and `/health/ready` (before:
  1 each). The application sets no cookies; auth stays browser-memory bearer.
- **TLS and redirect:** HTTPS 200 on the UI; the API's `/health/*` returns 200 (the API's `/` is a 404 by design).
  HTTP gets a 302 to HTTPS on both. The certificate fingerprint `3C:AF:…:B1:24` is unchanged (the self-signed
  `*.apps` wildcard). All 7 UI security headers are present, with the exact CSP.
- **Telemetry:** collector `up=1` (new pod), artifact index healthy `=1`, index age about 30 s (normal refresh), one
  exported API instance, 0 export errors.
- **UWM:** `mias-alerts` has all 11 rules with health `ok`, and **no MIAS alert pending or firing** after the rollout.
  As designed, one restart per workload stays below `MiasPodRestarting`'s threshold of more than 2 in 30m.
- **Artifacts:** both files are byte-identical to the baseline, mode 0444; PVC Bound (same UID); PV Retain; publisher
  0.

## Regression and safety

- **Helm and manifest suites:** 53 passed.
- **Focused suites** (Helm, manifests, observability, API integration and foundation): 117 passed.
- **Full Python regression** (disposable PostgreSQL and Redis, the four exclusions): 2359 passed, 7 skipped, **2
  failed**. Both are known baselines: Fed runpy and SEC argv. The intermittent geopolitical test passed. There are no
  new failures.
- **Settled state, 26 minutes after deployment** (past every rule's `for` window): all 11 rules are `ok`, none is
  pending or firing, and every pod is Ready with 0 restarts.
- **Protected areas and persistent services:**
  - Phase 6 hashes verified; migration head `0007_technical_evidence_ledger`.
  - `mias-postgres` and `mias-redis` unchanged (same ids and start times).
  - `git diff --check` clean.

## Rollback

The preferred rollback is to check out the previous chart commit and run
`helm upgrade mias deploy/helm/mias -n mias --reset-values --wait`.

- **Cookie annotation:** removing it re-enables the router cookie; no pods restart.
- **Checksum algorithm:** reverting it changes the checksum values again, so mias-api and otel-collector **restart
  once more**. That's expected, not a failure.
- **Not recommended:** `helm rollback 21`. It works, but takes the values from history rather than git.

## Remaining limitations

- **Pod-template changes still roll their workload,** by design. For example, changing collector resources or the
  image restarts it; only checksum *inputs* were narrowed.
- **`mias-publisher` has no checksum** (unchanged): it reads its ConfigMap only when started, and it's normally at 0.
- **Stale annotation:** the `mias-api` Route still carries a `kubectl.kubernetes.io/last-applied-configuration`
  annotation from its Phase 14 kubectl origin. It's harmless, and could be removed in a later cleanup.
- **Identity test:** the Phase 15 identity test normalises the checksum values. Their correctness is proven by the
  dedicated checksum tests instead.
