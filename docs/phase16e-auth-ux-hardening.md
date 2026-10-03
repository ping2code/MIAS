# Phase 16E: Auth/session handling and UX hardening

Phase 16E hardens the dashboard before its first live deployment. The auth model is unchanged: a user-entered read
token, held in memory only. A 401 clears it, and a refresh means signing in again.

This phase adds:
- a polished session-ended flow with safe return paths;
- a memory-only lock;
- a manual theme control;
- a tablet and phone navigation drawer;
- focus, reduced-motion, contrast and wording reviews;
- the first **real-browser** validation, including the canonical copy and download carried over from 16C.

**What did not change:**
- no new runtime dependencies;
- no API, Helm or container-configuration changes;
- nothing deployed; the live cluster was not touched.

## Session-ended flow

When any authenticated request returns 401:
1. The client's global handler clears the token from memory, clears the whole query cache and the in-memory cursor
   trail, and marks the session `expired`. It is idempotent, so concurrent 401s make a single transition.
2. The route guard redirects to `/signin` with `replace`, carrying the **safe return path** of the page the user was
   on, including its filters.
3. The sign-in screen shows an `alert` banner: **"Your MIAS session ended. Sign in again to continue."** It adds "You
   will return to the page you were on." when the return path isn't `/`. The page heading receives focus.
4. After a successful sign-in, the user returns to exactly that safe path. For example, `/alerts?symbol=ZZZZ` comes
   back as `/alerts?symbol=ZZZZ`.

There are no loops: the redirect uses `replace`, `/signin` is never a return target, and the sign-in validation itself
uses a candidate token that never triggers the global handler.

## Return-path safety (`auth/returnPath.ts`)

A return path is accepted only if **all** of these hold:
- it's a string starting with `/`, not `//`, with no backslash and no control characters, at most 512 characters;
- it parses as a same-origin path, with no `%2F` or `%5C` encoded-slash tricks;
- the pathname is a **known app route**: `/`, `/market-intelligence`, `/market-intelligence/sha256:<64 hex>`,
  `/alerts`, `/alerts/sha256:<64 hex>`, `/options-intelligence`, `/trade-setups`, `/invalidation-checks` or `/status`.

Query parameters are then filtered to the app's own non-secret keys (`symbol`, `as_of_from`, `as_of_to`, `limit`,
`tab`), with short and safe values only. Anything else is dropped. `cursor`, for example, is never carried.

**Rejected, falling back to `/`** (each is a unit-test case):
- `http://…`, `https://…`, `//evil…`, `///…`, `/\evil`;
- `javascript:…` and `data:…`;
- `/%2F%2Fevil` and `/%5cevil`;
- `/signin`, unknown routes, malformed artifact ids;
- control characters, an empty string, a non-string, and anything over-long.

## Memory-only lock

The **Lock** button in the top bar hides the dashboard without signing out. The session has three states:

| State | Behaviour |
|---|---|
| authenticated, unlocked | normal |
| authenticated, **locked** | The token stays only in the session store's closure, which is the same place as before; nothing else holds it. `getToken()` returns **null**, so no request can carry it. The protected tree is **unmounted**, so the content isn't in the DOM behind an overlay. The query cache is cancelled and cleared, so no polling happens; the browser test confirmed no API request while locked. The lock screen heading receives focus. |
| unauthenticated | sign-in |

**Unlocking:**

| Outcome | Result |
|---|---|
| Enter the **same** read token | It's compared inside the session store (the token never leaves it), then validated with `GET /api/v1/version`. On success the user is back on the same page. |
| A different token | "That token does not match this session. The dashboard stays locked." No API call is made, and focus returns to the field. |
| API answers 401 | The token no longer works: the session ends, and the user goes to sign-in with the session-ended banner. |
| Network error or 5xx | "MIAS is temporarily unavailable. The dashboard stays locked — try again." |
| Sign out (from the lock screen) | Clears everything. |

There's no PIN and no persistence. A reload from the locked state is unauthenticated.

## Theme (`lib/theme.ts`, `components/ThemeControl.tsx`)

- **Control:** **System**, **Light** or **Dark**, as a native radio group (`fieldset` with the legend "Theme"), each
  with an icon and a text label. It's keyboard operable with arrow keys and has a visible focus ring. On tablets and
  phones the text labels are visually hidden but stay accessible.
