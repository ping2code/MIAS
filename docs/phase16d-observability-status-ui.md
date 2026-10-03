# Phase 16D: Observability and status views

Phase 16D turns System Status into a lightweight operational view, built only from the API's own health and version
endpoints plus what this browser tab observed. It doesn't cross the Phase 16 boundary: no Prometheus, Thanos,
collector, Kubernetes or OpenShift calls, no browser telemetry, no new endpoints, and no new dependencies.

## Architecture and endpoints

```
Browser (Status page, AppShell indicator)
   │  GET /health/live   GET /health/ready   GET /api/v1/version   (same-origin, via the mias-ui proxy)
   ▼
mias-ui proxy ──▶ mias-api
```

| Endpoint | Used for | Cadence |
|---|---|---|
| `GET /health/live` | liveness | 30 s, and immediately when Status opens |
| `GET /health/ready` (200 or 503) | readiness, its checks, and the readiness history | 30 s, and immediately when Status opens |
| `GET /api/v1/version` | API service, version, build, analytical formats | 5 min |
| *(client)* | recent activity, connectivity, request ids | as requests happen |

An integration test records every request the Status page makes. The result is exactly these three paths, all
same-origin with no query string, and nothing else.

The bundle audit now also rejects infrastructure endpoints:
- the Prometheus `/api/v1/query` path and `thanos-querier`;
- ports 9090, 9091, 4317, 4318 and 8889;
- `/apis/<group>/v1`, `openshift-monitoring`, and the OTLP `/v1/traces` and `/v1/metrics` paths.

A planted example was caught.

## Status page

The layout runs top to bottom: summary tiles, then a two-column grid of cards, then full-width sections.

1. **Summary tiles:** Liveness, Readiness, API build, UI build. The health tiles are `aria-live`.
2. **Service health:**
   - API liveness, API readiness, and an overall derived status;
   - Dashboard ("Running in this browser tab");
   - the last successful API response (time, route, status, copyable request id);
   - the last status refresh.

   It also explains the difference: liveness means the process is running; readiness means it can serve correctly.
   The two are always shown separately.
3. **Readiness checks:** every check in the latest readiness body, as reported, with name, Pass or Fail, and a short
   meaning for the four checks `api/readiness.py` defines (`settings`, `artifact_root`, `artifact_index`,
   `receipt_root`).
   - An unknown check name is shown literally, with no description.
   - The header states "N of M checks pass", plus "the API answered HTTP 503 (reachable, not ready)" when relevant.
   - A **503 is rendered as structured data**, not as an error panel.
4. **Connectivity**, as observed by this tab:
   - API reachable (Reachable, Unreachable or Unknown);
   - retrying now (route, attempt, wait);
   - last failure, last network error, last timeout and last server error, each with time, route, status and
     request id.

   It claims nothing about the collector, Prometheus or OpenShift.
5. **Builds & runtime:**
   - from `/api/v1/version`: service, API version, API build, and the analytical formats table;
   - from the UI: build id, and package version (`package.json`, injected as a non-secret define).
6. **Readiness history:** see below.
7. **Recent API activity:** see below.
8. **Operational guidance:**
   - Deep metrics and telemetry are in OpenShift monitoring.
   - Request ids are the troubleshooting handle: copy one and search the mias-api JSON logs for `request_id`, which
     sits on the same line as the trace and span ids.
   - The metric names to look up are `http_server_request_duration_seconds` and `mias_artifact_index_*`.
   - There are no links, credentials or embedded consoles.

A **Refresh now** button re-checks both health endpoints on demand. It's user-triggered and doesn't change the polling
intervals.

## Interpreting liveness and readiness

Each health endpoint has one *latest observation*: its newest data, or an error that is newer than the data.

| Observation | Liveness wording | Readiness wording | Health |
|---|---|---|---|
| ok / ready | "Live — the API process is running" | "Ready — the API can serve correctly" | healthy |
| not_ready (HTTP 503 with body) | — | "API reachable but not ready (HTTP 503)" | degraded |
| unreachable (network error or timeout, after retries) | "API unreachable" | "API unreachable" | unavailable |
| failed (other HTTP error or an unexpected body) | "API liveness check failed (HTTP 500)" | "API readiness check failed (HTTP …)" | unavailable |
| pending | "Checking…" | "Checking…" | unknown |

