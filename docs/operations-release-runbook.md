# MIAS operations: release, verification and rollback runbook

**This is the authoritative procedure for releasing, verifying and rolling back MIAS on OpenShift.** It consolidates
the proven steps from Phase 14 (container, OpenShift baseline, publishing, Helm), Phase 15 (observability), Phase 16
(UI) and Hardening Tasks 1–2 (alerting, restart hygiene, router cookies). The phase documents stay as history and
evidence; follow this one.

> **Secrets.** Never print, log, commit or paste a token, password or registry credential. Where a step needs the read
> token (authenticated checks, browser QA), hold it in a shell or process variable only, pass it to `curl` on stdin
> (`-H @-`), and `unset` it afterwards. Get explicit approval before reading any Secret value. Never read unrelated
> Secrets (for example Alertmanager receivers).

---

## 1. Scope

| Covered | Not covered |
|---|---|
| Building, pushing, deploying, verifying and rolling back `mias-api`, `mias-ui`, the OpenTelemetry collector configuration and the Helm chart `deploy/helm/mias` in namespace `mias` | Scientific or pipeline changes (Phase 6 contracts, migrations); PostgreSQL and Redis; TLS and DNS replacement; auth replacement. Each needs its own approved plan. |

Never combine a scientific or contract change with a platform release.

## 2. Current topology

Facts as of chart 0.4.1 and Helm revision 25. **Verify current values** with §32 before relying on them.

```
https://mias-ui.apps.<domain>  ─router (edge TLS)─▶ mias-ui :8080 ─same-origin proxy─▶ mias-api :8080 ─▶ /var/lib/mias/artifacts (PVC, RWO)
https://mias-api.apps.<domain> ─router (edge TLS)─▶ mias-api :8080 ─OTLP/HTTP─▶ otel-collector ◀─scrape─ UWM Prometheus ─▶ Thanos Ruler ─▶ Alertmanager
```

| Item | Current value |
|---|---|
| Platform | OpenShift 4.21.x; namespace `mias`; Helm release `mias`, chart `mias` **0.4.1** (verify current value) |
| mias-api | 1 replica, Recreate, digest-pinned (`image.digest`), Secret `mias-api-auth` for the read token |
| mias-ui | **2 replicas** on different workers (strict hostname topology spread), PDB `mias-ui` (minAvailable 1), RollingUpdate (maxUnavailable 0, maxSurge 1), digest-pinned (`ui.image.digest`), nginx same-origin proxy, no Secret. See `docs/hardening-ui-ha.md` |
| otel-collector | 1 replica, Recreate, digest-pinned (`observability.collector.image.digest`), ServiceMonitor scraped by UWM |
| mias-publisher | toolbox Deployment, **normally 0 replicas** |
| Storage | PVC `mias-artifacts` (RWO, `thin-csi`), Helm `resource-policy: keep`; PV reclaim policy **Retain** |
| Routes | `mias-api` and `mias-ui`, edge TLS with Redirect, `disable_cookies: "true"`; self-signed lab `*.apps` certificate |
| NetworkPolicies | 7 in `mias`: the default deny, the API's router ingress, telemetry egress, collector ingress, and 3 UI policies; 2 in `mias-monitoring` (default deny; exporter: UWM ingress, DNS and ingress VIP /32 egress) |
| Monitoring | UWM enabled; PrometheusRule `mias-alerts` (12 rules); Thanos Ruler → platform Alertmanager |
| Synthetic | Blackbox Exporter (`mias-monitoring`, digest-pinned) probes `https://<ui route>/healthz` every 30 s through Probe `mias-ui-healthz` (in `mias`); alert `MiasUiSyntheticFailing`. See `docs/hardening-synthetic-monitoring.md` |
| Auth | shared static read token; the UI keeps it in browser memory only |
| Registry | internal OpenShift registry, **no external route** (push via `oc port-forward`) |

## 3. Preconditions

- `oc whoami` returns the operator identity (`mias-admin` in the lab). OAuth sessions expire: re-login before
  starting, not halfway through a push.
- Tools: `git`, `helm` (4.x), `podman`, `node` 22 and `npm`; the project Python venv for tests; `curl`, `openssl`.
- A change ticket or PR that states the **change type** (§5) and the **expected rollout impact** (§10).
- The **previous known-good digests** are recorded, and their tags still exist (§25).
- Out-of-band prerequisites exist: Secret `mias-api-auth` in `mias`; namespace `mias-monitoring` with ImageStream
  `blackbox-exporter` holding the pinned exporter digest (`docs/hardening-synthetic-monitoring.md` §2).

## 4. Pre-release verification (read-only)