- **Applying it:** `data-theme="light"` or `"dark"` on `<html>`. "System" removes the attribute, and the CSS follows
  `prefers-color-scheme`. Dark tokens are defined once for `[data-theme="dark"]` and identically for System-in-dark;
  a test checks the two sets are equal.
- **Storage:** the preference is the only stored value: localStorage key **`mias-ui-theme`**, value `light` or `dark`.
  It's removed for System, values are validated on read, and storage failures are ignored.
  - This is a deliberate exception: a non-sensitive per-viewer preference. The auth token, session state and
    diagnostics are still never stored.
  - Tests confirm that sign-in, use and a theme change produce exactly one `setItem("mias-ui-theme", "dark")` call.
  - The real browser shows `[["mias-ui-theme","dark"]]` in localStorage, with sessionStorage and cookies empty.
- **First paint:** the stored theme is applied in `main.tsx` **before the first render**. The empty document before
  the scripts run uses the System theme, so there's no flash of unreadable colours; both themes are complete.

## Navigation drawer

At 900 px and below, the persistent side navigation is replaced by a **Menu** button (`aria-expanded`,
`aria-controls`, `aria-haspopup="dialog"`) that opens a drawer. It's built with React and CSS only:

- `role="dialog"`, `aria-modal="true"`, labelled "Navigation".
- **Focus:** moves into the drawer on open, and Tab is trapped inside.
- **Background:** the skip link, top bar and main region are `inert` while the drawer is open, so background content
  can't be reached by keyboard.
- **Closing:** Escape, the **Close** button and the backdrop close the drawer and return focus to **Menu**.
- **Choosing a page** closes the drawer and focuses that page's heading. Real-browser QA found that choosing the page
  already shown left focus nowhere; this is fixed and tested.
- **Desktop:** unchanged, with no Menu button, no trap and no inert.

## Focus management

| Situation | Behaviour |
|---|---|
| Route change | Page `h1` receives focus (16B), including drawer navigation. |
| Sign-in or unlock error | The `alert` is announced and linked with `aria-describedby`; focus returns to the token field. |
| Session ended | The banner is a `role="alert"` and the sign-in heading takes focus. |
| Lock | The lock-screen heading takes focus. |
| Retry | The error panel unmounts on retry, so focus moves to the enclosing region's heading instead of being lost. Retry is shown only when retrying can help (`describeError(...).retryable`). |
| Copy | The result is announced (`role="status"`). Failure is not silent: "Copy unavailable — select the canonical text manually." It stays visible until the next attempt. |
| Download | Focus stays on the button and "Saved as `<hex>.json`" is announced. |

There are no focus traps on desktop.

## Reduced motion

- A `@media (prefers-reduced-motion: reduce)` block removes **all** animation and transitions, and sets
  `scroll-behavior: auto`.
- The skeleton pulse, spinner and drawer slide only run under `no-preference`.
- Nothing depends on animation.
- The real browser confirmed the drawer's computed `animation-name` is `none` under reduce.

## Contrast review (WCAG 2.1 AA)

`tests/unit/contrast.test.mjs` computes WCAG contrast ratios **directly from `app.css`** for both themes (65 checks):
- **Text (at least 4.5:1):**
  - text on bg, surface, surface-2, sunken, neutral, error and accent-soft;
  - muted and subtle on every surface;
  - links and accent on surfaces;
  - accent-text on the accent button;
  - every badge tone on its background and on the surface.
- **Non-text (at least 3:1):**
  - the focus ring on every surface;
  - the borders of interactive controls.

**Finding:** input, quiet-button, toggle and segmented-control borders used `--border-strong`, at about 1.7–2:1. That
fails WCAG 1.4.11. A new `--control-border` token fixes it: `#7b8594` in light (3.4–3.7:1) and `#6b7685` in dark
(3.5–4.0:1). Decorative card and table borders keep the subtle tokens.

Colour is never the only signal: every badge, status, readiness cell and outcome carries text plus a symbol.

## Wording

**Errors** (`api/errors.ts`, reviewed):

| Condition | Title — detail |
|---|---|
| Network | "MIAS is unreachable" — "MIAS is temporarily unreachable. Try again." |
| Timeout | "MIAS did not respond in time" — "MIAS is temporarily unreachable. Try again." |
| 5xx, `dependency_unavailable` | "Temporarily unavailable" — "MIAS is temporarily unavailable." |
| 401 | "Session ended" — "Your session ended. Sign in again." |
| 404 | "Not found" — "That artifact could not be found." |
| 409 | "Ambiguous latest" — "More than one artifact matches the latest timestamp. MIAS will not choose between them." |
| 400 | "Request not accepted" — "MIAS did not accept the request. Check the filters and try again." |
| `artifact_invalid` | "Integrity check failed" — "This artifact failed its integrity check, so it is not shown. Report the request id." |
| Unexpected body | "Unexpected response" — "MIAS returned a response the dashboard could not read. Try again later." |

