# Phase 16B: Dashboard skeleton and API client

Phase 16B delivers the first runnable MIAS dashboard. It implements the Phase 16A decisions without changing the API,
the artifacts or the live cluster:
- a `ui/` workspace (Vite, React, strict TypeScript);
- a typed same-origin API client with the locked retry contract;
- an in-memory sign-in session;
- the navigation shell, the Overview and System Status pages, and placeholder pages;
- an nginx container that serves the bundle and proxies `/api/v1/` and `/health/` to mias-api;
- a disabled-by-default Helm `ui.*` block.

**Not done in 16B:** no image push, no `helm upgrade`, no `oc apply`. The live cluster stays at Helm revision 15
(Phase 15).

## Workspace

| | |
|---|---|
| Runtime dependencies | `react` 19.3.0, `react-dom` 19.3.0, `react-router` 8.4.0, `@tanstack/react-query` 5.104.1 (exact pins, `package-lock.json` committed) |
| Toolchain | TypeScript 6.0.3 (strict, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`), Vite 8.3.2, ESLint 10 + typescript-eslint `strictTypeChecked`, Vitest 5, Testing Library, MSW 3, jsdom 29 |
| Node | `>=22.12` (builder image: Node 22.23.2; local: 22.22.1) |
| Styling | plain CSS, system fonts, light/dark via `prefers-color-scheme`. No Tailwind, MUI, Bootstrap or charts. |

Two toolchain pins were forced by compatibility:
- **TypeScript 6.0.3, not 7:** typescript-eslint 8.71 supports TypeScript below 6.1.
- **jsdom 29.1.1, not 30:** jsdom 30 needs Node 22.22.2 or later.

**Gates:**

```bash
cd ui
npm ci
npm run typecheck
npm run lint
npm test -- --run
npm run build
npm run audit:bundle
```

**Build id:** `MIAS_UI_BUILD_ID` (the git SHA) is injected through a Vite `define`. It's validated as
`[A-Za-z0-9][A-Za-z0-9._+-]{0,63}` and defaults to `dev`. It's the only build-time value, and it isn't secret.

## API client (`ui/src/api/client.ts`)

- **URLs:** relative and same-origin only (`/health/live`, `/health/ready`, `/api/v1/...`). There's no base URL, no
  CORS and no cookies (`credentials: "omit"`).
- **Authorization:** `Authorization: Bearer <token>` goes on `/api/v1/*` only, taken from the in-memory session.
  - At sign-in, a candidate token is sent explicitly instead.
  - It's never sent to `/health/*`, never put in a URL, and never logged.
  - With no token, a protected call fails locally as 401 without touching the network.
- **Request id:** each attempt sends `X-Request-ID: ui-<24 hex>` from `crypto.getRandomValues`. The id the API echoes
  is returned with every result and error.
- **Timeouts:** 10 s per attempt using `AbortController`. The caller's `signal` cancels immediately, and a cancelled
  call is never retried.
- **Response handling:**
  - JSON responses get minimal runtime shape checks; a mismatch or non-JSON body raises `invalid_response`.
  - `/health/ready` 503 is returned as data (`not_ready` plus its checks), not as an error.
  - The canonical endpoint is read with `response.text()` and returned as exact text plus `ETag` and `Cache-Control`.
    It's never parsed or re-serialised.
- **Errors:** the closed envelope `{"error":{code,message,request_id}}` maps to a typed `ApiError` with kind, status,
  code and request id. User-facing wording comes from `describeError` and never includes headers, bodies or stack
  traces.

**Retry policy:**

| Outcome | Retries |
|---|---|
| 400, 401, 404, 409, `artifact_invalid` (500), other 4xx, aborted, `invalid_response` | none |
| Network error, timeout, 500 (`internal` or no code), 502, 503, 504 | at most 2, after 1 s then 3 s |

React Query runs with `retry: false`, so the client is the only place retries happen.

**Global 401:** a 401 on any session call:
1. clears the token (the session store's `expire()` is idempotent);
2. clears the whole query cache;
3. lets the route guard redirect to `/signin` with `replace`.

Sign-in validation uses the candidate token, so a wrong token there never triggers this path. There are no redirect
loops.

## Session (`ui/src/auth/session.ts`)

- **States:** `unauthenticated`, `authenticating` and `authenticated`.
- **Storage:** the token lives only in a closure. It isn't in the snapshot, React state, query keys, localStorage,
  sessionStorage, cookies, IndexedDB or the URL. A page refresh means signing in again.
- **Sign-in:**
  1. A password-type input (`autocomplete="off"`) collects the token.
  2. `GET /api/v1/version` validates it.
  3. On **200**, the version is stored and the user goes to the requested page (or `/`).
  4. On **401**, the screen shows "Invalid or expired read token" and the field is cleared.
  5. On a **network error or 5xx**, the screen shows "temporarily unavailable … your token was not rejected"; it is
     never reported as an invalid token.
- **Return path:** only same-app paths are accepted, so there's no open redirect.

## Routes and pages

| Route | Content |
|---|---|
| `/signin` | Sign-in |
| `/` | **Overview** (see below) |
| `/market-intelligence`, `/alerts` | Placeholder: "Detailed view arrives in Phase 16C", plus the kind's newest entry or "No data available yet" |
| `/options-intelligence`, `/trade-setups`, `/invalidation-checks` | Placeholder ("a later Phase 16 step"), with the same working empty state |
| `/status` | **System Status** (see below) |
| anything else | 404 page |

The AppShell has a skip link, a title, the API status badge (live and ready), the UI and API builds, Sign out, the
primary navigation (`aria-current`), and the main region.

**Overview** shows only API-stated facts:
- API live and ready;
- the API build and version, and the UI build;
- **latest Market Intelligence** and **latest Alert**;
- for each of the five kinds, **has data / newest as_of** from `GET /api/v1/<kind>?limit=1`;
- the **last refreshed** time, from the UI clock.

`latest` requires a symbol and there's no symbols endpoint, so the symbol is taken from the newest history entry and
**labelled as such**. With no history, the widget shows the empty state. A 409 `ambiguous_latest` shows its own
message and the request id.

There are **no totals**, since the API has no count endpoint, and no derived judgments: domain values are shown
verbatim in neutral chips.

**System Status:**
- liveness;
- readiness with each check;
- version, with the analytical formats table (`options_intelligence` has two formats);
- the UI build;
- the last successful fetch, and the last error with its request id.

It makes no Prometheus, collector or Kubernetes calls.

**Page states:** every data region is in exactly one of loading (skeleton, `aria-busy`), empty ("No data available
yet"), error, or loaded. The error state shows a safe title, the code, the request id and Retry. A background refresh
keeps the data and shows "Refreshing…".

**Timestamps:** local time is the main display, and the tooltip and `data-original` hold the ISO string exactly as
returned plus UTC. Invalid values are shown verbatim.

**Accessibility (WCAG 2.1 AA baseline):**
- semantic landmarks and one `h1` per page, focused on navigation;
- a skip link and visible `:focus-visible`;
- `th scope` in tables, `aria-live` for refresh status and `role="alert"` for errors;
- status shown as text plus a symbol, never colour alone;
- AA-contrast tokens in both themes, and `prefers-reduced-motion` respected.

### Refresh cadence (React Query)

| Query | Interval / staleTime |
|---|---|
| `/health/live`, `/health/ready` | 30 s |
| `/api/v1/version` | 5 min (seeded at sign-in) |
| History first page (`limit=1`) | 60 s |
| `latest` | 30 s |
| Detail, canonical | immutable: `staleTime: Infinity`, no refetch on focus or reconnect |

Polling pauses in background tabs (`refetchIntervalInBackground: false`).

## Tests (Vitest)

There are **76 tests in 8 files**.

**Unit tests:**
- request-id format and randomness;
- auth injection only on `/api/v1`, relative URLs, `credentials: omit`;
- the echoed request id;
- query encoding;
- readiness 503 as data;
- canonical exact text, including bytes a JSON round-trip would change, plus ETag;
- envelope mapping and `invalid_response`;
- the full retry matrix, with backoff `[1000, 3000]`;
- 10 s timeout with fake timers;
- abort without retry;
- 401 handling for session calls, sign-in candidates and health calls;
- session transitions, the token never in the snapshot, and no storage, cookie or IndexedDB writes;
- timestamps;
- query cadence and keys.

**Component tests:**
- the sign-in redirect and the password field;
- success, with no storage;
- 401 wording, and 503 or network not reported as an invalid token;
- an empty field;
- global 401: token and cache cleared, one redirect;
- sign-out;
- Overview loaded, empty, 409 and loading;
- Status: checks, formats, readiness 503, last error request id and Retry;
- the five placeholders and their empty state;
- the 404 page;
- keyboard order (skip link, Sign out, navigation, Enter navigates).

**Fixtures** (`ui/src/tests/fixtures/phase13.json`) are real responses captured from the Phase 13 API. A scratch
script ran `create_app` with TestClient over the sealed Phase 13 test samples, publishing them through the artifact
store CLI into temporary directories with a placeholder token. The fixtures contain no token.

## Bundle audit (`ui/scripts/audit-bundle.mjs`)

The audit fails the build if `dist/` contains any of the following:
- a source map, or a `sourceMappingURL`;
- the placeholder token or a literal bearer token;
- an API token variable name or a `VITE_*` variable;
- `localStorage`, `sessionStorage`, `indexedDB` or `document.cookie`;
- a CDN or analytics host, a private key, or a JWT-like string;
- an inline `<script>`, `<style>` or `style=`;
- an external `<script>`, `<link>` or CSS URL;
- any URL outside a short allowlist of library strings that are never fetched: React error-doc links, the React
  Router docs link, W3C XML namespaces, and the `http://localhost` that React Router uses as a `new URL()` parsing
  base.

A negative run, with a planted token, an external URL, `localStorage`, a source map and an inline script, reported all
five.

The result is **4 files and 327 KB** (`index.html`, one JS chunk of about 321 KB or 99.5 KB gzip, one CSS file, and
`favicon.svg`): PASS.

## Container (`ui/Containerfile`)

| | |
|---|---|
| Builder | `registry.access.redhat.com/ubi9/nodejs-22@sha256:f7a0b11c9a55c9e1e05983619c6c4884359ca423fccbc4fa7a04d2fc0ee2d869`. Runs `npm ci --ignore-scripts`, typecheck, lint, build and the bundle audit, and asserts no `.map`. |
| Runtime | `registry.access.redhat.com/ubi9/nginx-124@sha256:dd82c493608da2526d05491c5ada4fb3b333d884dac004268150997bf40e4148` (nginx 1.24.0) |
| Contents | `/opt/mias-ui/html` (dist, root-owned, 0644/0755), `/opt/mias-ui/etc` (config template and includes), `/usr/local/bin/mias-ui-entrypoint`. No Node, npm, sources, tests or maps; the S2I `nginx-start` and `nginx.d` contents are removed. |
| User / port | nominal `USER 1001`; runs under any UID in group 0; port 8080 |
| Signals | `STOPSIGNAL SIGQUIT` (graceful drain); SIGTERM also exits 0 |
| Labels | `org.opencontainers.image.revision=<git SHA>`, plus title, description, source and base name |
| Local build | `localhost/mias-ui:4398d07e5319`, revision `4398d07e53195c23800fce86fa28b7ad478c1627`, about 339 MB (the base image is 338 MB; the UI adds about 1 MB). **Not pushed.** |

The base image still contains `dnf`, `rpm` and `yum`. They can't be used by a non-root UID on a read-only root.

**Entrypoint:**
1. Validates `MIAS_UI_API_UPSTREAM`: `host:port`, lowercase DNS name or IPv4, no scheme or path. The default is
   `mias-api.mias.svc.cluster.local:8080`.
2. Takes the resolver from `MIAS_UI_RESOLVER`, or the first `nameserver` in `/etc/resolv.conf`, and validates it as an
   IP.
3. Renders `/tmp/nginx/nginx.conf` with `sed` (two placeholders only).
4. Runs `exec nginx -e stderr -g 'daemon off;'`.

Invalid input exits with status 2. nginx's resolver ignores search domains, which is why the upstream must be fully
qualified. Resolving at request time lets the UI start, and `/healthz` answer, while the API is down.

**nginx configuration:**
- **Proxy:** `/api/v1/` and `/health/` go to `http://<upstream>`.
  - Request headers pass through unchanged, including the browser's `Authorization` and `X-Request-ID`.
  - Nothing is injected, and `Cookie` is stripped.
  - The path and query pass through unchanged.
  - `proxy_intercept_errors off`, so 401, 404 and 503 bodies and headers arrive intact. ETag and Cache-Control are
    preserved, and nothing is cached.
  - Timeouts: connect 5 s, send and read 15 s.
  - `limit_except GET` (GET and HEAD only; anything else gets 403).
- **SPA:**
  - `/` falls back to `index.html`.
  - `/api*` and `/health` outside the proxied prefixes return 404 and never get `index.html`.
  - A missing `/assets/*` returns 404.
- **`/healthz`:** static `200 ok`, never proxied, not logged.
- **Caching:** `index.html` and SPA fallbacks are `no-store`; hashed `/assets/*` are
  `public, max-age=31536000, immutable`; other static files are `no-cache`.
- **Headers on every response:**
  - CSP `default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer`
  - `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`
  - COOP `same-origin`
  - CORP `same-origin`
  - `server_tokens off` (`Server: nginx`, with no version)
- **Logs:** JSON access lines with timestamp, request id, method, **path without query**, status, bytes, duration
  and upstream status. There are no headers, so no Authorization or cookies. Errors go to stderr.
- **Writable state:** pid, temp paths and the rendered config, all under `/tmp`.

### Podman matrix (local, rootless)

The UI ran with:
- arbitrary UID `1000770000:0`, with `--uidmap 1000770000:0:1 --uidmap 0:1:1001 --gidmap 0:0:1 --gidmap 1:1:1000`;
- a read-only root and a 16 MiB `/tmp` tmpfs;
- `--cap-drop=ALL` and `no-new-privileges`.

The upstream was a stdlib echo/mock server.

**Result: 59 passed, 0 failed.**

- **Identity and filesystem:** the UID and GID, no root processes, root and `/etc` not writable, state only in
  `/tmp`, no Node or npm, no maps or sources, and the revision label.
- **Static serving:** `/healthz`; the exact CSP and each security header; `no-store` on `index.html` and SPA
  fallbacks; `Server: nginx` with no version; immutable hashed assets; a missing asset gets 404; SPA routes fall back;
  `/api`, `/api/v2/x` and `/health` get 404.
- **Proxy:** Authorization unchanged, X-Request-ID passed, query unchanged, Cookie stripped, no Authorization injected,
  `/health/` proxied.
- **Pass-through:** 401 status, body and request id; 503 status and body; canonical ETag, Cache-Control and exact
  bytes (sha256 equals the id).
- **Methods and timeouts:** POST gets 403, HEAD gets 200, and the 15 s read timeout returns 504.
- **Logs:** no Authorization, Bearer or token, no query strings, every access line is JSON, no permission or emerg
  errors.
- **API down:** `/healthz` stays 200 and the API path returns 504.
- **Shutdown:** SIGTERM exits 0, and `podman stop` (SIGQUIT) exits 0 in 1 s.
- **Bad configuration:** six invalid upstreams and an invalid resolver each exit 2.

**End-to-end against the real Phase 15 API image** (`localhost/mias-api:259236684a74`): the Phase 13 samples were
published to a temporary artifact root, and the read token was random, held only in the environment, and never
printed. **Result: 15 passed, 0 failed.**
- live and ready through the UI;
- version 200 with the token, with the API echoing the UI request id;
- a wrong token gets a 401 envelope carrying the UI request id; no token gets 401;
- history `limit=1` and `latest?symbol=META` work;
- canonical bytes are identical to the stored file, with ETag and Cache-Control;
- latest without a symbol gets 400;
- the SPA is served;
- the token is absent from the UI and API logs and from the UI container's inspect output.

## Helm (`ui.*`, chart `mias` 0.2.0)

```yaml
ui:
  enabled: false
  image: {repository: image-registry.openshift-image-registry.svc:5000/mias/mias-ui, digest: "", versionLabel: "", pullPolicy: IfNotPresent}
  replicaCount: 1
  api: {upstream: "", clusterDomain: cluster.local}   # empty: mias-api.<ns>.svc.<clusterDomain>:8080
  resources: {requests: {cpu: 10m, memory: 32Mi}, limits: {cpu: 200m, memory: 128Mi}}
  tmp: {sizeLimit: 16Mi}
  route: {enabled: true, host: mias-ui.apps.ngc.sirii.org}
```

**Disabled (the default):** the render is **byte-identical to Phase 15**, checked by sha256 for the default,
`observability.enabled=false`, `networkPolicy.enabled=false` and `route.enabled=false` value sets, with and without an
explicit `ui.enabled=false`.

The chart version stays 0.2.0, because a bump would change the `helm.sh/chart` label on every object.

**Enabled** (`templates/ui.yaml`):

| Object | What it is |
|---|---|
| ServiceAccount `mias-ui` | `automountServiceAccountToken: false` |
| ConfigMap `mias-ui-config` | holds only `MIAS_UI_API_UPSTREAM` |
| Deployment `mias-ui` | see below |
| Service `mias-ui` | ClusterIP 8080 |
| Route `mias-ui` | edge TLS, Redirect |

The Deployment:
- runs 1 replica with RollingUpdate (`maxUnavailable: 0`, `maxSurge: 1`);
- uses the restricted pod and container security contexts shared with the other workloads;
- has no `runAsUser` or `fsGroup`;
- has a 16 Mi `/tmp` `emptyDir`;
- runs startup, liveness and readiness probes on `/healthz`;
- disables service links and sets a 15 s grace period;
- carries a config checksum annotation.

**NetworkPolicies** (when `networkPolicy.enabled`):
- `mias-ui-allow-router`: ingress from the router policy group on TCP 8080 only.
- `mias-ui-egress-api`: egress to `mias-api` pods on TCP 8080, plus openshift-dns pods on UDP/TCP 5353. The 5353 rule
  follows the Phase 15 pattern.
- `mias-api-allow-ui`: ingress to `mias-api` from `mias-ui` pods on TCP 8080. It's an additive policy:
  `mias-api-allow-router` is untouched.

**Not included:** no Secret, RBAC, HPA, `ipBlock` or `0.0.0.0/0`, and no token or Authorization anywhere in the chart.

**Rendering checks:**
- an empty or invalid `ui.image.digest` fails the render (digest pinning is mandatory);
- `ui.api.upstream` must be `host:port`.

`tests/test_helm_ui.py` has 10 tests. The existing Helm, observability and manifest suites are unchanged and pass.

## Validation summary

| Gate | Result |
|---|---|
| `npm ci` / typecheck / lint / test / build / audit | clean / 0 errors / 0 warnings / 76 passed / built / PASS |
| Podman matrix | 59/59 |
| End-to-end with the real API image | 15/15 |
| Helm suites (`test_helm_ui`, `test_helm_mias`, `test_helm_observability`, `test_openshift_manifests`) | 37 passed |
| Full Python regression (disposable PostgreSQL and Redis, four exclusions) | 2343 passed, 7 skipped, 2 failed: both known baseline failures (`test_fed_pipeline` smoke scripts, `test_sec_pipeline` script path); the third baseline test passed this run |

## Known limitations and follow-ups

- **Deployment:** no image is pushed and nothing is deployed. 16F covers pushing the image, setting `ui.image.digest`,
  `helm upgrade --reset-values`, DNS and hosts entries for `mias-ui.apps.ngc.sirii.org`, and live NetworkPolicy
  proofs.
- **Latest symbol:** comes from the newest history entry, because there's no symbols endpoint. Totals aren't shown,
  because there's no count endpoint.
- **Theme:** follows `prefers-color-scheme`. The manual theme toggle and the tablet navigation drawer from 16A §21 are
  deferred to 16E; narrow screens wrap the navigation instead.
- **Scope:** the detail views, raw/canonical tab and history paging are 16C. Status depth is 16D.
- **Session:** memory only (the optional sessionStorage mode from 16A §4 was not adopted, per the 16B instructions).
  Refreshing signs the user out.
- **Proxy errors:** errors nginx generates itself (502/504 when the API is unreachable) have nginx's small body. The
  client treats them as retryable, never shows the body, and reports "temporarily unavailable".