```bash
cd ~/MyRepos/MIAS-codex
git fetch origin && git status --short          # must print nothing
git rev-parse HEAD origin/main                  # must be identical (release only from merged main)
ls migrations/versions | sort | tail -1         # expect the current head (today 0007_technical_evidence_ledger)
```

**Phase 6 frozen hashes** (must print `phase6 True`):

```bash
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -c "
from evidence.registry import HYPOTHESIS_HASHES, PROTOCOL_HASH, REGISTRY_HASH
print('phase6', REGISTRY_HASH=='b1080c2e9347285bd3a56cad55b44e46649ea049bfaaedcc7e15488a45d87acd' and PROTOCOL_HASH=='15587aed49589b8d8535be636b07a3b285cf0c71e0a2b0df63c0d663f349d3c8' and sorted(HYPOTHESIS_HASHES.values())==sorted(['d89efb30b22af34f3ffe6a7b4cd500af8a4a9f95b5f1a903e32f8fec6dca8bca','78e82413275f0bc8a7e4500cbbecbb263ed4414fb4b699838a18c7e212838e4a']))"
```

**Persistent services:** record these. They must be identical after the release.

```bash
docker inspect -f '{{.Name}} {{.Id}} {{.State.Status}} {{.State.StartedAt}}' mias-postgres mias-redis
```

**Live state:**

```bash
helm list -n mias                                                        # revision, chart, status=deployed
oc get deploy,pods -n mias                                               # api/ui/collector 1/1, publisher 0/0
oc get pvc mias-artifacts -n mias -o jsonpath='{.status.phase} {.metadata.uid}{"\n"}'
oc get pv "$(oc get pvc mias-artifacts -n mias -o jsonpath='{.spec.volumeName}')" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}{"\n"}'
oc get istag -n mias -o custom-columns=TAG:.metadata.name,DIGEST:.image.metadata.name   # rollback tags exist
```

Alerts must be healthy and quiet (§18). Don't release while a MIAS alert is firing unless the release fixes it.

## 5. Test gates by change type

**Frontend gates (`ui/`):**

```bash
cd ui && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build && npm run audit:bundle
```

**Helm and manifest suites:**

```bash
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_helm_mias.py tests/test_helm_ui.py tests/test_helm_observability.py tests/test_helm_alerts.py \
  tests/test_helm_restart_cookie.py tests/test_openshift_manifests.py
```

**Focused API suites:** `tests/test_api_foundation.py`, `tests/test_api_integration.py` and
`tests/test_observability.py`. API container tests: `MIAS_CONTAINER_IMAGE=localhost/mias-api:<sha12>
MIAS_CONTAINER_BUILD_ID=<sha> pytest tests/test_container_runtime.py`.

**Full Python regression:** the four standard exclusions, with disposable PostgreSQL and Redis. Never the persistent
`mias-postgres` or `mias-redis`.

```bash
docker run -d --name mias-test-phase2c-postgres --label mias.disposable-test=phase2c \
  --mount type=volume,destination=/var/lib/postgresql/data -e POSTGRES_DB=mias_test_phase2c \
  -e POSTGRES_USER=mias_test_user -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55433:5432 \
  postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6
docker run -d --name mias-test-phase2j-redis --label mias.disposable-test=phase2j --tmpfs /data \
  -p 127.0.0.1:56379:6379 redis@sha256:c7d14d623c137a1bb6c3a6755b0b0aad499177087c2140eefcf2f122950b172d \
  redis-server --save "" --appendonly no
TEST_DATABASE_URL=postgresql://mias_test_user@127.0.0.1:55433/mias_test_phase2c \
MIAS_PHASE2C_TEST_CONTAINER=mias-test-phase2c-postgres MIAS_PHASE2J_REDIS_URL=redis://127.0.0.1:56379/0 \
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m pytest -q -p no:cacheprovider \
  --ignore=tests/test_openai.py --ignore=tests/test_openai_analyzer.py \
  --ignore=tests/test_alert_formatter.py --ignore=tests/test_headline_similarity.py tests
docker rm -f -v mias-test-phase2c-postgres mias-test-phase2j-redis
```

**Known baseline failures (the only acceptable ones):**
- `tests/test_fed_pipeline.py::ExistingBehaviorTests::test_existing_smoke_scripts_with_mocked_services` (Fed runpy);
- `tests/test_sec_pipeline.py::SecPipelineTests::test_script_path_matches_extracted_function` (SEC argv);
- `tests/test_geopolitical_identity_stats.py::PostgreSQLStagingRolloutTests::test_full_runbook_with_in_memory_redis`
  (intermittent: passes or fails).

**Any other failure is new and blocks the release.** Never add a failure to this list to unblock a release.

**Which gates apply:**

