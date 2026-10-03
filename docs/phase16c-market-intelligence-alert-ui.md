# Phase 16C: Market Intelligence and Alert views

Phase 16C adds the first full domain views to the read-only dashboard:
- Market Intelligence history, detail and canonical views;
- Alert history, detail, deliveries and canonical views;
- cursor pagination;
- filtering, using only the API's own parameters.

It builds on the Phase 16B foundation without changing it: same stack and runtime dependencies (`package.json` and the
lockfile are unchanged), memory-only token, same-origin proxy, same API client contract and polling.

There are no API, backend, Helm, container-configuration or cluster changes. There is no trading logic, no scoring,
and no delivery state the API didn't return.

## Pages

| Route | Page | API |
|---|---|---|
| `/market-intelligence` | History: filters, cursor pagination, latest-for-symbol | `GET /api/v1/market-intelligence`, `GET /api/v1/market-intelligence/latest` |
| `/market-intelligence/:id` | Detail: Summary and Raw / Canonical tabs (`?tab=canonical`) | `GET /api/v1/market-intelligence/{id}`, `…/{id}/canonical` |
| `/alerts` | History: filters, cursor pagination, latest-for-symbol | `GET /api/v1/alerts`, `GET /api/v1/alerts/latest` |
| `/alerts/:id` | Detail: Summary (with Deliveries) and Raw / Canonical tabs | `GET /api/v1/alerts/{id}`, `…/{id}/canonical`, `…/{id}/deliveries` |

Options Intelligence, Trade Setups and Invalidation Checks stay as the 16B placeholders.

**Endpoints used:** exactly the eight endpoints above, plus the 16B health and version calls. No endpoint, parameter or
field was added or assumed.

## Contracts used (Phase 13, unchanged)

**Market Intelligence history and detail** (view `market-intelligence-summary-v1`):
- `intelligence_id`, `intelligence_format_version`, `rules_version`;
- `symbol`, `as_of`;
- `synthesis_id`;
- `timeframe_pattern`, `technical_status`, `market_context_available`;
- `conflict_codes[]`;
- `attention[]` (`code`, `category`).

**Alert history and detail** (view `alert-summary-v1`):
- `alert_id`, `alert_format_version`, `rules_version`;
- `alert_code`, `symbol`, `subject_kind`;
- `assessment_id` (nullable), `transition` (nullable, `{previous, current}`);
- `as_of`;
- `facts` (an object);
- `source_refs[]` (`role`, `object_kind`, `id`).

**Deliveries** (view `alert-deliveries-v1`):
- `alert_id`, `channel`, `sequence`, `status` (`delivered` or `failed`);
- `provider_message_id`, `attempts`, `safe_error_code`;
- `attempted_at`, `completed_at`;
- `delivery_contract_version`, `render_version`.

**Every response:** `meta.{api_version, view, request_id, served_at}`, plus `limit` and `next_cursor` on lists.

**History parameters:** `symbol`, `as_of_from`, `as_of_to`, `limit` (1–200) and `cursor`. Ordering is the backend's
(sealed `as_of` descending, then id), and the UI never re-sorts. The cursor is **bound to its query**: a cursor from
one filter set is rejected (400) for another, which the end-to-end check below confirms.

## History lists

**Columns.**

| List | Columns |
|---|---|
| Market Intelligence | Symbol, As of, Timeframe pattern, Technical status, Market context, Artifact id, Open |
| Alerts | Alert (code), Symbol, As of, Subject, Transition (previous → current), Sources (role: kind), Artifact id, Open |

Every value is a returned field. Labels are mechanical, so `all_bullish` becomes "All bullish", with the literal value
in `title` and `data-value`.

**Filters** (`FilterBar`, a `role="search"` form):
- **Inputs:** symbol, as-of from, as-of to (both `datetime-local`, in local time), and per page (25, 50, 100, 200).
- **Validation** mirrors the API:
  - the symbol must match `[A-Z][A-Z0-9.-]{0,9}` (input is upper-cased);
  - times must be complete, and "from" must not be after "to".
- **Times** are converted to RFC 3339 UTC with an explicit `Z` (for example `2026-09-24T05:00:00Z`). The exact values
  sent are shown under the bar.