No stack traces, exception classes, nginx bodies, headers or token details are ever shown. Sign-in keeps "Invalid or
expired read token" for a 401. For a backend failure it says "MIAS is temporarily unavailable. Your token was not
rejected — try again shortly."

**Empty states:**

| Context | Wording |
|---|---|
| Market Intelligence, filtered | "No market intelligence is available for the selected filters." |
| Market Intelligence, unfiltered | "… yet." |
| Alerts, filtered | "No alerts are available for the selected filters." |
| Alerts, unfiltered | "… yet." |
| Options Intelligence | "No options intelligence is available yet." |
| Trade Setups | "No trade setups are available yet." |
| Invalidation Checks | "No invalidation checks are available yet." |
| Overview kinds table | "None yet." |

## Real-browser validation

**Method:**
- **Driver:** `ui/scripts/browser-qa.mjs` drives a **real Chromium 151**
  (`docker.io/chromedp/headless-shell@sha256:5f877a2a559dea1a99fb750da695d28a020cdd49db660aead6c78a46e3c7dd50`, run
  with Podman on the host network) over the DevTools protocol with `playwright-core` 1.63.0.
- **Stack:** the real Phase 15 `mias-api` image (Phase 13 sample artifacts, a random token held only in the
  environment) behind this phase's `mias-ui` image (`localhost/mias-ui:fe5d2df8096d`), same-origin at
  `http://127.0.0.1:18082`. That's a secure context, so the clipboard works.
- **Tooling footprint:** `playwright-core` was installed in a scratch directory. It is **not** a project dependency,
  and no browser binary was downloaded into the repo or the image.
- **Why a container:** WSL has no Linux browser, its Chromium libraries (libnss3, libasound) are missing, and the
  Windows Chrome DevTools port isn't reachable from WSL in NAT mode. The headless-shell container avoids system
  changes.

**Result: 50/50 checks passed** on the final image.

- **Sign-in:**
  - an unauthenticated deep link redirects to `/signin`;
  - a wrong token gets "Invalid or expired read token", with focus back in the field;
  - the right token returns to `/status`, with focus on the heading.
- **Pages:**
  - Status readiness checks;
  - the MI list and a filter kept in the URL; MI detail;
  - the canonical tab shows **exactly** the API text; the ETag-match check passes;
  - Alerts and alert detail, with "Delivery information is not available in this deployment."
- **Copy Canonical:** the clipboard holds **exactly** the canonical text.
- **Download Canonical:** see below.
- **Theme:**
  - Dark and Light apply the right computed background;
  - System follows emulated `prefers-color-scheme` dark and light;
  - only `mias-ui-theme` is in localStorage, and sessionStorage and cookies are empty.
- **Lock:**
  - no `main`, no navigation, heading focused;
  - **no API requests while locked**;
  - a wrong token stays locked; the same token unlocks to the same page.
- **Tablet (820×1180) and phone (390×844):**
  - Menu replaces the side navigation;
  - the drawer opens from the keyboard with focus inside and an inert background;
  - Escape returns focus to Menu;
  - choosing a page closes the drawer and focuses its heading.
- **Reduced motion:** the drawer animation is `none`.
- **Session ended:**
  - the API was restarted with a rotated token mid-session and a new filter applied;
  - the browser landed on `/signin` with the banner and the "you will return" note;
  - after signing in with the new token it was back on `/alerts?symbol=ZZZZ` with the filtered empty state;
  - a reload required sign-in again;
  - the theme preference survived the reload.
- **Hygiene:**
  - no CSP violations (a `securitypolicyviolation` listener was installed before any page script);
  - no console or page errors;
  - every request same-origin, no token in any URL;
  - `Authorization` sent only to `/api/v1/*`;
  - the token appears in neither the QA log nor any QA output file.

## Canonical download and CSP finding

The download works under the **unchanged** production CSP:

> `default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none';
> frame-ancestors 'none'`