| Change type | Required gates |
|---|---|
| UI only (`ui/`) | frontend gates; image build (its in-build gates); Podman matrix and real-API end-to-end; local and live browser QA (§15); Helm suites if the digest pin changes |
| Helm or chart only | Helm and manifest suites; render diff (§10); server dry run (§11); focused API suites if API ConfigMap or Deployment templates change; full regression when pod templates or workloads change |
| API image or API code | focused API suites; API container tests; full regression; Helm suites (digest pin) |
| Collector configuration | Helm suites (`test_helm_observability`, `test_helm_restart_cookie`); telemetry validation (§17) |
| Alert rules | `test_helm_alerts`; read-only PromQL validation of every rendered expression against Thanos (each must parse, and its operands must bind to real series) |
| Documentation only | `git diff --check`; Helm lint and render if `NOTES.txt` changed |
| **Scientific code** | All of the above **plus** frozen-contract verification (Phase 6 hashes, migration head). **Never** in the same release as platform hardening. |

The **Podman matrix** and **real-API end-to-end** checks (Phases 16B–16F) run the UI image with an arbitrary UID,
read-only root and `/tmp` tmpfs. They check:
- headers and CSP, cache policy, SPA fallback;
- proxy pass-through of Authorization and X-Request-ID; 401 and 503 pass-through; canonical ETag and bytes;
- POST returning 403, the 504 timeout, clean SIGTERM;
- invalid configuration exiting 2;
- deep links.

The scripts that ran them aren't in the repository yet (§31). Until they are, recreate them from the 16B–16F
documents.

## 6. Build (exact commit only)

Build from the **merged main commit**, from an exact `git archive`, so no working-tree content can leak in:

```bash
SHA=$(git rev-parse origin/main); B=$(mktemp -d)
git archive "$SHA" ui | tar -x -C "$B"                  # UI
podman build --no-cache --build-arg MIAS_UI_BUILD_ID=$SHA -t localhost/mias-ui:${SHA:0:12} -f "$B/ui/Containerfile" "$B/ui"
mkdir -p "$B/api" && git archive "$SHA" | tar -x -C "$B/api"   # API (whole repository is the build context)
podman build --target runtime --build-arg MIAS_BUILD_ID=$SHA -t localhost/mias-api:${SHA:0:12} -f "$B/api/Containerfile" "$B/api"
```

**What the builds check:**
- **UI:** the builder runs `npm ci`, typecheck, lint, the Vite build and the bundle audit. No source maps, no Node in
  the runtime image.
- **API:** the `runtime` target is the production image. `--target validation` builds the test image used for
  in-image regression (Phase 14B).

**Rules:**
- Tag = the 12-character commit SHA.
- OCI `org.opencontainers.image.revision` = the full SHA (set by the build arg).
- **Never `latest`, never a mutable tag in Helm.**

## 7. Image provenance (record this in the release evidence)

```bash
podman image inspect localhost/mias-ui:${SHA:0:12} \
  --format 'id={{.Id}} digest={{.Digest}} revision={{index .Labels "org.opencontainers.image.revision"}} size={{.Size}}'
```

Record: commit SHA, local image ID, local digest, revision label, size, base image digests (from the build log), and
gate results.

**Supply-chain gates** (Hardening Task 6; full workflow and rationale in `docs/hardening-image-signing-sbom.md`).
These run after the push (§8) and digest check (§9), and **before** the digest is pinned in Helm. The tools run as
digest-pinned containers. Every cosign call uses `--tlog-upload=false`, because nothing goes to the public Rekor.

1. **SBOM** of the pushed registry digest, in CycloneDX JSON:
   `syft scan registry:localhost:5005/mias/<component>@<digest> -o cyclonedx-json=<component>-sha256-<hex>.cdx.json`.
   Review it for secrets (§6 of the doc), then archive it in `~/.mias-release-evidence/sbom/`.
2. **Sign the digest:**
   `cosign sign --key ~/.mias-signing/mias-release.key --tlog-upload=false --allow-insecure-registry
   --sign-container-identity image-registry.openshift-image-registry.svc:5000/mias/<component>
   -a org.opencontainers.image.revision=$SHA -a mias.component=<component> -y localhost:5005/mias/<component>@<digest>`.
   The key stays in `~/.mias-signing/`; never copy it elsewhere or print it.
3. **Attest the SBOM:**
   `cosign attest --key … --tlog-upload=false --type cyclonedx --predicate <sbom> -y …@<digest>`.
4. **Verify in a fresh process** against the registry, using the committed public key:
   `cosign verify --key docs/release-evidence/mias-release.pub --insecure-ignore-tlog=true --allow-insecure-registry …@<digest>`,
   plus `cosign verify-attestation --type cyclonedx …`. Both must pass, showing the in-cluster identity and the
   revision annotation equal to `$SHA`.
