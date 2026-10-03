# Phase 16F: Final UI validation, first live deployment, and Phase 16 closure

The MIAS dashboard (`mias-ui`) is now live on the lab OpenShift cluster at **https://mias-ui.apps.ngc.sirii.org**.
It's a digest-pinned image built from an exact commit, deployed with the existing chart. It runs under restricted-v2
with no service-account token. It's isolated by NetworkPolicies proven allow and deny on the live cluster. Its
same-origin proxy to mias-api, auth, canonical copy and download, and full UI were validated in a real browser over
HTTPS. Rollback was exercised live.

Live QA found one real bug: horizontal page scroll at phone width. It was fixed minimally, re-gated, rebuilt,
re-pushed and redeployed.

## Starting point (read-only discovery)

| | |
|---|---|
| Git | `main` = `origin/main` = `71dbb3de09d9b4888887d89d60217c260bc0e806`, clean tree |
| Cluster | ngc-282t4, OpenShift 4.21.15. The server reports Kubernetes **v1.34.6** (the prompt said 1.34.7). |
| Helm | `mias` revision **15**, chart `mias-0.2.0`, no user-supplied values (the defaults are the deployed state) |
| Workloads | mias-api 1/1 (`sha256:634c5372…`), otel-collector 1/1 (`sha256:2d756157…`), mias-publisher **0/0** |
| UI | none: no Deployment, Service, Route, ServiceAccount or ImageStream |
| NetworkPolicies | `mias-default-deny`, `mias-api-allow-router`, `mias-api-egress-telemetry`, `otel-collector-ingress` (specs recorded) |
| Storage | PVC `mias-artifacts` Bound to `pvc-8ffa03dd…` (Retain); 2 artifacts (1 MI, 1 alert), sha256 recorded |
| Secret | `mias-api-auth` resourceVersion 6143238 (metadata only) |
| Telemetry | UWM `up{namespace="mias"}=1`, `mias_artifact_index_healthy=1` |

## Image build and provenance

Each image was built with Podman from an exact `git archive <commit> ui` (no working-tree content) using
`ui/Containerfile`:
- builder: UBI 9 nodejs-22 `@sha256:f7a0b11c…d869`;
- runtime: UBI 9 nginx-124 `@sha256:dd82c493…4148`.

The builder runs `npm ci`, typecheck, lint, the Vite build and the bundle audit. The frontend tests ran on the same
archived tree.

| | First image | **Deployed image** (after the live-QA fix) |
|---|---|---|
| Commit (OCI `revision` label) | `71dbb3de09d9b4888887d89d60217c260bc0e806` (merged main) | `7c344762fbdd78f829cc7c58b05ad2dab825c368` (this branch: main + the fix) |
| Local image ID | `d4032cfa3aa7…345c` | `688f7e152391…b4ff` |
| Local manifest digest | `sha256:fdc3c083…c5a0` | `sha256:fcd46e9a…a6ac` |
| **Registry digest** | `sha256:b311627d841358fbdafb4abbdc486e09100925b8c7392886c807bac1d94e896d` | **`sha256:03782545d6b876dd178a8a59e308593215e7c9326a67d71d8ecd81eed754ce36`** |
| Registry config ID = local image ID | yes | yes |
| Size | about 339 MB (UBI nginx base 338 MB) | about 339 MB |
| Tests on the archived tree | 299/299 | 303/303 |

Registry and local manifest digests differ because layers are recompressed on push. Provenance is proven by the
registry's config ID equalling the local image ID, plus the revision label.

**Push** used the Phase 14 procedure: a temporary `oc port-forward` to `svc/image-registry`, and `oc whoami -t` piped
to `podman login --password-stdin` into a temporary auth file that was deleted after each push. ImageStream
`mias/mias-ui` was **created explicitly** (labels `app.kubernetes.io/name=mias-ui`, `part-of=mias`) before the first
push. Tags `71dbb3de09d9` and `7c344762fbdd` exist; **deployment is always by digest**.