- **Mechanism:** a same-origin `blob:` URL (`blob:http://127.0.0.1:18082/…`) clicked through an anchor with
  `download`. Creating a blob URL and following it as a download isn't governed by `connect-src` or `script-src`, and
  no violation was reported.
- **Result:**
  - the file name is `<64 hex>.json`;
  - the **downloaded bytes equal the API's canonical bytes** (byte comparison);
  - neither the file name nor the download URL contains the token.

No CSP change was needed, and none was made.

## Screenshots and manual visual QA

There are 26 screenshots: desktop 1920×1080 and 2560×1440, tablet 820×1180 and phone 390×844. They cover sign-in,
Overview, the MI list, detail and canonical, Alerts and alert detail, Status, the locked screen, session ended and the
drawer, in light and dark. They are not committed.

**Findings, fixed:**
1. The compact copy glyph (⧉) isn't in Chromium's bundled fonts and rendered as a box. It's now a small inline SVG,
   which needs no font and no CSP change.
2. Readiness check names broke mid-word (`artifact_r oot`) in the narrow column. They're now kept whole.

**Accepted:** on 2560-wide screens content is capped at 1760 px, which leaves empty space on the right. This keeps the
desktop density intended in 16C.

## Accessibility summary

- landmarks: banner, the `Primary` navigation, `main` and named regions;
- labelled inputs;
- tabs, radio-group and dialog semantics; drawer focus order and the trap;
- visible focus on every control, including the visually hidden theme radios;
- `aria-live` and `role="alert"` where appropriate;
- AA contrast computed from tokens;
- text plus symbol for every status.

No axe dependency was added, since semantic assertions cover these.

## Tests

There are **299 tests in 18 files**, all passing and stable over 3 runs. That's 171 from 16D plus 128 new.

| Suite | Tests | Coverage |
|---|---|---|
| `tests/unit/auth16e.test.ts` | 37 | Return-path allow and deny tables (including every unsafe form above); the lock model (no token while locked, same-token match, no snapshot leak, no storage, sign-out and expiry from lock, a fresh store is unauthenticated); theme storage (default system, one key and three values, tampered values and storage errors ignored). |
| `tests/unit/contrast.test.mjs` | 65 | WCAG ratios from `app.css` in both themes; chosen Dark equals System-in-dark. |
| `tests/components/ux16e.test.tsx` | 26 | Session-ended round trip with filters; 401 on `/`, `/alerts`, alert detail and `/status`; unsafe `from`; reload; no token in storage or cookies; sign-out clears token, cache and diagnostics; lock (hidden content, no requests, focus; wrong token without an API call; unlock; 401 → sign-in; unreachable keeps it locked; sign-out from lock); theme (group semantics, default, Light, Dark, System, arrow keys, restore); drawer (desktop unaffected, keyboard open, modal, focus inside, Tab trap, inert, Escape returns focus, Close, destination focuses heading, current page focuses heading); retry focus; no Retry for 400. |

16B tests were updated for the new wording and the tab order (skip link, theme, Lock, Sign out, navigation).

## Gates

| Gate | Result |
|---|---|
| `npm ci` (clean) | ok, **no dependency changes** |
| typecheck / lint | ok / 0 errors, 0 warnings |
| `npm test -- --run` | 299/299 |
| `npm run build` | JS 384.3 KB (114.8 KB gzip), CSS 19.9 KB |
| `npm run audit:bundle` | **PASS** |

**Bundle audit change:** the blanket `localStorage` ban is replaced by a rule that allows `localStorage` **only** as
`.getItem`, `.setItem` or `.removeItem` with the literal key `"mias-ui-theme"`. Any other key, a computed key or a bare
reference fails, and `sessionStorage`, `indexedDB` and `document.cookie` still fail. A negative run with
`localStorage.setItem("mias-token", …)`, `localStorage.getItem(k)` and `sessionStorage.setItem` reported all three.

The return-path parser uses the same `http://localhost` parse base React Router uses, so the external-URL allowlist
is unchanged.

## Container validation (local; nothing pushed)

The image `localhost/mias-ui:fe5d2df8096d` is revision `fe5d2df8096d5f119ed395d01475ccddb3cd1bf8`. The Containerfile
and nginx config are unchanged.

| Run | Result | New in 16E |
|---|---|---|
| Podman matrix | **71/71** (68 from 16D plus 3) | `/signin` and `/status` deep links return `no-store` with the **exact unchanged CSP**; the hashed CSS ships the `data-theme` dark and light tokens and the reduced-motion block |
| End-to-end against the real API image | **22/22** | — |
| Real-browser QA | **50/50** | everything above |