5. **Record** the component's entry in `docs/release-evidence/image-provenance.yaml` (digest, commit, config ID,
   signature attachment, SBOM hash and counts). Commit it **together with** the Helm digest pin;
   `tests/test_supply_chain.py` fails if the pin and the manifest disagree.

## 8. Registry push (internal registry, no external route)

```bash
AUTH=$(mktemp -u)/auth.json; mkdir -p "$(dirname "$AUTH")"
oc port-forward -n openshift-image-registry svc/image-registry 5005:5000 >/dev/null 2>&1 & PF=$!
oc whoami -t | podman login --authfile "$AUTH" --tls-verify=false -u "$(oc whoami)" --password-stdin localhost:5005
podman push --authfile "$AUTH" --tls-verify=false localhost/mias-ui:${SHA:0:12} localhost:5005/mias/mias-ui:${SHA:0:12}
kill $PF; rm -f "$AUTH"                                  # always remove the auth file
```

- The token goes through stdin only. Never echo it, write it to the shell history, or keep the auth file.
- `--tls-verify=false` is only for the local port-forward to the in-cluster registry.
- ImageStreams: `mias/mias-api`, `mias/mias-ui` and `mias/otel-collector-contrib`. A new ImageStream should be
  created explicitly, with the labels `app.kubernetes.io/part-of=mias`.

## 9. Digest verification

```bash
oc get istag mias-ui:${SHA:0:12} -n mias -o jsonpath='digest={.image.metadata.name}
revision={.image.dockerImageMetadata.Config.Labels.org\.opencontainers\.image\.revision}
configId={.image.dockerImageMetadata.Id}
'
```

Required, or stop:
- `revision` equals `$SHA`;
- `configId` equals the local image ID;
- the **registry digest** (which differs from the local digest because layers are recompressed) is what goes into
  `values.yaml`.

Then (after the supply-chain gates in §7) pin the digest through a git commit: `ui.image.digest` and `ui.image.versionLabel` (or `image.digest` and
`image.versionLabel` for the API), plus the matching test constant in `tests/test_helm_ui.py` (`DEPLOYED_UI_DIGEST`)
or `tests/test_helm_mias.py` (`DIGEST`).

## 10. Helm render and diff

```bash
helm lint deploy/helm/mias
helm template mias deploy/helm/mias -n mias > /tmp/render.yaml
helm get manifest mias -n mias > /tmp/live.yaml
# compare per object (kind/name), ignoring comments; review every changed field
```

**Expected rollout impact:**

| Change | Restarts |
|---|---|
| Chart version or labels only | **nothing** (since Hardening Task 2: checksums hash ConfigMap data only) |
| API ConfigMap data (`api.*`, OTLP endpoint, exporters, intervals, resource attributes) | mias-api once |
| Collector configuration (`mias.collectorConfig`, for example `traceDebugVerbosity`) | otel-collector once |
| API-only observability values | API only, not the collector |
| ServiceMonitor values, `monitoring.alerts.*`, Routes, NetworkPolicies | no pod restart |
| Image digest, resources, probes or other pod-template fields | that workload once (API and collector use Recreate, so a brief outage; UI uses RollingUpdate) |

If the diff shows anything outside the intended change, **stop**.

## 11. Server dry run

```bash
helm upgrade mias deploy/helm/mias -n mias --reset-values --dry-run=server
```

- **Always `--reset-values`.** Without it Helm silently reuses the previous revision's `--set` values (Phase 14E).
- **No `--force-conflicts`** unless the dry run reports a real server-side-apply ownership conflict. It was needed
  once (Phase 14E/15, fields co-owned by the original `kubectl apply`) and not since.

## 12. Deployment

1. **Capture the pre-deploy state:**

   ```bash
   helm list -n mias
   oc get pods -n mias -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.uid} {.status.startTime} restarts={.status.containerStatuses[0].restartCount}{"\n"}{end}' > pods-before.txt
   oc exec -n mias deploy/mias-api -- sh -c 'cd /var/lib/mias/artifacts && find . -type f | sort | xargs sha256sum' > artifacts-before.sha
   ```

2. **Upgrade:** `helm upgrade mias deploy/helm/mias -n mias --reset-values --wait --timeout 8m`, then record the new
   revision.
3. **Wait:** `oc rollout status deployment/<name> -n mias` for each workload expected to roll (§10).

## 13. Rollout verification

- **Only the expected workloads restarted:** compare pod UIDs and start times with `pods-before.txt`.
- **No rollout loops:** exactly one ReplicaSet with replicas per Deployment, and `observedGeneration` equal to
  `generation`.