## Local validation before each push

| | First image | Deployed image |
|---|---|---|
| Podman matrix | 71/71 | 71/71 |
| Real-API end-to-end | 22/22 | 22/22 |
| Real-browser QA (local) | 50/50 | 54/54 (with the new phone-width checks) |

## Chart version decision

**Bumped `mias` 0.2.0 → 0.3.0**: a material new live component is now part of the chart. `appVersion` stays
`259236684a74`, because it tracks the unchanged API image.

`values.yaml` now **defaults to the deployed state**, like the API digest: `ui.enabled: true`, the pinned registry
digest, and `ui.image.versionLabel: 7c344762fbdd`. So `helm upgrade --reset-values` with no `--set` reproduces it,
and rollback is `--set ui.enabled=false`.

**Finding:** the chart version label also feeds mias-api's `checksum/config` annotation, which is the sha256 of its
rendered ConfigMap, labels included. So the 0.3.0 upgrade **restarted mias-api once**: single replica, Recreate,
seconds. The tests prove the `ui.enabled=false` render equals Phase 15 except for those two label-derived values, and
that rollback removes exactly the UI objects. A data-only checksum would avoid restarts on future metadata bumps; it's
listed for final hardening.

## Pre-deploy render and Helm upgrades

**Default render:**
- 4 ConfigMaps, 4 Deployments, 7 NetworkPolicies, 1 PVC, 2 Routes, 3 Services, 4 ServiceAccounts, 1 ServiceMonitor;
- **no** Secret, Role, RoleBinding, HPA, `ipBlock`, `0.0.0.0/0`, `privileged`, `hostNetwork`, `runAsUser`, token or
  mutable tag;
- all three images pinned by digest.

**Diff against the live revision 15:**
- 8 new UI objects;
- 0 removed;
- the chart label on every object;
- the mias-api checksum (the ServiceMonitor showed a whitespace-only change).

`helm upgrade --dry-run=server` reported **no SSA field conflicts**, so `--force-conflicts` was **not** used.

| Revision | Command | Effect |
|---|---|---|
| 16 | `helm upgrade mias deploy/helm/mias -n mias --reset-values --wait` | UI deployed with the first image; mias-api restarted once (the checksum) |
| 17 | the same, with the fixed digest in `values.yaml` | the UI rolled to the fixed image; the API pod was **not** restarted |
| 18 | `… --reset-values --set ui.enabled=false --wait` | **rollback test** |
| **19** | `… --reset-values --wait` | **restore**, the final state |

## Live verification

**Rollout:** mias-ui 1/1 Running and Ready, 0 restarts; `oc rollout status` succeeded. mias-api 1/1, collector 1/1,
publisher 0/0.

**Security context (live pod):**
- **SCC and identity:** `restricted-v2`; UID **1000750000**, from the namespace range (assigned by the SCC, not the
  chart); gid 0.
- **Container settings:**
  - `runAsNonRoot: true`, `allowPrivilegeEscalation: false`;
  - capabilities drop `ALL` (CapEff `0000000000000000`), NoNewPrivs 1;
  - seccomp `RuntimeDefault`;
  - `readOnlyRootFilesystem: true`.
- **Filesystem:** writing to `/opt` and `/etc` is refused (read-only); only the `/tmp` emptyDir is writable.
- **Service account:** `automountServiceAccountToken: false`, and `/var/run/secrets/kubernetes.io/serviceaccount` is
  absent; `enableServiceLinks: false`.
- **Probes and resources:** startup, liveness and readiness all on `/healthz`; resources 10m/32Mi requested,
  200m/128Mi limit.

**Route and TLS:**
- Route `mias-ui`: host `mias-ui.apps.ngc.sirii.org`, edge with Redirect, wildcard None, admitted.
- `http://…` returns **302** to `https://…`.
- TLS 1.3 with the lab's self-signed `*.apps.ngc.sirii.org` ingress certificate (a known lab limitation).