## Python regression

- **Focused:** Helm UI, chart, observability, manifests, and the API integration, foundation and observability suites:
  101 passed.
- **Full:** with disposable PostgreSQL and Redis and the four exclusions: 2342 passed, 7 skipped, **3 failed**. All
  three are the known baseline failures:
  - Fed runpy;
  - SEC argv;
  - the intermittent geopolitical `test_full_runbook_with_in_memory_redis`.

  There are no new failures.
- **Persistent services:** `mias-postgres` and `mias-redis` have the same ids and start times before and after. The
  disposable containers were removed.

## Helm, cluster and protected areas

- **Helm:** no chart change; `ui.enabled` stays `false`.
- **Cluster (read-only):** Helm `mias` revision 15. There are no `mias-ui` resources; NetworkPolicies, Routes and the
  collector are unchanged; Secret `mias-api-auth` is at resourceVersion 6143238 (unchanged, no value read).
- **Protected areas:** Phase 6 hashes verified; migration head `0007_technical_evidence_ledger`. No change outside
  `ui/` and this document.

## Limitations

- **Tested engine:** real-browser QA ran on Chromium only (headless). Firefox, Safari and Edge weren't automated.
  Windows Chrome and Edge exist on the host but aren't reachable over CDP from WSL in NAT mode.
- **Run from:** the browser reached the UI over `http://127.0.0.1` (a secure context). On the cluster the UI is
  served over HTTPS through the edge route, also a secure context, so the clipboard behaves the same. This is to be
  re-confirmed live in 16F.
- **Unlock check:** the lock is a privacy screen, not a security boundary against someone with access to the open
  browser process. The token remains in that tab's memory by design. There's no idle auto-lock.
- **Theme storage:** the preference is the only browser-stored value. Clearing site data resets it to System.
- **Not committed:** the screenshots, and `playwright-core` itself (the script expects a path to it).

## Phase 16F handoff (final UI validation and the first live deployment)

16F owns the first live deployment. Exact steps:

1. **Build** `mias-ui` from the 16E merge commit:
   `podman build --build-arg MIAS_UI_BUILD_ID=$(git rev-parse HEAD) -t mias-ui:<sha12> -f ui/Containerfile ui`.
   Re-run the gates, the bundle audit and the Podman matrix on that exact image.
2. **Push** to the internal registry using the 14B/14C procedure: a temporary `oc port-forward` to the registry and
   an auth file from `oc whoami -t` on stdin, deleted afterwards. Record the digest. ImageStream: `mias/mias-ui`.
3. **Helm:** set `ui.enabled=true`, `ui.image.digest=<sha256>` and `ui.image.versionLabel=<sha12>`. Use the default
   upstream (`mias-api.mias.svc.cluster.local:8080`) unless the cluster domain differs. Render the diff first, then
   run `helm upgrade mias deploy/helm/mias -n mias --reset-values --force-conflicts --wait`. The chart changes in
   16B–16E: none.
4. **DNS:** add a hosts or DNS entry for `mias-ui.apps.ngc.sirii.org`, pointing at ingress VIP 192.168.1.60 (like the
   API).
5. **Live checks:**
   - restricted-v2 SCC on the pod, with an arbitrary UID and a read-only root;
   - Route edge TLS plus the HTTP→HTTPS redirect;
   - security headers and CSP through the Route;
   - the proxy passes Authorization and X-Request-ID through;
   - NetworkPolicy allow and deny proofs: router→ui:8080 allowed; ui→api:8080 and DNS allowed; ui→anything else
     denied; an unlabelled pod→ui denied; the api accepts ui;
   - `/healthz` probes healthy.
6. **Live real-browser QA:** run `ui/scripts/browser-qa.mjs` against `https://mias-ui.apps.ngc.sirii.org` (lab
   self-signed certificate: trust it or use a CDP context with `ignoreHTTPSErrors`). Re-confirm clipboard and
   download over HTTPS. Skip the token-rotation step unless approved; it restarts the API.
7. **Rollback plan:** `ui.enabled=false` plus `helm upgrade --reset-values` removes every mias-ui object and the
   `mias-api-allow-ui` policy. The API is unaffected.
8. **Close Phase 16:** final report, docs index, screenshots attached to the 16F report if wanted.
