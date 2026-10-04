# Hardening Task 4: synthetic monitoring of the UI Route

This task adds an external-path check of the dashboard. Every 30 s, a Prometheus Blackbox Exporter requests the UI
Route's `/healthz` the way a user would: name resolution, the ingress VIP, the OpenShift router, edge TLS and the
mias-ui pod. User Workload Monitoring (UWM) scrapes the result, and `MiasUiSyntheticFailing` alerts when it fails.

Delivered in chart **0.4.0** (appVersion unchanged, `259236684a74`) and deployed as Helm **revision 23**. No existing
workload restarted: mias-api, mias-ui and otel-collector kept their pods and Deployment generations, and the publisher
stayed at 0.

## 1. Architecture

```
UWM Prometheus ──scrape /probe?target=…&module=http_2xx_mias──▶ blackbox-exporter :9115   (namespace mias-monitoring)
                                                                   │ DNS (openshift-dns)
                                                                   ▼
                                    https://mias-ui.apps.ngc.sirii.org/healthz ─▶ ingress VIP 192.168.5.141:443
                                                                   ─▶ router (edge TLS) ─▶ mias-ui :8080 ─▶ "ok"
probe_success{namespace="mias",job="mias-ui-synthetic"} ─▶ Thanos Ruler ─▶ MiasUiSyntheticFailing ─▶ Alertmanager
```

**Placement:**

| Object | Namespace | Why |
|---|---|---|
| ServiceAccount, ConfigMap, Deployment, Service `blackbox-exporter`; NetworkPolicies `synthetic-default-deny`, `blackbox-exporter` | `mias-monitoring` | A prober makes outbound requests. Keeping it out of `mias` leaves the MIAS default deny and its "no egress except telemetry" rule untouched. |
| Probe `mias-ui-healthz` | `mias` (release namespace) | UWM Prometheus enforces `namespace=<the Probe's namespace>` on scraped series, and the Thanos Ruler enforces `namespace="mias"` on `mias-alerts`. A Probe in `mias-monitoring` would produce series the rule can't see. |
| Alert `MiasUiSyntheticFailing` | `mias` (`mias-alerts`, new group `mias.synthetic`) | Same rule object as the other 11 alerts. |

**Values (`syntheticMonitoring`):**
- `enabled: true`;
- `namespace: mias-monitoring`;
- `image` (repository and digest), `interval: 30s`, `scrapeTimeout: 15s`, `probeTimeout: 10s`;
- `tls.insecureSkipVerify: true` (temporary, see §5);
- `ingressVIP: 192.168.5.141/32`;
- `resources`.

The feature renders **only while `ui.enabled` and `ui.route.enabled` are both true**, because without a UI Route there
is nothing to probe. The NetworkPolicies also follow the chart-wide `networkPolicy.enabled`.

## 2. Prerequisites (out of band, one time)