- **Never sent:** invalid input (the request is never made), empty values, and any parameter beyond the five above.

**URL state:**
- The URL holds only the API filter values (`?symbol=META&as_of_from=…`). It never holds the token, a request id or a
  cursor.
- Unknown or invalid URL values are dropped before any request.
- Filter changes are history entries, so browser back and forward work.

**Pagination** (`CursorPagination`):
- The controls are **First page**, **‹ Previous** and **Next ›**.
- There are no page numbers or totals, because the API supplies only `next_cursor`. The summary reads "Showing N items
  (up to L per page)", plus "end of history" when the next cursor is null.
- **Next** uses `meta.next_cursor` exactly.
- **Previous** pops an **in-memory** cursor trail. Each page visit is a history entry whose `location.state` holds a
  random key into a module-level map, so browser back and forward retrace the pages.
- **Where cursors live:**
  - never in storage or the URL;
  - forgotten on sign-out and on a global 401;
  - after a reload the list starts at the first page.
- **Polling:** only the first page polls (60 s). Later pages stay stable, keeping cursors valid.
- **Page changes:** the previous rows stay visible (dimmed, `aria-busy`) while the next page loads. Rows are never
  carried across a filter change.
- **Rejected cursor** (400): "The API did not accept this query or page cursor", with **Back to the first page**.

**Latest for symbol** (`LatestForSymbol`): this needs a symbol filter, since there's no symbols endpoint. The lookup
runs on request with **Show latest for S**, via `…/latest?symbol=S`.

| Response | Shown |
|---|---|
| 200 | as of, key state, the id and **Open** |
| 404 | "No market intelligence exists for S." / "No alert exists for S." |
| 409 `ambiguous_latest` | "The latest … for S is ambiguous. More than one artifact shares the most recent sealed as_of time, and MIAS does not choose between tied artifacts", with a pointer to the history, where the tied artifacts appear first. No artifact is picked or linked. |

## Detail pages

The shared detail frame (`ArtifactDetailFrame`) provides:
- breadcrumbs, and the `h1` (which receives focus on navigation);
- the full id in the subtitle;
- WAI-ARIA tabs **Summary** and **Raw / Canonical**, using arrow keys, Home and End; the tab is in the URL, so deep
  links and back work;
- detail data that is immutable by id and never re-polled.

**Id handling:**
- A malformed id (not `sha256:` plus 64 lowercase hex) shows an explanation and **makes no request**.
- A 404 shows "Not found. No … with this id exists", with a link back to the list.

**Market Intelligence summary**, with every view field exactly once, verbatim:

| Section | Content |
|---|---|
| Observation | symbol; `as_of` in local time plus the original ISO string and UTC |
| State | timeframe pattern, technical status, market context |
| Identity | full artifact id and synthesis id with copy; format and rules versions |
| Attention | a table in recorded order, duplicates kept |
| Conflict codes | an ordered list, duplicates kept |
| Response | `meta`: view, API version, served at, request id |

**Alert summary:**

| Section | Content |
|---|---|
| Event | code, symbol, `as_of` (original and UTC), subject, transition |
| Facts | literal keys and values, verbatim |
| Source references | role, kind, id. Market Intelligence references link to `/market-intelligence/:id`; other kinds show the id with copy, because their detail pages arrive later. |
| Identity | alert id, assessment id (or "None"), format and rules versions |
| Deliveries | see below |
| Response | `meta` |

**Deliveries** (`GET …/{id}/deliveries`):

| Response | Shown |
|---|---|
| **503 `dependency_unavailable`** (this deployment has no receipt root) | "Delivery information is not available in this deployment." This is a neutral notice (`role="status"`, info styling, not red, not `role="alert"`), explaining that no delivery status is shown or implied. The client treats this code as a **definitive capability answer and does not retry it**. Other 503s keep the 16B retry policy. |
| 200 with receipts | only the returned receipts: sequence, channel, status, attempts, attempted and completed times, provider message id, error code |
| 200 empty | "No delivery receipts are recorded for this alert." |
| other errors | the standard error panel |