A 401 on `/api/v1/version` keeps the 16B global sign-out behaviour.

## Status derivation (AppShell and "Overall")

`lib/apiStatus.ts` is a pure function, tested in a full table. The first matching rule wins:

1. readiness **ready** → **Ready**. If liveness's latest observation failed or was unreachable, it's **Degraded**
   instead.
2. readiness **not_ready** → **Not ready**. The API answered 503: it's reachable but can't serve correctly.
3. readiness **and** liveness both **unreachable** → **Offline**.
4. readiness **pending** → **Checking**.
5. anything else → **Degraded**. For example, readiness failed unexpectedly, or only one endpoint is unreachable.

**Hysteresis:** the client retries network errors and timeouts twice (1 s, then 3 s) before a query fails, so a single
transient failure never becomes an observation. Offline also needs **both** endpoints unreachable, so a lone failed
artifact request never shows Offline.

The AppShell indicator reads "API: Ready", "API: Not ready", "API: Degraded", "API: Offline" or "API: Checking". Each
has a symbol and text, and is exposed as `data-api-status` for tests. It shares the same cached queries and the 30 s
cadence.

## Diagnostics model (`app/diagnostics.ts`)

**One entry per logical request.** The client calls `onRequest` once per logical request, after all of its attempts.
The entry holds:

| Field | Content |
|---|---|
| `startedAt` | when the request started |
| `method` | always GET |
| `route` | the route template, with no query and no ids: `/api/v1/alerts`, `/api/v1/{family}/latest`, `/api/v1/{family}/{id}`, `…/{id}/canonical`, `…/{id}/deliveries`, `/health/live`, `/health/ready`, `/api/v1/version` |
| `status` | the final HTTP status, or null |
| `outcome` | see the categories below |
| `durationMs` | the whole logical request, including backoff waits |
| `attempts` | how many attempts were made |
| `requestId` | the id the API echoed, or the one the client sent |
| `note` | `not_ready` for readiness 503, `capability_unavailable` for deliveries 503 `dependency_unavailable` |

**Outcome categories:** `success`, `client_error` (4xx), `server_error` (5xx or an unexpected body), `network_error`,
`timeout` and `cancelled`.

**Not failures:** contract-defined answers (the notes above) and cancellations. They don't count as "last failure"
and are hidden by "Problems only".

**Retry visibility:** individual attempts are never shown as rows. The `onRetry` hook only feeds "Retrying now",
which clears when the request completes. A final success after a retry is labelled **"Recovered after retry"**, and a
final failure after retries shows "N attempts".

**Bounds:**

| Kept | Limit |
|---|---|
| Request summaries | the newest **50**, newest first |
| Readiness samples | the newest **20** |
| Last success, last failure, last network error, last timeout, last server error | each kept separately, so eviction never loses them |
| Total count | unbounded |

**Memory only:** no storage APIs at all. A reload starts empty. Sign-out resets the diagnostics; a global 401 keeps
them, since they hold no secrets.

**Privacy:** every entry is **rebuilt field by field from an allow-list**:
- the route must match the template grammar, otherwise it's stored as `(other)`;
- the request id must match the API's id grammar, otherwise null;
- check names must match the API's check-name grammar.

So no header, token, cookie, query string, symbol, cursor, body or canonical text can be stored, even if a caller
passed one. A unit test feeds placeholder canaries (`Bearer …`, `Authorization`, `Cookie`, `api_key`, `password`,
`symbol=`, `token=`, a canonical canary) and asserts none survives. A diagnostics hook that throws can't affect a
request.

There's **no trace id**: the client contract exposes only `X-Request-ID`, so no trace id is shown and no
trace-navigation link is invented.

## Readiness history