These are created outside Helm, like Secret `mias-api-auth`. A Helm-managed Namespace would break the server dry run
(the namespace doesn't exist yet when objects in it are validated), and would delete everything in it on disable.

```bash
oc create namespace mias-monitoring
oc label namespace mias-monitoring app.kubernetes.io/part-of=mias app.kubernetes.io/component=synthetic-monitoring
oc create imagestream blackbox-exporter -n mias-monitoring
oc label imagestream blackbox-exporter -n mias-monitoring app.kubernetes.io/part-of=mias
```

**Image:** upstream `quay.io/prometheus/blackbox-exporter:v0.28.0`.
- Upstream index digest `sha256:43027b43…`; linux/amd64 config `sha256:2bb660d6acaae57b2514fa4d114428b1670c6ef84eee07834710b26fc003796b`.
- Pushed through `oc port-forward` to the internal registry (`mias-monitoring/blackbox-exporter:v0.28.0`). Stored as
  `sha256:22def1f1843206a9dc6bf476533c16c025d0594ad2867778368a1735b87c7d38`, the digest pinned in `values.yaml`.
- The ImageStream sits in the exporter's own namespace, so the pod pulls it with its default dockercfg secret. No
  cross-namespace pull RBAC is needed.

The namespace has the cluster defaults: PSA audit/warn `restricted`, and UID range `1000730000/10000`.

## 3. The exporter

| Property | Value |
|---|---|
| SCC | `restricted-v2` (admitted as such), arbitrary UID from the namespace range; no `runAsUser` or `fsGroup` in the chart |
| Security context | `runAsNonRoot`, seccomp `RuntimeDefault`, `allowPrivilegeEscalation: false`, all capabilities dropped, **read-only root filesystem** |
| Service account | `blackbox-exporter`, `automountServiceAccountToken: false` on the SA and the pod; no RBAC; `enableServiceLinks: false` |
| Storage | none: only the ConfigMap volume, mounted read-only at `/etc/blackbox_exporter`; no PVC, no emptyDir |
| Resources | requests 10m CPU / 16Mi; limits 100m / 64Mi (observed 1m / 9Mi) |
| Replicas | 1, RollingUpdate (maxUnavailable 0) |
| Health | liveness and readiness on `/-/healthy` |
| DNS | `ndots: 1`, so the Route host resolves as an absolute name first, without search-domain expansion |
| Restart hygiene | `checksum/config` hashes only the ConfigMap data (`sha256(toJson(data))`, the Task 2 pattern) |

**Module `http_2xx_mias`:**
- `GET` with a 10 s timeout; only status 200 is accepted, over HTTP/1.1 or HTTP/2;
- IPv4 only (`preferred_ip_protocol: ip4`, no fallback);
- **no redirect following**, and `fail_if_not_ssl: true`: a plain-HTTP or redirected answer fails;
- the body must match `^ok`, the UI's `/healthz` answer. A router error page with status 200 would therefore fail.

The rendered config passed `blackbox_exporter --config.check` with the v0.28.0 image.

## 4. Network policy

`synthetic-default-deny` selects every pod in `mias-monitoring` and denies all ingress and egress. The
`blackbox-exporter` policy then allows only:

| Direction | Peer | Port |
|---|---|---|
| Ingress | pods `app.kubernetes.io/name=prometheus` in `openshift-user-workload-monitoring` | TCP 9115 |
| Egress | cluster DNS pods (`dns.operator.openshift.io/daemonset-dns=default` in `openshift-dns`) | UDP and TCP 5353 |
| Egress | `ipBlock` **192.168.5.141/32** (the cluster ingress VIP) | TCP 443 |

**Why an ipBlock:** the routers are host-network pods, so a pod or namespace selector can't match them. Inside the
cluster, `mias-ui.apps.ngc.sirii.org` resolves to the keepalived ingress VIP `192.168.5.141` (the vSphere `ingressIPs`
value).

This is the chart's only ipBlock, and it isn't in `mias`. The `mias` policies still contain no ipBlock, which the
tests enforce.

**Proven live:**
- DNS from the pod resolves the Route host to `192.168.5.141`;
- the probe succeeds;
- an arbitrary HTTPS request from the pod (`example.com`) times out.

If the VIP ever changes, update `syntheticMonitoring.ingressVIP`. Until then the probe fails, and the alert fires
(see §8).

## 5. TLS: temporary lab mode

The lab `*.apps` certificate is self-signed by the ingress operator. It's the same certificate users see (SHA-256
`3C:AF:…:B1:24`, valid until 2028-09-11). The exporter therefore runs with `insecure_skip_verify: true`, set from
`syntheticMonitoring.tls.insecureSkipVerify`:

- **What it still checks:** a TLS handshake (TLS 1.3 observed), HTTPS rather than HTTP, and the content.
- **What it does not check:** that the certificate chains to a trusted CA.
- **Where it applies:** only in the exporter's ConfigMap. No CA or TLS setting was added to any MIAS container or
  Route.
- **What's still recorded:** `probe_ssl_earliest_cert_expiry` (currently 1852320268, i.e. 2028-09-11), so expiry is
  visible in UWM. It is **not alerted on** yet; see §9.

**When trusted TLS lands,** set `syntheticMonitoring.tls.insecureSkipVerify=false`. If the CA isn't publicly trusted,
also mount it into the exporter (a `ca_file` in the module). Only the exporter's config checksum changes, so only the
exporter restarts.

## 6. Alert

```yaml
- name: mias.synthetic
  rules:
    - alert: MiasUiSyntheticFailing
      expr: |
        (probe_success{namespace="mias",job="mias-ui-synthetic"} == 0)
        or (up{namespace="mias",job="mias-ui-synthetic"} == 0)
      for: 5m
      labels: {severity: critical, component: ui, part_of: mias}
```

- **`probe_success == 0`:** the user path is broken (DNS, VIP, router, TLS, Route or pod).
- **`up == 0`:** the probe couldn't run (exporter down, or blocked by the NetworkPolicy). That's not proof the UI is
  down, but it is a loss of external visibility worth paging on.
- **Relation to `MiasUiUnavailable`:** that alert stays as is. It sees pod availability; this one sees the routed path.
  When both fire, the pods are down; when only this one fires, the problem is in front of the pods.