- **New pods:** Ready, with 0 restarts.
- **Publisher:** still 0 replicas.
- **UI HA:** `mias-ui` 2/2 Ready, on two **different** nodes; PDB `mias-ui` shows `disruptionsAllowed` 1:
  ```bash
  oc get pods -n mias -l app.kubernetes.io/name=mias-ui -o wide          # 2 pods, both Ready, different NODE
  oc get pdb mias-ui -n mias -o jsonpath='{.status.desiredHealthy} {.status.currentHealthy} {.status.disruptionsAllowed}{"\n"}'   # 1 2 1
  ```
  While a UI rollout is in progress, OpenShift's own `PodDisruptionBudgetAtLimit` may show as pending for a few
  seconds. That's expected; it fires only after 60m at the limit.
- **Synthetic exporter** (`mias-monitoring`): Ready, 0 restarts. It restarts only when its probe configuration
  changes.

## 14. Route and TLS verification

```bash
for h in mias-ui mias-api; do
  curl -s -o /dev/null -w "$h http %{http_code} %{redirect_url}\n" http://$h.apps.<domain>/      # expect 302 to https
  curl -sk -D - -o /dev/null https://$h.apps.<domain>/health/live | grep -ci '^set-cookie'        # expect 0
  echo | openssl s_client -connect $h.apps.<domain>:443 -servername $h.apps.<domain> 2>/dev/null | openssl x509 -noout -fingerprint -sha256 -enddate
done
curl -sk -D - -o /dev/null https://mias-ui.apps.<domain>/ | grep -i -E '^(content-security-policy|x-content-type-options|referrer-policy|permissions-policy|cross-origin-opener-policy|cross-origin-resource-policy|server):'
```

**Expected:**
- the exact CSP: `default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none';
  base-uri 'none'; frame-ancestors 'none'`;
- `nosniff`, `no-referrer`, Permissions-Policy, COOP and CORP `same-origin`;
- `Server: nginx` with no version;
- the certificate fingerprint and expiry as previously recorded (a change is an incident unless a TLS change was
  planned).

## 15. Browser and UI QA

- **Unauthenticated:**
  - `https://mias-ui.apps.<domain>/healthz` returns `ok`;
  - deep links return the SPA with `no-store`;
  - `/api` returns a plain 404, never SPA HTML.
- **Real browser:** `ui/scripts/browser-qa.mjs` drives Chromium over CDP. It covers:
  - sign-in, wrong token, return path;
  - Overview, Market Intelligence list, filter and detail;
  - exact canonical text, **Copy** (clipboard equals source) and **Download** (bytes equal the API, file name
    `<hex>.json`);
  - Alerts and deliveries notice, Status;
  - theme; lock and unlock; tablet and phone drawer; no horizontal scroll at 390 px;
  - reduced motion; reload requires sign-in; no CSP violations; same-origin only; no token in URLs.

  Live mode: `MIAS_QA_URL=https://mias-ui.apps.<domain> MIAS_QA_LAB_TLS=1 NODE_EXTRA_CA_CERTS=<ingress CA>`, with
  the token in `MIAS_QA_TOKEN` (env only), `PLAYWRIGHT_CORE=<install path>`, and Chromium from
  `chromedp/headless-shell` on the host network with a shared `TMPDIR` mount for downloads. Never set
  `MIAS_QA_RESTART` (token rotation) against production without approval. The ingress CA is the
  `default-ingress-cert` ConfigMap in `openshift-config-managed`.
- **Without an approved token:** run the unauthenticated subset and rely on the last authenticated QA run. Record that
  decision.

## 16. API validation

```bash
curl -sk https://mias-api.apps.<domain>/health/live          # {"status":"live"}
curl -sk https://mias-api.apps.<domain>/health/ready         # status "ready", every check "pass"
curl -sk -o /dev/null -w '%{http_code}\n' https://mias-api.apps.<domain>/api/v1/version         # 401 without a token
curl -sk -H @- https://mias-api.apps.<domain>/api/v1/version <<<"Authorization: Bearer $T"      # 200; build = the deployed SHA
```

Through the UI proxy, `/health/*` returns 200 and `/api/v1/version` returns 401 without a token.

## 17. UWM and telemetry validation

Query Thanos read-only:

```bash
THANOS=$(oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}')
q(){ curl -sk -G -H "Authorization: Bearer $(oc whoami -t)" --data-urlencode "query=$1" "https://$THANOS/api/v1/query"; }
q 'up{namespace="mias",job="otel-collector"}'
q 'mias_artifact_index_healthy{namespace="mias",exported_job="mias/mias-api"}'
q 'mias_artifact_index_last_success_age_seconds{namespace="mias",exported_job="mias/mias-api"}'
oc logs -n mias deploy/mias-api --since=30m | grep -c -i -E 'transient error|failed to export'
q 'probe_success{namespace="mias",job="mias-ui-synthetic"}'
q 'probe_http_status_code{namespace="mias",job="mias-ui-synthetic"}'
oc get pods -n mias-monitoring
```