No `delivered`, `pending` or `failed` state is ever inferred.

**Raw / Canonical** (`CanonicalPanel`), the same for both kinds:
- The text is exactly the `response.text()` of `…/{id}/canonical`, held in memory and shown monospaced and scrollable
  (focusable).
- **Metadata shown:**
  - the ETag, with a check ("Matches artifact id" or "Does not match artifact id");
  - `Cache-Control`;
  - the size in UTF-8 bytes;
  - "Immutable: content-addressed".
- **Copy canonical** writes the original text. **Download canonical** builds a Blob from the original text and saves it
  as `<64 hex>.json`.
- **Formatted (not canonical)** is an optional pretty-printed copy, labelled as not the canonical byte sequence. Copy
  and Download never use it.
- Loading shows a small inline indicator. The canonical body is fetched only when the tab is opened, and cached for the
  session.

## Reusable components and helpers

**Components:**
- `ArtifactId`: short `a5363431…7415` in tables or full in details, plus a copy button and an optional link.
- `CopyButton`: accessible name "Copy id …" or "Copy canonical text", with a polite live status.
- `DomainBadge`: text, symbol and tone.
- `MetadataList`: labelled rows, with the literal field name in small text.
- `DetailSection` and `DetailSkeleton`.
- `FilterBar`, `CursorPagination`, `Tabs`, `CanonicalPanel`, `TableSkeleton`, `Breadcrumbs`, `ResponseMeta`.
- `RequestError`: 404, 409 and 400 wording, otherwise the 16B error panel.

**Helpers:** `lib/artifactId`, `lib/filters`, `lib/cursorTrail`, `lib/domain` (labels and tones) and `lib/canonical`.

**Badge tones** describe only backend-defined system facts:

| Tone | Applied to |
|---|---|
| positive | technical status `available`, market context available, delivery `delivered` |
| changed | technical status `unavailable`, alert codes ending `_changed` or `_invalidated`, attention `conflict` and `gap` |
| info | other alert codes, attention `presence` |
| negative | delivery `failed` only |
| neutral | timeframe patterns and subjects, always; any unknown value |

Every badge has text plus a symbol (●, ✓, △, ✕, ℹ), so colour is never the only signal. There are no buy/sell
colours.

**Query keys** contain only the kind, the id, the filters (every slot present, null when unset, in a fixed order) and
the cursor. They never contain the token, request ids or trace ids.

## Timestamps

- Tables show local time, with the original ISO string and UTC in the tooltip and `dateTime`.
- Detail pages also print `Original …` and `UTC …` under `as_of`.
- `as_of` is never rounded or rewritten.

## Loading, empty and error states

| State | List | Detail | Canonical |
|---|---|---|---|
| Loading | skeleton rows | section skeletons | inline spinner |
| Background refresh | "Refreshing…"; loaded rows kept; a failed refresh shows the panel above the kept rows | — | — |
| Empty | "No market intelligence is available for the selected filters." / "No alerts are available for the selected filters." ("… yet" when unfiltered) | — | — |
| 401 | global sign-out (16B) | global sign-out | global sign-out |
| 404 | — | not found | not found |
| 409 | latest panel explanation | — | — |
| 503 deliveries | — | capability notice | — |
| 5xx or network | retry panel with code and request id | same | same |

No stack traces are ever shown.

## Accessibility

- Tables have captions and `th scope`, with the symbol or alert code as the row header.
- Filter fields have labels; errors use `aria-invalid` and `aria-describedby`.
- Tabs use `tablist`, `tab` and `tabpanel` with `aria-selected` and `aria-controls`, roving `tabindex`, and arrow,
  Home and End keys.
- The canonical display is a `radiogroup`.
- Copy buttons have explicit accessible names, and their result is announced politely.
- Pagination is a `nav` with a live summary.
- The detail `h1` receives focus; every action is a native button or link.
- Statuses and badges are text plus a symbol.
- Tests cover keyboard tab switching, the skip-link and navigation order (16B), and focus on the detail heading.

## Responsive behaviour