- **Window:** `for: 5m` at a 30 s interval means about 10 consecutive failed probes. A single rollout of mias-ui
  (maxUnavailable 0) doesn't trip it.
- **Scope:** the group renders only while synthetic monitoring is active. The other 11 rules are byte-for-byte
  unchanged (tested).

## 7. Tests

`tests/test_helm_synthetic.py` (18 tests) covers the 17 required items:

| # | Test | Proves |
|---|---|---|
| 1 | resources render when enabled | all 7 objects; exporter objects in `mias-monitoring` with `part-of: mias`; the Probe in `mias`; no Namespace object |
| 2 | disabled path removes them cleanly | `syntheticMonitoring.enabled=false` removes exactly those 7 objects and the alert; every other object is identical; `ui.enabled=false` and `ui.route.enabled=false` also remove them |
| 3 | probe target is exact | static target `https://mias-ui.apps.ngc.sirii.org/healthz`, derived from `ui.route.host`; prober `blackbox-exporter.mias-monitoring.svc:9115` `/probe` over HTTP; ClusterIP Service on 9115 |
| 4 | module | `http_2xx_mias` in both the Probe and the config (the only module); GET, 200 only, no redirects, SSL required, body `^ok`, IPv4 |
| 5 | interval and timeouts | 10 s probe < 15 s scrape < 30 s interval; at least 10 probes per 5 m window |
| 6 | TLS lab mode | `insecure_skip_verify` follows the value (true now, false renders false), appears once in the whole render, and is commented TEMPORARY |
| 7 | restricted | exact pod and container security contexts; no UID, fsGroup, host or privileged fields; digest-pinned image (bad digests refused); read-only config mount; health probes |
| 8 | no SA token | automount off on the SA and the pod; no RBAC or Secret objects |
| 9 | resources | exact requests and limits |
| 10 | no PVC | only the ConfigMap volume; the only PVC is `mias-artifacts` |
| 11 | minimal NetworkPolicy | exact default deny and exact allow rules (UWM ingress on 9115; DNS; VIP /32 on 443); no `0.0.0.0/0`; `networkPolicy.enabled=false` drops them |
| 12 | alert only when active | `MiasUiSyntheticFailing`: exact labels, annotations and expression; absent with synthetic, UI or UI Route off; the Probe stays when alerts are off |
| 13 | existing 11 alerts unchanged | identical rule objects and groups with synthetic on and off |
| 14 | chart bump restarts nothing | a copy at chart 9.9.9 renders identical pod templates for all five Deployments. The exporter's checksum equals `sha256(toJson(data))` and changes only for probe-config values (`probeTimeout`, TLS), not `interval` or unrelated values |
| 15 | no workload diff | mias-api, mias-ui, otel-collector and mias-publisher specs hash-equal chart 0.3.1 (main `5d1befe`); the ConfigMaps are unchanged |
| 16 | no Route change | both Route specs hash-equal 0.3.1; annotations still exactly `disable_cookies` |
| 17 | no storage change | the PVC spec hash-equals 0.3.1, with `resource-policy: keep`; no PV or volumeClaimTemplates |
| — | lint and version | `helm lint` clean; chart 0.4.0, appVersion `259236684a74` |

**Updated tests:**
- **`test_helm_mias.py`:** the object set includes the synthetic objects; namespaces are checked per object; the
  single ipBlock is pinned to the exporter policy; chart 0.4.0.
- **`test_helm_alerts.py`:** 11 core alerts plus the synthetic one, and `probe_success` added to the verified metrics.
  The core set is unchanged with synthetic off.
- **`test_helm_ui.py`:** disabling the UI also removes the synthetic objects and alert.
- **`test_helm_observability.py`:** the "no ipBlock" checks are scoped to `mias`.
- **`test_openshift_manifests.py`:** the strict test YAML parser accepts the empty map `{}` (`podSelector: {}`).

## 8. Deployment and live verification

**Render diff against live revision 22:**
- **Added:** the 6 objects in `mias-monitoring` and the Probe in `mias`.
- **Changed beyond the chart label:** only `mias-alerts` (the new `mias.synthetic` group).
- **Removed:** none.

**Server dry run** (`--reset-values --dry-run=server`): clean, with no conflicts, so `--force-conflicts` wasn't used.

**Upgrade:** `helm upgrade mias deploy/helm/mias -n mias --reset-values --wait` produced **revision 23**, chart
`mias-0.4.0`.

**Restart proof:**