**Expected:**
- `up` = 1 and index healthy = 1;
- index age under 60 s (it refreshes every 30 s);
- 0 sustained export errors;
- `probe_success` = 1 and status 200 (the synthetic probe of the UI Route); the exporter pod Ready.

Cumulative counters reset when the API pod is replaced. That's normal.

## 18. Alert validation

```bash
curl -sk -H "Authorization: Bearer $(oc whoami -t)" "https://$THANOS/api/v1/rules?type=alert"   # mias.* groups: 12 rules, health "ok"
curl -sk -H "Authorization: Bearer $(oc whoami -t)" "https://$THANOS/api/v1/alerts"             # no MIAS alert pending or firing
```

- **Timing:** recheck about 15 minutes after the rollout settles, past the longest short `for` window.
- **Restarts:** a single planned restart per workload stays below `MiasPodRestarting` (more than 2 in 30m).
- **Delivery:** Alertmanager receiver delivery is **not** verified, because it's Secret-backed (see
  `docs/hardening-uwm-alerting.md`).

## 19. Artifact and storage validation

- Artifact sha256 list equal to `artifacts-before.sha`; modes `444`.
- PVC Bound with the **same UID**; PV `Retain`; PVC annotation `helm.sh/resource-policy: keep`.
- `oc get deploy mias-publisher -n mias` shows 0 replicas.

## 20. Secret and log audit

Scan the recent logs of all three pods for the token value (held in a variable and compared, never printed),
`Authorization`, `Bearer`, `cookie`, `password`, `api_key`, `secret` and canonical payload fragments (for example
`decision_trace`, `packet_pointers`). **Expect 0 everywhere.** QA logs and output files must also contain no token.

## 21. Success criteria

The release is complete when all of these hold:
- §13–§20 pass;
- only the intended objects changed;
- only the expected workloads restarted, once;
- no new alert is pending or firing after settling;
- the release evidence (§7, §9, the Helm revision, the gate results) is written in the PR or a release note.

## 22. Rollback decision criteria

Roll back (or fix forward, §23) if any of these hold:
- **Readiness:** not recovered within 5 minutes of the rollout, or a rollout loop or repeated restarts.
- **Server errors:** an unexpected increase, or `MiasApi5xxRatio` firing.
- **QA:** browser QA fails a previously passing check.
- **Telemetry:** not recovered within 15 minutes (collector `up` or index metrics absent).
- **Leakage:** any token or secret in logs, QA output or URLs. Treat as an incident and roll back immediately.
- **Artifacts:** any artifact hash or mode change, or a PVC UID change.
- **Unexpected object changes:** a Route, NetworkPolicy, Secret or PrometheusRule changed outside the release intent,
  or a rule's health is not `ok`.

## 23. Rollback procedures (in order of preference)

1. **Fix forward:** only if the cause is trivial, understood and testable. Run the same gates; no shortcuts.
2. **Image rollback through git (preferred):** revert the digest-pin commit, or pin the previous known-good digest (§25)
   in `values.yaml`, run the Helm tests, and `helm upgrade --reset-values --wait`. This keeps git as the source of
   truth, with an explicit digest, and is reproducible.
3. **`helm rollback <rev>`:** emergency only, with the caveats in §26.

## 24. Emergency UI disable (proven live in 16F)

```bash
helm upgrade mias deploy/helm/mias -n mias --reset-values --set ui.enabled=false --wait
```

- **Removes only** the UI objects: Deployment, Service, Route, ServiceAccount, ConfigMap, `mias-ui-allow-router`,
  `mias-ui-egress-api`, `mias-api-allow-ui` and the PDB `mias-ui`.
- **Also drops:** the `MiasUiUnavailable` rule, and the synthetic probe objects with their alert.
- **Untouched:** the API, collector, PVC, PV and ImageStream (no restart).
- **Restore:** a plain `helm upgrade mias deploy/helm/mias -n mias --reset-values --wait` from the committed defaults,
  then re-verify (§13–§19).
- **Alerts only:** to remove just the alert rules, use `--set monitoring.alerts.enabled=false`.

## 25. Image rollback

Before every release, confirm the previous known-good digest **still exists**:

```bash
oc get istag -n mias
```

Then roll back by pinning that digest through git (§23.2). Don't prune any tag that's referenced by:
- the current or previous release pin;
- any revision still in Helm history: `helm history mias -n mias`, then
  `helm get manifest mias -n mias --revision <n> | grep @sha256`.

The cluster image pruner keeps only tagged images (`keepTagRevisions=3`), so deleting a tag can make its image
unrecoverable.