**DNS / hosts:**
- In WSL, `mias-ui.apps.ngc.sirii.org` resolves to **192.168.1.60** through the lab DNS in `/etc/resolv.conf`
  (`nameserver 192.168.1.60`). That server answers `*.apps.ngc.sirii.org`; a nonexistent name resolves too. No client
  change was needed or made, and cluster DNS was not altered.
- The Windows hosts file has a `mias-api` entry but not `mias-ui`. For a Windows browser that doesn't use the lab DNS,
  add `192.168.1.60  mias-ui.apps.ngc.sirii.org` to `C:\Windows\System32\drivers\etc\hosts` (admin). This was not done
  here.

**Security headers (live Route):**
- `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src
  'none'; base-uri 'none'; frame-ancestors 'none'` (unchanged and strict)
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`
- `Cross-Origin-Opener-Policy: same-origin`
- `Cross-Origin-Resource-Policy: same-origin`
- `Server: nginx` (no version)
- `index.html`: `no-store`

The OpenShift router adds its default sticky-session cookie, as on the API Route. It isn't an auth cookie: the UI's
fetches use `credentials: "omit"` and nginx sends `Cookie ""` upstream.

## Proxy and auth

All checks went through the UI Route. The token was held in memory only and passed on curl's stdin, never in argv,
files or output.

- **Health:** `/health/live` and `/health/ready` return 200, proxied.
- **Authorization forwarded:** `GET /api/v1/version` with the token returns **200** with the real API build
  `259236684a745d9e…`.
- **Request id:** `X-Request-ID: ui-16f…a1` was echoed in the response **and** appears as `request_id` in the
  mias-api JSON access log.
- **Errors pass through intact:**
  - a wrong token returns the 401 `unauthorized` envelope, carrying the sent request id;
  - no token returns 401;
  - an unknown id returns the 404 envelope;
  - latest with no symbol returns the 400 envelope;
  - an unknown `/api/v1/…` path returns 404 `application/json` — the SPA never masks API errors.
- **Restrictions:** POST returns 403 (`limit_except GET`); `/api` returns a plain-text 404 (not SPA).
- **Live nginx config:** `proxy_set_header Cookie ""`; resolver `172.30.0.10` (from the pod's resolv.conf); upstream
  `mias-api.mias.svc.cluster.local:8080`.

## NetworkPolicy proofs (live)

| Path | Expected | Observed |
|---|---|---|
| router → mias-ui:8080 | allow | the Route serves 200 |
| mias-ui → mias-api:8080 | allow | 200 (`/health/live`); proxy works |
| mias-ui → cluster DNS (5353) | allow | `mias-api.mias.svc.cluster.local` resolves |
| mias-api ← mias-ui | allow | proxied 200 |
| mias-ui → otel-collector :4318 and :8889 | deny | connection fails (000) |
| mias-ui → Kubernetes API (`kubernetes.default.svc`, 172.30.0.1:443) | deny | 000 |
| mias-ui → thanos-querier (openshift-monitoring) :9091 | deny | 000 |
| mias-ui → console (openshift-console) :443 | deny | 000 |
| mias-ui → ingress VIP 192.168.1.60:443 | deny | 000 |
| mias-ui → internet (1.1.1.1, example.com, registry.access.redhat.com) | deny | 000 |
| random unlabelled pod in `mias` → mias-ui (Service and pod IP) | deny | 000 |
| random pod → mias-api | deny | 000 |
| random pod → Kubernetes API (**positive control**) | reachable | 200 |

The "random pod" was a temporary restricted pod (`mias-qa-netpol`), deleted after each use; none remain. The proofs
were repeated after the rollback and restore. The four baseline policy specs were compared with the recorded
baseline: **unchanged**.

## Health and probes

- `/healthz` is static in nginx and answers 200 `ok`.
- The probes are healthy (0 restarts) through every rollout.
- `/healthz` staying 200 while the API is down was proven locally (Podman matrix: "healthz 200 while API down"). The
  live API wasn't deliberately disrupted.

## Live browser QA over HTTPS

**Method:**
- **Driver:** `ui/scripts/browser-qa.mjs` drives real Chromium 151 (`chromedp/headless-shell@sha256:5f877a2a…`, Podman,
  host network) over CDP with `playwright-core` 1.63.0 (in a scratch directory; not a project dependency).
- **Target:** `https://mias-ui.apps.ngc.sirii.org`. The browser context accepts the lab's self-signed certificate
  (`MIAS_QA_LAB_TLS=1`); Node verifies the Route with the cluster's ingress CA (`NODE_EXTRA_CA_CERTS`).