There's one sample per completed readiness query, recorded from the query cache, so each sample already includes the
client's retries. A sample holds:
- the time;
- the result: `ready`, `not_ready`, `unreachable` or `failed`;
- the HTTP status;
- each check's name and pass/fail exactly as returned (empty when no body arrived);
- the request id.

The page shows the samples two ways:
- a **compact strip**, oldest to newest. Each cell has a symbol and visually-hidden text with the time and result, so
  colour is never the only signal.
- a **list**: observed time, result badge, HTTP status, and the names of failing checks.

Memory only, it resets on reload, and it isn't drawn as a chart because it isn't a metric time series.

## Visual design and accessibility

- **Visual design:** same design language as 16C (cards, `DetailSection`, `MetadataList`, `DomainBadge`), with dense
  rows and compact tiles rather than giant status tiles. There are new badge kinds for `health`, `outcome` and
  `check`; colour signals only system health.
- **Live regions:** status changes use `aria-live` (summary tiles, overall status, AppShell indicator, page meta).
- **Semantics:**
  - tables have captions and `th scope`;
  - the readiness strip is an `ol` with an accessible name;
  - "Problems only" is a labelled checkbox;
  - every copy button is named "Copy request id …".
- **Focus and keyboard:** the page `h1` receives focus on navigation, and every control is a native button,
  checkbox or link.
- **Responsive:**
  - tiles are four across on desktop, two at 1100 px and below, one on phones;
  - cards stack at 1100 px and below;
  - the activity table becomes stacked cards at 640 px and below.
- **Theme:** works with the existing light and dark tokens. The manual toggle is still 16E.

## Polling

There are no new intervals: health every 30 s (paused in background tabs), version every 5 min. The Status page uses
`refetchOnMount: "always"` for the two health queries, so opening it shows current state immediately.

## Tests

There are **171 tests in 15 files**, all passing and stable over 3 runs. That's 125 carried over (three superseded 16B
Status cases were replaced) plus 46 new.

| Suite | Tests |
|---|---|
| Unit: `tests/unit/status16d.test.ts` | 31. Bounded requests (50) and readiness (20) with eviction; last-of-kind kept after eviction; redaction with canaries and an exact allowed-field set; route-template grammar; clear and reset; retry-state clearing; outcome categories; contract notes and cancellations not counted as failures; "Recovered after retry"; full derivation table (13 cases); newest-observation selection; precise wording; reachability; durations with an injected clock (including backoff); route templates with no symbol, cursor or id; a single final summary after exhausted retries; a throwing hook doesn't break requests; readiness 503 sampled as not ready with its checks; unreachable readiness sampled without a body. |
| Component: `tests/components/status.test.tsx` | 14. Checking/loading; healthy (separate liveness and readiness, every check, builds, formats, UI build and version); 503 rendered as reachable-not-ready with mixed checks and no crash panel; unreachable (Offline) versus not ready; liveness 500 wording with request id; recent activity (route templates, status, outcome, duration, note, request-id copy, no secrets); Problems-only and the empty state after Clear; recovered after retry; readiness history grows on Refresh now (strip and list, failing check, 503); operational guidance with no links; AppShell Ready, Not ready, Offline and Checking. |
| Integration: `tests/integration/status16d.test.ts` | 1. The Status page requests exactly `/health/live`, `/health/ready` and `/api/v1/version`, same-origin, with no query string and no infrastructure paths. |

**Fixtures:** `ui/scripts/capture_api_fixtures.py` now also captures `health:live`, `health:ready`, `version` and a
**real readiness 503**. That 503 comes from an API whose configured receipt root was removed after start-up, so
`receipt_root` fails and the other three checks pass. Only the new keys were added to `phase16c.json`; the existing
fixtures are byte-identical.

## Gates

| Gate | Result |
|---|---|
| `npm ci` (clean) | ok, **no dependency changes** |
| typecheck / lint | ok / 0 errors, 0 warnings |
| `npm test -- --run` | 171/171 |
| `npm run build` | JS 375.7 KB (112.4 KB gzip), CSS 16.9 KB |
| `npm run audit:bundle` | PASS, including the new infrastructure-endpoint rule |
| Runtime source grep | no storage APIs, cookies, `sendBeacon`, or Prometheus/Thanos/Kubernetes calls (those names appear only in explanatory text). `Authorization` is set only in the client, on `/api/v1/*`. |