- **Desktop first:** 1080p and 1440p, with dense tables and a content width capped at 1760 px.
- **Navigation:** sticky top bar; sticky side navigation and sticky table headers.
- **At or below 1100 px:** details become a single column.
- **At or below 900 px:** the navigation becomes a wrapping row.
- **Tablet:** tables scroll horizontally inside their card.
- **At or below 640 px:** history tables become stacked read-only cards, with labels from `data-label`.

## Visual design

The goal was a restrained research-terminal look:
- neutral surfaces with one accent (blue);
- a spacing scale of 4, 8, 12, 16, 24 and 32 px, and a type scale of 12, 13, 14, 17 and 24 px;
- cards with subtle borders;
- compact uppercase table headers and tabular numerals;
- pill badges, underline tabs and a compact filter bar.

It uses system fonts and has no animation beyond the skeleton pulse and a spinner (both disabled under
`prefers-reduced-motion`), no charts and no external assets. Every colour is a CSS variable redefined for dark mode,
and the text pairs meet AA. The manual theme toggle stays deferred to 16E.

## Tests

There are **128 tests in 12 files**, all passing and stable over 3 runs. That's 74 from 16B (the two Market
Intelligence and Alerts placeholder cases were retired) plus 54 new.

**Fixtures:** `ui/src/tests/fixtures/phase16c.json` is generated by `ui/scripts/capture_api_fixtures.py`. The script
runs the real `create_app` with TestClient over the sealed Phase 13 samples (`tests.test_artifact_store.samples`), in
temporary directories with a placeholder token. It captures:
- full history, `limit=2` page 1 and page 2 (a real cursor), symbol, window and empty filters;
- latest, latest 404, and a **real 409 tie** (two sealed Phase 8 MIs for one symbol and `as_of`);
- details and canonicals for every sample;
- 404, a bad cursor and a bad instant (both 400);
- deliveries 503 (no receipt root), plus deliveries 200 and 200-empty (Phase 12E receipt writer, temporary receipt
  root).

Nothing was hand-written.

| Group | Tests |
|---|---|
| Unit: `tests/unit/domain16c.test.ts` | 21. Id shortening and validation; filter serialization (omit empty, upper-case, reject invalid, local to UTC `Z`, inverted window, URL read and write order, unknown parameters dropped); cursor state machine and memory store; badge tones and the neutral fallback; download Blob bytes equal the original bytes; copy uses the original; formatted copy is separate; deliveries 503 mapping and no retry, while other 503s retry; real 409 handling; deterministic query keys with no token or request id; only the first page polls. |
| Component: Market Intelligence | 16. Loading, loaded, empty; filtering with only supported parameters and URL state; next and previous cursor plus browser back/forward; rejected cursor; latest with no symbol, 200, 404 and 409; detail with every field and no buy/sell/score/recommend text; 404 and malformed id (no request); canonical exact text, ETag, Cache-Control, formatted label, copy and download of the original; keyboard tabs; list to detail and back; failed background refresh keeps rows. |
| Component: Alerts | 11. Loading, loaded (transition shown; no delivery words in the list), empty, window filter parameters; detail (facts verbatim, source-ref links, null assessment); deliveries 503 neutral and fetched once; deliveries 200 shows only returned receipts; deliveries empty; canonical exact text and ETag; 404; source-ref navigation to MI detail. |
| MSW integration | 6. Exact parameter set and order, auth only in the header and never in the URL, request-id format; exact backend cursor; latest is symbol-only; canonical text with ETag and Cache-Control; detail equals the API body; deliveries 503 with request id, a single call. |

## Gates

| Gate | Result |
|---|---|
| `npm ci` (clean) | ok, 305 packages, **no dependency changes** |
| `npm run typecheck` | ok |
| `npm run lint` (`--max-warnings=0`) | ok |
| `npm test -- --run` | 128/128 |
| `npm run build` | `index.html` 0.5 KB, JS 359 KB (108 KB gzip), CSS 15 KB |
| `npm run audit:bundle` | **PASS**: no source maps, tokens, storage APIs, inline scripts or styles, CDN, analytics or unexpected URLs |

The first build failed the audit on a React Router `useSearchParams` warning string that contains a GitHub URL. I fixed
it by not using that hook (the detail tab is read with `useLocation` and `useNavigate`), so the string is tree-shaken
out. The audit allowlist is unchanged.