- **Scope:** run without token rotation, as instructed.

**Result: 51/51 on revision 17, and 51/51 again on the final revision 19.**

- **Sign-in:**
  - deep link → `/signin`;
  - a wrong token gets "Invalid or expired read token", with focus back in the field;
  - the right token returns to `/status`.
- **Status:** the API's 3 readiness checks, live and ready, builds.
- **Market Intelligence:**
  - history rows equal the API's own history;
  - filter in the URL;
  - detail;
  - the canonical tab shows the exact API text, and the ETag matches the id.
- **Copy Canonical:** the clipboard holds the exact canonical text, over HTTPS.
- **Download Canonical:**
  - same-origin `blob:` URL; file `a5363431…7415.json`;
  - the bytes equal the API canonical **and** the artifact file on the PVC (sha256 `85ebe206…`);
  - no token in the name or URL.
- **CSP:** strict and unchanged, with **no violations**.
- **Alerts:** list and detail; "Delivery information is not available in this deployment."
- **Theme:** Dark and Light applied; System follows `prefers-color-scheme`; only `mias-ui-theme` in localStorage;
  sessionStorage and cookies empty.
- **Lock:** no `main` or navigation; no API requests while locked; a wrong token stays locked; the same token unlocks
  to the same page.
- **Tablet 820 and phone 390:** Menu drawer, focus in, inert background, Escape returns focus, choosing a page focuses
  its heading.
- **Phone layout:** no page-level horizontal scroll on Overview, MI, Alerts or Status (new check).
- **Reduced motion:** the drawer animation is `none`.
- **Session refresh:** a reload requires sign-in again; signing in again works.
- **Hygiene:**
  - no console errors;
  - every request same-origin;
  - no token in any URL;
  - `Authorization` only on `/api/v1/*`;
  - the token appears in no QA log or output file.

## Live visual QA

28 screenshots per run at 2560×1440 and 1920×1080 (light and dark), 820×1180 and 390×844 (light and dark). They
cover sign-in, Overview, the MI list, detail and canonical, Alerts and alert detail, Status, lock, the drawer and
after-reload. A programmatic sweep of 8 views at 4 widths was also run.

**Finding, fixed:** at 390 px, Overview (450 px wide) and Status (504 px) scrolled horizontally. Three tables were not
inside the `.table-wrap` scroll container:
- Overview "Artifacts by kind";
- Status "Readiness checks";
- Status "Analytical formats".

They're now wrapped (`0f16039`), with a component test that every table on Overview, Status, MI and Alerts is inside
a scroll wrapper. The test fails on the old pages and passes on the fix. After the redeploy the sweep shows **32/32
views with no page-level horizontal scroll**.

Icons render (the 16E inline SVG), and nothing was clipped.

## Log and secret audit

Full logs of all three pods were scanned:

| Pod | Lines | token value | Authorization / Bearer | cookie / password / api_key / secret | canonical payload fragments | cookie canary |
|---|---|---|---|---|---|---|
| mias-ui | 84 | 0 | 0 | 0 | 0 | 0 |
| mias-api | 131 | 0 | 0 | 0 | 0 | 0 |
| otel-collector | 1556 | 0 | 0 | 0 | 0 | 0 |

mias-ui access logs are JSON (request id, method, path without query, status, bytes, duration, upstream status). The
Secret was never printed, its resourceVersion is unchanged, and no token was written to any file.

## Telemetry impact

- UWM: `up{namespace="mias"}=1` and `mias_artifact_index_healthy=1`.
- Request metrics are flowing by route. The cumulative counter restarted with the API pod at revision 16.
- Collector Ready with 0 restarts; 0 API export errors.
- UI → collector is denied by policy, and the UI has no browser telemetry.

## API and artifact safety

- **Artifacts:** both files are byte-identical to the baseline (sha256) and still mode 0444.
- **Publisher:** still 0 replicas.
- **Storage:** PVC Bound to the same UID, with `helm.sh/resource-policy: keep`; PV `Retain`/Bound.
- **UI mounts:** the UI mounts only an emptyDir `/tmp`.
- **API Route:** spec unchanged; only its `helm.sh/chart` label moved to 0.3.0.

## Rollback proof (live)

1. **Revision 18** (`--set ui.enabled=false`): exactly the 8 UI objects were removed (Deployment, Service, Route,
   ServiceAccount, ConfigMap, and the `mias-ui-allow-router`, `mias-ui-egress-api` and `mias-api-allow-ui` policies).
   - mias-api and the collector pods kept the **same UIDs** (no restart).
   - The API Route reports ready (200), and the UI hostname gets the router's 503.
   - The PVC, PV, publisher and ImageStream tags are unchanged.
2. **Revision 19** (plain `--reset-values` from defaults): the UI came back on the same digest, Ready.
   - The API and collector were still not restarted.
   - Isolation was re-proven, and live browser QA passed 51/51.

## Final live topology

```
https://mias-ui.apps.ngc.sirii.org ─(router, edge TLS)─▶ mias-ui :8080 ─(same-origin proxy)─▶ mias-api :8080
https://mias-api.apps.ngc.sirii.org ─(router, edge TLS)─▶ mias-api :8080 ─OTLP─▶ otel-collector ◀─ UWM scrape
```

| | State |
|---|---|
| Helm | `mias` revision **20** (19 before the provenance reconciliation), chart **0.3.0**, appVersion 259236684a74, deployed |
| mias-api | 1/1 Ready, `sha256:634c5372…` |
| mias-ui | 1/1 Ready, **`sha256:8c4e9341…8cd0`** (built from merged main `90effc1778a7`; earlier `sha256:03782545…ce36`) |
| otel-collector | 1/1 Ready |
| mias-publisher | 0 |
| Routes | `mias-api` (unchanged spec), `mias-ui` (edge with Redirect) |
| NetworkPolicies | the 4 baseline (specs unchanged) plus `mias-ui-allow-router`, `mias-ui-egress-api`, `mias-api-allow-ui` |
| PVC / PV | Bound / Retain |
| ImageStream `mias-ui` | tags `7c344762fbdd` (deployed) and `71dbb3de09d9` (superseded, kept for audit) |

## Repository changes in 16F

| Commit | Change |
|---|---|
| `75a29b7` | Chart 0.3.0; UI enabled by default with the first digest; Helm tests (rollback render equals Phase 15 except the label-derived values; the default deploys the pinned image) |
| `0f16039` | **Live-QA fix:** wrap three tables (Overview, Status) in `.table-wrap`; layout regression test |
| `7c34476` | `browser-qa.mjs`: lab TLS option, a reload check without rotation, API-relative row counts, dark 1440p captures, a phone-width overflow check (QA tooling only) |
| `bf16a20` | Pin the fixed image digest `sha256:03782545…` |
| (this doc) | Phase 16F documentation |