## Container validation (local; nothing pushed)

The image `localhost/mias-ui:f29248273ebe` is revision `f29248273ebe60e97d8435b589c170f328ff67bb`. The Containerfile
and nginx config are unchanged.

**Podman matrix: 68/68.** These are the 66 checks from 16C plus 2 new ones:
- the `/status` deep link returns `index.html` with `no-store`;
- `/health/healthz` is proxied to the API (404) and is never SPA content.

**End-to-end against the real Phase 15 API image: 22/22.** These are the 20 checks from 16C plus 2 new ones:
- the readiness checks seen through the UI are exactly the API's own (`settings`, `artifact_root`,
  `artifact_index`);
- `/status` is served.

## Python regression

- **Focused suites** (Helm UI, Helm chart, observability chart, manifests, API integration and foundation, API
  observability): 101 passed.
- **Full regression** (disposable PostgreSQL and Redis, the four exclusions): 2343 passed, 7 skipped, **2 failed**.
  Both are known baseline failures:
  - the Fed runpy smoke scripts;
  - the SEC argv script path.

  The intermittent geopolitical baseline test passed this run. There are no new failures.
- **Persistent services:** `mias-postgres` and `mias-redis` have the same ids and start times before and after the
  run. The disposable containers were removed.

## Helm, cluster and protected areas

- **Helm:** no chart change; `ui.enabled` stays `false`.
- **Cluster:** read-only check shows Helm `mias` revision 15. There are no `mias-ui` resources, and the collector,
  ServiceMonitor, NetworkPolicies and Route are unchanged.
- **Protected areas:** Phase 6 hashes verified; migration head `0007_technical_evidence_ledger`. No change outside
  `ui/` and this document.

## Limitations

- **Tab-local scope:** everything is what *this tab* observed since it loaded. There's no cross-user or historical
  view, by design; that belongs to OpenShift monitoring.
- **Duration** is measured in the browser (`Date.now()`), includes backoff waits, and has millisecond resolution. It
  isn't server latency.
- **Cancelled rows:** cancellations (for example, leaving a page mid-request) appear as `cancelled` rows. They are
  hidden by "Problems only".
- **"Dashboard: Running in this browser tab"** is self-evident and doesn't probe mias-ui's `/healthz`, which isn't on
  the allowed endpoint list.
- **No direct console link**, because no stable approved URL exists yet. Guidance names the console area and metric
  names instead.
- **Fixtures:** the readiness 503 fixture is from a removed receipt root. The lab deployment has no receipt root, so it
  reports three checks.

## Phase 16E handoff (auth/session handling and UX hardening)

**Auth and session:**
- session-expiry UX: keep the 16B global 401 flow and add a clear "session ended" banner when it happens on a deep
  page, with the return path preserved;
- an explicit idle or lock option (memory only);
- sign-in rate feedback;
- **no** token persistence (the locked decision stands).

**UX hardening:**
- the manual light/dark theme toggle (16A §21; per-viewer preference only, in memory or with documented consent;
  respect `prefers-color-scheme` by default);
- the tablet navigation drawer;
- consistent focus management after async actions;
- reduced-motion audit;
- a contrast pass on the new badge tones;
- empty and error copy review.

**Diagnostics follow-ups (optional):**
- show the AppShell status reason in a tooltip;
- link the error panels' request ids to the Status page's recent-activity row (in-page anchor only).

**Carry forward:**
- the 16C real-browser download check under the production CSP;
- screenshots (16E/16F);
- 16F performs the first live deployment (image push, `ui.image.digest`, `helm upgrade --reset-values`, DNS/hosts,
  live NetworkPolicy proofs).

**Constraints unchanged:** no infrastructure calls from the browser, no browser telemetry, and no runtime
dependencies without justification.