**Signatures and SBOMs are retained with their image:**
- each signed digest has `sha256-<digest>.sig` and `.att` tags in the same ImageStream;
- never prune those while the image is kept, and remove them together with it;
- keep the archived SBOM and the manifest entry as long as the image exists;
- never prune by tag age alone, and check Helm history first.

**Before any signature enforcement** (an ImagePolicy), every rollback digest must be signed too. See
`unsignedRetained` in `docs/release-evidence/image-provenance.yaml`.

## 26. Helm rollback caveats

- **Old state:** `helm rollback` restores that revision's values and chart, including old labels and checksum
  algorithms, so it can restart workloads.
- **Missing images:** it fails, or leaves pods in ImagePullBackOff, if the revision's image was pruned. Verify the
  images first (§25).
- **Git drift:** it takes the cluster away from git. Reconcile git afterwards by pinning the same state and upgrading
  normally.
- **History limit:** Helm keeps 10 revisions; older revisions can't be rolled back to.

## 27. PVC and PV safety

**NEVER delete the `mias-artifacts` PVC or its PV.** They hold the sealed, content-addressed artifacts.

**Safeguards:**
- Helm `resource-policy: keep`, so uninstall and rollback leave the PVC;
- PV `Retain`;
- the publisher writes only through `artifact_store.runner`;
- artifact files are mode 0444.

**Never:**
- `oc delete pvc/pv`;
- `helm uninstall` without an explicit plan;
- change the storage class or access mode in place;
- copy files into the store path manually.

**Capacity:** expand only by a PVC size increase (`thin-csi` allows expansion), planned. `MiasArtifactPvcFilling`
warns at 80%.

## 28. Publisher safety

- `mias-publisher` is an operator toolbox: **0 replicas** except while publishing. It uses required pod affinity to
  the API's node (RWO volume).
- **Publish procedure** (Phase 14D): scale to 1; `oc cp` to `/tmp` only; `python -m artifact_store.runner publish
  --kind <kind> --file /tmp/...`; `verify`; scale to 0.
- **Never** scale the publisher during a release unless the release explicitly includes publishing.
  `MiasPublisherLeftRunning` fires after 2 hours above 0.

## 29. Post-release checklist

- [ ] Only the intended objects changed (render diff matched the live result).
- [ ] Only the expected workloads restarted, once; no loops; publisher 0.
- [ ] Both Routes: HTTPS works, HTTP returns 302, no `Set-Cookie`, headers and CSP exact, certificate as expected.
- [ ] API health, ready and version checked; UI `/healthz` and browser QA passed (or the documented subset).
- [ ] Telemetry: collector `up`, index healthy, no export errors.
- [ ] Alerts: 12 rules `ok`, nothing pending or firing after settling.
- [ ] Synthetic: `probe_success` = 1 and status 200 for `job="mias-ui-synthetic"`; blackbox-exporter Ready, 0 restarts.
- [ ] UI HA: `mias-ui` 2/2 Ready on different nodes; PDB `disruptionsAllowed` = 1; both pods receive traffic.
- [ ] Logs clean (no tokens or secrets); artifacts, PVC and PV unchanged.
- [ ] Release evidence recorded; the previous digest's tag kept for rollback.
- [ ] Supply chain, re-checked after the push and the pin:
  - `cosign verify` and `verify-attestation` pass against the registry for the deployed digest;
  - the registry config ID equals the local image ID, and the OCI revision equals the commit;
  - `tests/test_supply_chain.py` passes.

## 30. Operational checklists

**Daily:**
- pods Ready, no new restarts, publisher 0;
- `mias-ui` **2/2** Ready on two different nodes. With 1/2 the UI still serves (no MIAS alert), but redundancy is gone:
  investigate.
- `/health/ready` passes all checks;
- no MIAS alerts firing; index healthy;
- synthetic probe green: `probe_success{namespace="mias",job="mias-ui-synthetic"}` = 1 (the routed UI path works);
- PVC Bound and under 80%.

**Before deployment:**
- clean git, HEAD equal to origin/main;
- gates green for the change type (§5);
- image built from the exact commit, with provenance recorded and the digest verified;
- SBOM generated and attested, digest signed, signature verified from the registry in a fresh process, and
  `image-provenance.yaml` updated (§7);
- the previous rollback digest exists;
- render diff reviewed; server dry run clean;
- persistent services recorded.

**After deployment:** §29.

**Monthly:**
- certificate and CA expiry (the lab wildcard currently expires 2028-09-11; verify);
- PVC usage, and registry storage (the internal registry PVC is unmanaged);
- stale image tags against Helm history (§25);
- alert noise and missed incidents; Alertmanager receiver verification status;
- read-token rotation review;
- `oc` and OAuth session expiry awareness;
- confirm that this runbook still matches reality.

## 31. Known limitations (not solved here)