## Container validation (local Podman; nothing pushed)

The image `localhost/mias-ui:d44ed65bc6e5` is revision `d44ed65bc6e5a64b2d9f35e5b5155524a0631641`, about 339 MB. The
Containerfile and nginx config are unchanged from 16B.

**Matrix: 66/66.** These are the 59 checks from 16B plus 7 new ones:
- `/market-intelligence` (with and without filters), `/market-intelligence/<id>` (with and without `?tab=canonical`),
  `/alerts` and `/alerts/<id>` each return `index.html` with `no-store` and CSP;
- `/api/v1/alerts/<id>/deliveries` is proxied, never SPA.

**End-to-end against the real Phase 15 API image: 20/20.** These are the 15 checks from 16B plus 5 new ones:
- cursor page 2 through the proxy (2 + 1 items, last page);
- a cursor reused with a different filter gets 400 (cursors are query-bound);
- deliveries through the proxy get 503 `dependency_unavailable` (no receipt root);
- alert canonical bytes are identical to the stored file, with ETag;
- the `/alerts/<id>` deep link is served.

The random token was never printed and is absent from the logs.

## Python regression

- **Focused** (`test_helm_ui`, `test_helm_mias`, `test_helm_observability`, `test_openshift_manifests`,
  `test_api_integration`, `test_api_foundation`): 82 passed.
- **Full** (disposable PostgreSQL and Redis, the four exclusions): 2342 passed, 7 skipped, **3 failed**. All three are
  the known baseline failures:
  - Fed runpy smoke scripts;
  - SEC argv script path;
  - the intermittent geopolitical `test_full_runbook_with_in_memory_redis`.

  No new failures.
- **Persistent services:** `mias-postgres` and `mias-redis` have the same container ids and start times before and
  after. The disposable containers were removed.
- **Protected areas:** Phase 6 hashes verified; migration head `0007_technical_evidence_ledger`. No changes outside
  `ui/` and this document.

## Helm and cluster

- **Helm:** no chart change, because the runtime and container configuration are unchanged. `ui.enabled` stays
  `false`.
- **Cluster:** read-only check shows Helm `mias` revision 15 (`mias-0.2.0`, deployed). There are no `mias-ui`
  resources or ImageStream, and the NetworkPolicies and Route are unchanged. Nothing was pushed, upgraded or applied.

## Known gaps

- **Downloads:** canonical downloads use a `blob:` URL with an anchor `download`. They're verified in jsdom (exact bytes
  in the Blob); a real-browser check under the production CSP is part of 16F.
- **Clipboard:** copy needs a secure context, which the edge-TLS route provides. Without it the UI says "Copy
  unavailable — select the text instead".
- **No visual regression:** screenshot tooling isn't used, as agreed. Layout is checked semantically, and screenshots
  can come in 16E or 16F.
- **Latest needs a symbol:** there's no symbols endpoint, so the panel is offered only when a symbol filter is set.
- **Source-ref links:** Trade Setup and Invalidation Check references aren't linked yet, because those detail views
  come later.
- **Deliveries:** shown only on the alert detail page, never in the list (the history view has no delivery data).

## Phase 16D handoff (observability and status views)

- **Reuse:**
  - `DetailSection`, `MetadataList`, `DomainBadge` (add a `health` kind with the same text-plus-symbol rule),
    `RequestError` and `TableSkeleton`;
  - the polling cadence in `api/queries.ts`;
  - the diagnostics store (`app/diagnostics.ts`), which already records the last success and the last error request
    id.
- **Scope, API-backed only:**
  - richer `/status` with readiness checks over time (client-side, in memory);
  - API version and analytical formats;
  - recent request ids with their outcome, from the client;
  - a link-out note to OpenShift monitoring for metrics.
- **Constraints:**
  - still no Prometheus, collector or Kubernetes calls from the browser, and no browser telemetry, unless it's
    separately approved;
  - no new runtime dependencies or charts unless backed by a time-series endpoint.
- **Carry forward:** the 16C known gaps above, specifically the real-browser download check and the manual theme toggle
  (16E).