## Test results

| Gate | Result |
|---|---|
| Frontend (clean `npm ci`): typecheck / lint / tests / build / bundle audit | ok / 0 problems / **303/303** (299 + 4 layout tests), no MSW noise / ok / **PASS** |
| Dependencies | `package.json` and lockfile unchanged |
| Helm and manifest suites | 38 passed |
| Focused Python (Helm, manifests, API integration, foundation, observability) | 102 passed |
| Full Python regression (disposable PostgreSQL and Redis, 4 exclusions) | 2344 passed, 7 skipped, **2 failed**: both known baselines (Fed runpy, SEC argv); the intermittent geopolitical test passed. No new failures. Persistent `mias-postgres` and `mias-redis` unchanged (same ids and start times). |
| Protected areas | Phase 6 hashes verified; migration head `0007_technical_evidence_ledger`; no changes to API, artifact store, scientific packages, migrations or `deploy/openshift` |
| `git diff --check 71dbb3d..HEAD` | clean |

## Remaining gaps (carried to final integration and hardening)

- **TLS:** the lab ingress uses a self-signed certificate, and DNS relies on the lab resolver's wildcard (Windows
  needs a hosts entry).
- **Router cookie:** the default sticky cookie is set on both Routes. Consider
  `haproxy.router.openshift.io/disable_cookies: "true"` (single-replica backends don't need stickiness).
- **Chart checksum:** mias-api's `checksum/config` hashes labels too, so chart-version bumps restart the API. Hash only
  the ConfigMap `data`.
- **Availability:** single replicas everywhere. The API restart at revision 16 meant a brief API outage (Recreate
  strategy, seconds) during the rollout.
- **Auth:** interim auth is a shared static read token. OAuth/OIDC is future work.
- **Browser coverage:** live browser QA covered Chromium only.
- **Disruptive tests not run live:** `/healthz` independence from the API was proven locally, not by disrupting the
  live API, and token rotation was not run live.
- **Housekeeping:** the superseded ImageStream tag `mias-ui:71dbb3de09d9` remains, and can be pruned.

## Final provenance reconciliation

After the 16F branch was merged, the live image was rebuilt from the **exact merged main commit** and redeployed. This
makes the running UI provably originate from main.

| | Value |
|---|---|
| A. Merged `main` | `90effc1778a7612e8a708194644b46b79db09641` |
| Rebuilt image (local ID) | `9443160dcfa7a9d3d708f002aa72c17f7d6bcec485d58e41cc4ce4cefae6bd59` (local digest `sha256:08d38242…9505`) |
| B. Image config OCI revision | `90effc1778a7612e8a708194644b46b79db09641` |
| C. Immutable registry digest | `sha256:8c4e93412a62f3d9d9e696e531b56afa7847959e55a3c774ea85fe0dd4988cd0` (ImageStream tag `mias-ui:90effc1778a7`; registry config ID equals the local image ID) |
| Chart pin (`ui.image.digest`) | the same digest; `versionLabel: 90effc1778a7` (commit `0453f33`: digest and test constant only) |
| Helm revision | **20** (`helm upgrade --reset-values --wait`; server dry run showed no ownership conflicts, so no `--force-conflicts`) |
| D. Deployment `mias-ui` image | `…/mias/mias-ui@sha256:8c4e9341…8cd0` |
| E. Running pod imageID | `…/mias/mias-ui@sha256:8c4e9341…8cd0` |

**Result: A = B and C = D = E.**

**Build gates:**
- in-build: typecheck, lint, Vite build, bundle audit PASS;
- Podman matrix 71/71 and end-to-end against the real API image 22/22 (local random tokens);
- frontend tests 303/303 on 3 reruns. The first run had one transient failure: the 16E session-ended test's synchronous
  `toHaveFocus()` assertion is timing-sensitive. It was not changed here (no functional changes allowed); wrapping it in
  `waitFor` is a test-hardening follow-up.

**Focused live checks (revision 20), all passed:**
- **Pods:** mias-ui 1/1 Ready, 0 restarts; mias-api and the collector Ready, 0 restarts, **not restarted by this
  upgrade** (same pod UIDs); publisher 0.
- **Storage:** PVC Bound (same UID, `keep`); PV Retain.
- **Route:** `mias-ui` admitted, edge with Redirect; HTTPS 200; HTTP 302 to HTTPS.
- **Health:** `/healthz` returns `ok`; `/health/live` and `/health/ready` return 200 through the proxy.
- **Headers:** all six security headers present with the exact CSP; `Server: nginx`.
- **Policies:** 7 NetworkPolicies. The 3 UI policies are at generation 1 with specs equal to the chart render; the 4
  baseline specs equal the recorded baseline.
- **Cleanup:** no temporary resources remain.
- **Artifacts:** both artifacts byte-identical to the 16F baseline, mode 0444.
- **Telemetry:** UWM `up=1`, index healthy `=1`, request metrics flowing; collector 0 restarts; 0 API export errors.

**Secrets:** **no Secret value was read** during this reconciliation. Only unauthenticated endpoints were exercised,
plus `oc` metadata and the user's own `oc whoami -t` piped into a temporary registry login that was deleted after the
push.

The `oc` session expired mid-task (`Unauthorized`). The user re-authenticated as `mias-admin` before the push; no other
identity was used.

**Browser QA:** authenticated browser QA was **not repeated**, because no externally supplied token was available and
reading the cluster Secret was out of scope. Coverage relies on the completed revision-19 live QA:
- 51/51, twice;
- Copy and Download Canonical proven over HTTPS;
- visual sweep 32/32;
- rollback proven.

The rebuilt image comes from the same source as the revision-19 image, plus documentation and chart pins only.

The superseded ImageStream tags `71dbb3de09d9` and `7c344762fbdd` remain for audit, and can be pruned.

## Phase 16 closure statement

Phase 16 (16A–16F) delivered a read-only MIAS dashboard from architecture to live deployment:
- the same-origin proxy and memory-only auth model;
- Market Intelligence and Alert views with exact canonical artifacts;
- operational status from the API's own endpoints;
- hardened session, lock, theme and navigation UX;
- a digest-pinned, restricted, policy-isolated OpenShift deployment, validated in a real browser over HTTPS, with
  rollback proven live.

All 16F closure criteria are met, and the live deployment provably runs the image built from merged main
`90effc1778a7` (Helm revision 20). **Phase 16 is complete.**

## Final integration and hardening handoff

1. **TLS and DNS:** issue a trusted certificate for `*.apps.ngc.sirii.org` or per-Route certificates, and publish
   proper DNS records. Then re-run `browser-qa.mjs` without `MIAS_QA_LAB_TLS`.
2. **Router cookies:** add `haproxy.router.openshift.io/disable_cookies: "true"` to both Routes (a chart change), and
   re-verify headers.
3. **Restart-free metadata changes:** make mias-api's `checksum/config` hash only the ConfigMap `data`.
4. **Availability:** decide on replica counts and PodDisruptionBudgets. The API needs an RWX or read-only volume
   strategy before scaling.
5. **Auth:** plan OAuth/OIDC, or an authenticating proxy, to replace the shared read token; keep it memory-only in the
   meantime.
6. **Supply chain:** sign images (cosign or Podman signatures) and verify them in the cluster; prune the superseded
   `mias-ui:71dbb3de09d9` tag.
7. **Monitoring:** add UWM alert rules for mias-api readiness, the index-healthy gauge and collector `up`, plus a
   synthetic check of `https://mias-ui…/healthz`.
8. **Continuous QA:** run `browser-qa.mjs` (live mode) and the Podman matrix on each UI release. A token-rotation drill
   needs explicit approval.