- **TLS:** self-signed lab TLS (an ingress-operator CA), and no HSTS.
- **Auth:** a shared static read token; the public `mias-api` Route still exists alongside the UI proxy.
- **Single replicas:**
  - the API is on RWO block storage, so there's no API HA; the UI depends on it for data;
  - the collector is single replica;
  - the UI is HA (2 replicas, PDB, spread; `docs/hardening-ui-ha.md`), but no MIAS alert covers degraded UI capacity
    (1 of 2).
- **No preStop drain delay on UI pods.** A request that reaches a pod just after it gets SIGTERM can fail. No such
  failure has been observed (`docs/hardening-ui-ha.md` §7).
- **Lab client path:** the external `*.apps` front end (`192.168.1.60`) occasionally drops a TCP SYN, costing 1–5 s
  at connect. This is independent of MIAS: it happens before TLS and the Host header. Use client timeouts of at
  least 10 s when measuring continuity from WSL.
- **No central backends:** no trace backend (the collector's debug exporter only), and no central log backend.
- **Monitoring gaps:** Alertmanager receivers are unverified; no inhibition rules.
- **Synthetic monitoring:**
  - the probe skips TLS verification (lab self-signed certificate);
  - it runs from inside the cluster, through the ingress VIP, so the external `*.apps` front end (`192.168.1.60`) and
    users' DNS aren't covered;
  - the ingress VIP is pinned in values;
  - certificate expiry is collected but not alerted;
  - only the UI Route is probed.

  See `docs/hardening-synthetic-monitoring.md` §9.
- **Supply chain:**
  - MIAS images are signed (cosign key, no transparency log) and have signed CycloneDX SBOMs;
  - there's **no enforcing ImagePolicy** (OpenShift has no audit mode);
  - rollback digests are unsigned;
  - one lab key with no KMS, no SLSA build provenance, and no vulnerability scan.

  See `docs/hardening-image-signing-sbom.md` §15.
- **Validation scripts:** the Podman matrix and real-API end-to-end scripts aren't committed (§5).
- **Leftover annotation:** the `mias-api` Route still carries a `kubectl.kubernetes.io/last-applied-configuration`
  annotation from Phase 14 (harmless).

## 32. Commands reference (read-only unless marked)

| Purpose | Command |
|---|---|
| Release state | `helm list -n mias`; `helm history mias -n mias` |
| Workloads | `oc get deploy,pods,rs -n mias` |
| Deployed images | `oc get deploy -n mias -o jsonpath='{range .items[*]}{.metadata.name} {.spec.template.spec.containers[0].image}{"\n"}{end}'` |
| Running image IDs | `oc get pods -n mias -o jsonpath='{range .items[*]}{.metadata.name} {.status.containerStatuses[0].imageID}{"\n"}{end}'` |
| Image tags and digests | `oc get istag -n mias` |
| Routes | `oc get route -n mias -o yaml` (spec and annotations only; no Secrets) |
| NetworkPolicies | `oc get networkpolicy -n mias` |
| Storage | `oc get pvc -n mias`; `oc get pv <volume> -o jsonpath='{.spec.persistentVolumeReclaimPolicy}'` |
| Alert rules | `oc get prometheusrule mias-alerts -n mias`; Thanos `/api/v1/rules?type=alert` |
| UI HA | `oc get pods -n mias -l app.kubernetes.io/name=mias-ui -o wide`; `oc get pdb mias-ui -n mias` |
| Synthetic probe | `oc get probe -n mias`; `oc get deploy,pods,networkpolicy -n mias-monitoring`; Thanos `probe_success{namespace="mias",job="mias-ui-synthetic"}` |
| **Synthetic probe off (mutating)** | `helm upgrade … --reset-values --set syntheticMonitoring.enabled=false --wait` (no MIAS workload restarts) |
| Render | `helm template mias deploy/helm/mias -n mias`; `helm get manifest mias -n mias` |
| Dry run | `helm upgrade mias deploy/helm/mias -n mias --reset-values --dry-run=server` |
| **Deploy (mutating)** | `helm upgrade mias deploy/helm/mias -n mias --reset-values --wait --timeout 8m` |
| **Emergency UI off (mutating)** | `helm upgrade … --reset-values --set ui.enabled=false --wait` |
| **Publisher on/off (mutating)** | `oc scale deployment/mias-publisher --replicas=1` (or `0`) `-n mias` |

**History and evidence:**
- `docs/phase14b-container.md` through `phase14f-final-openshift-validation.md`;
- `phase15-observability.md`;
- `phase16a…phase16f`;
- `hardening-uwm-alerting.md`, `hardening-restart-cookie.md`, `hardening-synthetic-monitoring.md`, `hardening-ui-ha.md`, `hardening-image-signing-sbom.md`.