| Workload | Before and after (identical) |
|---|---|
| mias-api | `mias-api-7598db4d79-jrxfc` (uid `59c3015e…`), generation 10, 0 restarts |
| mias-ui | `mias-ui-d48744d9b-8rdnc` (uid `741b404d…`), generation 2, 0 restarts |
| otel-collector | `otel-collector-9b8c77dd9-bq52c` (uid `3a0db7e7…`), generation 6, 0 restarts |
| mias-publisher | 0 replicas, generation 22 |

**Exporter:** `blackbox-exporter-78674f675b-z5zzh`, Ready within seconds, 0 restarts.
- SCC `restricted-v2`, UID 1000730000;
- read-only root, all capabilities dropped, no privilege escalation;
- no `/var/run/secrets/kubernetes.io` mount;
- image digest `sha256:22def1f1…`.

**UWM:**
- **Target:** `probe/mias/mias-ui-healthz` was active and `up` about 75 s after the upgrade (the config-reloader
  interval).
- **Series:**

  | Series | Value |
  |---|---|
  | `probe_success` | 1 |
  | `probe_http_status_code` | 200 |
  | `probe_http_ssl` | 1 |
  | `probe_tls_version_info` | TLS 1.3 |
  | `probe_http_version` | 1.1 |
  | `probe_duration_seconds` | about 0.09 s (max 0.12 s) |
  | `probe_dns_lookup_time_seconds` | about 0.004 s |

  All series carry `namespace="mias"` and `instance="https://mias-ui.apps.ngc.sirii.org/healthz"`.

**Rules:**
- **12 MIAS rules,** all `health=ok`: the previous 11 plus `MiasUiSyntheticFailing` in `mias.synthetic`.
- **Alerts:** none pending or firing.

**Unchanged:**
- **Routes:** both Routes keep host, edge/Redirect and `disable_cookies`. Their resourceVersions changed only for the
  chart label.
- **Responses:** UI `/healthz` returns `ok`, and `/` returns 200; the API's `/health/ready` returns 200; HTTP gets a 302
  to HTTPS.
- **Headers and certificate:** 0 `Set-Cookie`; all 7 UI security headers; certificate fingerprint as before.
- **Telemetry:** collector `up=1`, index healthy, index age under 1 s.
- **Artifacts and storage:**
  - artifacts byte-identical to the baseline (`85ebe206…`, `53d7509a…`), mode 0444;
  - PVC Bound with the same UID (`8ffa03dd…`); PV Retain.
- **Persistent services:** `mias-postgres` and `mias-redis` untouched.

No failure was injected: the Route wasn't broken to test the alert, per the task's rules.

## 9. Limitations

- **TLS verification is skipped** (lab self-signed certificate; §5). The probe proves TLS works, not that the
  certificate is trusted.
- **In-cluster vantage point:** WSL and lab DNS resolve `*.apps` to `192.168.1.60`, an external front end, while the
  cluster resolves to the ingress VIP. The probe therefore covers DNS (cluster), VIP, router, TLS and the pod, but
  **not** the external front end or a user's DNS. A probe from outside the cluster would be needed for that.
- **The ingress VIP is pinned** in values. A VIP change breaks the probe (fails safe: the alert fires) until the value
  is updated.
- **Certificate expiry is not alerted.** `probe_ssl_earliest_cert_expiry` is collected. A proposed rule:
  ```
  probe_ssl_earliest_cert_expiry{namespace="mias",job="mias-ui-synthetic"} - time() < 21 * 86400
  ```
  It wasn't added in this task (the scope was one alert).
- **The API Route isn't probed.** Only the UI Route is. The UI's `/healthz` doesn't exercise the API: an API outage
  is covered by `MiasApiUnavailable` and `MiasApiNotReady`.
- **Missing series aren't alerted.** If the Probe object itself were deleted, both series would disappear and this
  alert couldn't fire.
- **Single exporter replica.** An exporter outage pages through the `up == 0` branch rather than going silent.
- **Alertmanager delivery** remains unverified (Secret-backed receivers; unchanged from Task 1).
- **Chart NOTES** don't mention the Probe yet. Changing them would make the repo differ from revision 23's stored
  notes, so this is left for the next chart release.

## 10. Rollback

- **Disable only the probe:** `helm upgrade mias deploy/helm/mias -n mias --reset-values --set
  syntheticMonitoring.enabled=false --wait`. This removes the 7 objects and the alert, and restarts no MIAS workload.
- **Back to chart 0.3.1:** check out `5d1befe` and run `helm upgrade … --reset-values --wait`. The result is the same.
- **Namespace and ImageStream** stay either way (out of band). Remove them by hand only if synthetic monitoring is
  abandoned: `oc delete namespace mias-monitoring`.
