# Phase 13 API contract (locked at Phase 13E)

This is the contract of record for the MIAS API service after Phase 13. It consolidates
`phase13b-api-foundation.md` and `phase13c-artifact-read-api.md`. Where they differ, this document wins.

The lock is enforced by `tests/test_api_integration.py` (end-to-end, restart, route surface, view schemas, codes and
kinds), together with the 13B and 13C suites.

## 1. Service architecture

There is one service, **`mias-api`**, built as a modular monolith with FastAPI and uvicorn.
- **Framework imports:** only `api/` imports FastAPI, Starlette or pydantic.
- **Storage:** `artifact_store/` is framework-free.
- **Domain packages:** they stay framework-free and are unchanged by Phase 13.
- **Scope:** the API serves reads. It never runs collectors, the scheduler or scientific workflows.

## 2. Process boundaries

| Process | Role |
|---|---|
| `python -m api` | HTTP read service |
| `python -m orchestrator...` (scheduler) | Separate process, unchanged; never hosted by the API |
| `python -m artifact_store.runner` | Operator CLI: `publish`, `verify` |
| `python -m alert_engine.runner` | Operator CLI (Phase 12): build, replay, restore-state, restore-delivery, run-once, deliver, verify-* |

## 3. API version

- **Public namespace:** `/api/v1`.
- **Unversioned:** `/health/live` and `/health/ready`.
- **Independent analytical versions:** analytical object versions (`phase8-v1`, `phase9-v1`/`phase9-v2`, `phase10-v1`,
  `phase10-invalidation-v1`, `phase12-v1`) stay independent of the API version. `/api/v1/version` reports them.
- **Store version:** the store format is `artifact-store-v1`.

## 4. Authentication

| Surface | Rule |
|---|---|
| `/health/*` | Open |
| `/api/v1/*` | Read scope. Open only when no `MIAS_API_READ_TOKEN` is set, which is allowed only on a loopback bind. Otherwise it needs exactly one `Authorization: Bearer <token>`, compared in constant time. |
| Operate scope | Reserved. There are no action routes. |

- **Bind rule:** a non-loopback bind without a read token is a configuration error, so the process exits 2 without
  binding. An operator token does not count as read authentication.
- **Failures:** a missing, wrong or malformed credential returns `401 unauthorized` with `WWW-Authenticate: Bearer`.
- **No OIDC or OAuth in Phase 13.**

## 5. Health

- **`/health/live`:** returns 200 `{"status": "live"}`. It runs no checks and touches no dependency.
- **`/health/ready`:** returns 200 when every check passes, otherwise 503. Body:

  ```json
  {"status": "ready|not_ready", "checks": [{"name": "...", "status": "pass|fail"}]}
  ```

  The checks are:
  - `settings`;
  - `artifact_root`: configured, and a real directory, not a symlink;
  - `artifact_index`: built, and the last refresh succeeded;
  - `receipt_root`: only when configured; the whole Phase 12E receipt history validates.

  Readiness never depends on PostgreSQL, Redis, Telegram, providers or OpenAI. The body never contains paths, file
  names or validator text.

## 6–9. Artifact store

**Kinds (closed):**

| Kind | Validator | Id | Symbol | Sealed `as_of` |
|---|---|---|---|---|
| `market-intelligence` | `trade_setup.validation.validated_market_intelligence` | `intelligence_id` | `synthesis_ref.symbol` | `synthesis_ref.as_of` |
| `options-intelligence` | `trade_setup.validation.validated_options_intelligence` | `options_intelligence_id` | `snapshot_ref.underlying` | `snapshot_ref.as_of` |
| `trade-setup` | `trade_setup.validation.validated_assessment` | `assessment_id` | `inputs.symbol` | `inputs.assessment_as_of` |
| `invalidation-check` | `trade_setup.invalidation.validated_invalidation` | `invalidation_id` | `symbol` | `market_intelligence_ref.as_of` |
| `alert` | `alert_engine.validation.validated_alert` | `alert_id` | `subject.symbol` | `as_of` |

**Layout:**

```
<artifact-root>/<kind>/<64 lowercase hex>.json
```

Each file holds only the sealed canonical JSON bytes: sorted keys, compact separators, ASCII, no NaN, plus one
trailing newline. There is no wrapper. Files are created with mode 0444. There are no `latest.json` or `current.json`
files, no pointer files, no database catalog and no Redis pointers.

**Publish (operator CLI only):**

```bash
python -m artifact_store.runner publish --kind <kind> --file <operator path> [--root DIR]
```

1. Read the source as a regular file; a symlink is refused and the file is never modified.
2. Parse it as UTF-8 JSON, rejecting duplicate keys. The size cap is 64 MiB.
3. Run the kind's domain validator, which also recomputes the content id.
4. Store the canonical bytes at the path derived from the verified id:
   - **temporary file:** created with exclusive create and no symlink following, then fsynced;
   - **link:** hard-linked into place, which fails if the name exists (no-clobber);
   - **cleanup:** the temporary name is removed and the directory fsynced.

Results:
- **Identical republish:** `ALREADY_PRESENT`, a no-op.
- **A different body or a non-regular file under the id:** a conflict, which fails closed.
- **No destructive commands:** no delete, overwrite or purge.
- **Exit codes:** 0 ok or already present; 2 invalid input; 3 store unavailable or conflict; 4 validation failure.
- **Paths are operator input only:** the HTTP API never accepts a filesystem path.

## 10. Index, rebuild and refresh

- **Coverage:** a full, deterministic, validated index of the five kind directories, processed in sorted name order.
  A missing kind directory is an empty kind; other root entries are outside the store.
- **Every entry is checked:** it must be a regular `<hex>.json` file that passes its domain validator, is named after
  its id, and is stored in exactly its canonical bytes.
- **Fail closed:** an unexpected file, symlink, directory, invalid object, name/id mismatch, non-canonical bytes or
  duplicate id fails the whole build. Only interrupted-publish `.artifact-*.tmp` files are ignored.
- **Metadata only:** the index keeps kind, id, symbol, the sealed `as_of`, its UTC instant, and the bytes' SHA-256 and
  size.
- **Refresh:** at startup, then every **30 seconds** (fixed) in a daemon thread that stops at shutdown. Each refresh
  builds a complete new snapshot and swaps it in with one assignment, so readers never see a partial index.
- **Failed refresh:** the previous snapshot keeps serving reads and `artifact_index` fails readiness until a refresh
  succeeds. At startup with a corrupt store, nothing is served (`dependency_unavailable`).
- **Rebuilt only from files:** the index is rebuilt purely from the immutable files. Redis and PostgreSQL are not
  involved.

## 11–14. Query semantics

- **Lookup by id:** an exact `sha256:<64 lowercase hex>`, immutable. Malformed is `invalid_request`; absent or another
  kind's id is `not_found`.
- **Symbol:** `[A-Z][A-Z0-9.\-]{0,9}`, exact; no case folding and no fuzzy matching.
- **Instants:** RFC 3339 with an explicit offset (`Z` or `±HH:MM`). They're normalized to UTC for comparison only;
  canonical content is never changed. Send `+` as `%2B`.
- **Latest:** for one kind and one symbol, the greatest sealed `as_of` ≤ `as_of`, or overall when `as_of` is omitted.
- **Ambiguity:** if two or more distinct artifacts share the winning instant (equal instants count even with different
  offsets), the result is **409 `ambiguous_latest`**. There is no id tie-break.
- **History:** ordered by sealed `as_of` descending, then id ascending, with an optional `symbol` and an inclusive
  `as_of_from`/`as_of_to` window.
- **Pagination:**
  - `limit` defaults to 50; the maximum is 200.
  - `cursor` is opaque base64url (the last position plus a query fingerprint), at most 512 characters, strictly
    validated, and valid only for the query that produced it.
  - There are no page numbers, no total count and no unbounded lists.
  - Unknown, repeated or empty parameters are `invalid_request`.

## 15. Route map (complete, GET only)

```
GET /health/live
GET /health/ready
GET /api/v1/version
GET /api/v1/{family}                     history
GET /api/v1/{family}/latest              ?symbol=[&as_of=]
GET /api/v1/{family}/{id}                view
GET /api/v1/{family}/{id}/canonical      exact bytes
GET /api/v1/alerts/{id}/deliveries       receipt view
```

`{family}` is one of `market-intelligence`, `options-intelligence`, `trade-setups`, `invalidation-checks` or `alerts`.

- **Docs:** `/docs`, `/docs/oauth2-redirect` and `/openapi.json` exist only when `MIAS_API_DOCS_ENABLED=true`.
- **Methods:** other methods return 405.
- **Not present:** deliver, run-once, replay, restore, publish and SetupEvaluation routes. Their absence is tested.

## 16. Canonical endpoint

- **Body:** the exact stored bytes, never re-serialized.
- **Headers:** `Content-Type: application/json`, `ETag: "<content id>"`, and
  `Cache-Control: private, max-age=31536000, immutable`.
- **Re-checked on every request:** the file is re-read through `O_NOFOLLOW` directory descriptors, and its size and
  SHA-256 must equal the index. A tampered or deleted file returns **500 `artifact_invalid`**; corrupt bytes are never
  served.

## 17. View schemas (locked field lists, in order)

All views use this envelope:

```json
{"data": ..., "meta": {"api_version": "v1", "view": "<name>", "request_id": "...", "served_at": "..."}}
```

Lists add `meta.limit` and `meta.next_cursor`. `request_id` and `served_at` are runtime API metadata and never enter
a canonical object.

| View | Fields |
|---|---|
| `market-intelligence-summary-v1` | `intelligence_id`, `intelligence_format_version`, `rules_version`, `symbol`, `as_of`, `synthesis_id`, `timeframe_pattern`, `technical_status`, `market_context_available`, `conflict_codes`, `attention[{code, category}]` |
| `options-intelligence-summary-v1` | `options_intelligence_id`, `options_intelligence_format_version`, `rules_version`, `symbol`, `as_of`, `snapshot_id`, `contract_count` |
| `trade-setup-summary-v1` | `assessment_id`, `assessment_format_version`, `rules_version`, `symbol`, `as_of`, `outcome_status`, `no_setup_reasons`, `market_bias_state`, `eligible_side`, `candidate_count`, `market_intelligence_id`, `options_intelligence_id`, `policy_id` |
| `invalidation-check-summary-v1` | `invalidation_id`, `invalidation_format_version`, `rules_version`, `symbol`, `as_of`, `result`, `reason`, `assessment_id`, `side`, `required_pattern`, `observed_pattern`, `observed_technical_status`, `market_intelligence_id` |
| `alert-summary-v1` | `alert_id`, `alert_format_version`, `rules_version`, `alert_code`, `symbol`, `subject_kind`, `assessment_id`, `transition{previous, current}`, `as_of`, `facts`, `source_refs[{role, object_kind, id}]` |
| `alert-deliveries-v1` | `alert_id`, `channel`, `sequence`, `status`, `provider_message_id`, `attempts`, `safe_error_code`, `attempted_at`, `completed_at`, `delivery_contract_version`, `render_version` |

**Views are projections.** They copy existing validated fields; the only derived values are counts of lists the
object already holds. There are no scores, ranks, recommendations or "best" picks.

**Other locked bodies:**
- liveness: `{status}`;
- readiness: `{status, checks[{name, status}]}`;
- version: `{service, api_version, build, analytical_formats[{object, format_version, rules_version}]}`.

## 18. Error contract

```json
{"error": {"code": "<code>", "message": "<fixed message>", "request_id": "<id>"}}
```

| Code | HTTP | Code | HTTP |
|---|---|---|---|
| `invalid_request` | 400 | `conflict` | 409 |
| `unauthorized` | 401 | `payload_too_large` | 413 |
| `forbidden` | 403 | `artifact_invalid` | 500 |
| `not_found` | 404 | `dependency_unavailable` | 503 |
| `method_not_allowed` | 405 (with `Allow`) | `internal` | 500 |
| `ambiguous_latest` | 409 | | |

Each code has one fixed message. Responses never contain exception text, tracebacks, paths, hostnames, database or
Redis URLs, provider errors or secrets. An unexpected exception becomes `internal`, and the log line holds only the
request id, method, route template and exception type.

## 19. Request ids

- **Accepted:** exactly one incoming `X-Request-ID` matching `[A-Za-z0-9][A-Za-z0-9._-]{7,63}`.
- **Otherwise:** a generated 32-hex id replaces it. It is never rejected.
- **Returned:** in `X-Request-ID` on every response and in error bodies.
- **Available to code:** through `api.request_context.current_request_id()`.
- **Never** inside canonical objects.

## 20. Receipt view

`GET /api/v1/alerts/{id}/deliveries` is read-only.

- **Source:** the Phase 12E receipt files under `MIAS_RECEIPT_ROOT`, via `alert_engine.receipts.read_receipts`,
  filtered to the alert, in sequence order. Redis is not used.
- **Fields:** only those in §17. There are no tokens, chat ids, URLs, headers, raw responses, exception text or
  rendered message text.
- **Errors:** malformed history is `artifact_invalid`; an unset or missing root is `dependency_unavailable`; an unknown
  alert is `not_found`.

## 21. Authority boundaries

| Data | Authority | Used by the Phase 13 API? |
|---|---|---|
| Sealed analytical objects | Artifact store files | Yes (read-only) |
| Query and index | In-memory validated index, rebuilt from files | Yes |
| Delivery history | Phase 12E receipt files | Yes (read-only) |
| Alert coordination and delivered markers | Redis (operational cache) | **No**; never the API's historical authority |
| Collector and technical history | PostgreSQL | **No**; no Phase 13 migration (head `0007_technical_evidence_ledger`) |

## 22–23. Restart, recovery and readiness

- **No retained state:** the API keeps no state between runs. A restarted process rebuilds its index from the same
  immutable files and answers identically: same ids, order, views and canonical bytes.
- **No external state needed:** reconstruction needs no Redis, PostgreSQL, provider or network.
- **Receipts:** delivery views are rebuilt from receipt files on every request.
- **Tested:** `test_publish_index_read_and_restart` replays a transcript in a fresh interpreter and checks that no
  Redis, SQLAlchemy, provider or dotenv module was loaded.

## 24. Single-instance receipt limitation

Receipt views are complete only where the API sees the **whole** receipt root. The current design assumes a single
instance, or a fully shared view of that root. Receipt files are not yet a proven persistence mechanism for delivery
across multiple replicas.

## 25. Operator / HTTP boundary

- **Operator CLI:** publishing, store verification, and every Phase 12 build, replay, restore, run-once and deliver
  action.
- **HTTP:** read-only. The HTTP API never takes a path, never writes the store, and never sends anything.

## 26. Security guarantees

- **No client paths, no symlinks:** artifact paths come only from a closed kind and a checked id, and every store
  access refuses symlinks. A traversal URL reads no file.
- **Bounded input:** query parameters are strict and bounded (symbol ≤ 10 characters, cursor ≤ 512, limit ≤ 200,
  instants by pattern). Duplicates and unknown parameters are rejected.
- **No leaks:**
  - no `Server` header (the launcher disables it);
  - debug is off and CORS is off;
  - secrets are hidden from `repr`;
  - the Authorization header is never logged or echoed.
- **Inert imports:** importing `api` or `artifact_store` touches no store directory and loads no dotenv, `shared`,
  `collector`, `analyzer`, `openai`, `requests`, `redis`, `sqlalchemy`, `persistence`, `orchestrator`, `options_data`,
  `setup_evaluation`, uvicorn, Telegram adapter, delivery guard or state store.

## 27–28. Non-goals and Phase 13D

Phase 13 does not provide:
- actions over HTTP (deliver, retry, run-once, build, replay, restore, publish);
- SetupEvaluation routes;
- collection, technical or Phase 10 triggers;
- job queues, SSE or WebSockets;
- OpenTelemetry;
- containers or manifests;
- migrations;
- Redis-backed API state;
- new analytical logic, ranking, scoring, recommendations or AI.

**Phase 13D (HTTP delivery action) is deferred** and is not required for Phase 13 closure.

## 29. Phase 14 handoff (requirements only; no implementation chosen)

**Process:**
- **Command:** `python -m api`.
- **Bind:** `MIAS_API_HOST` and `MIAS_API_PORT` (default `127.0.0.1:8080`). A non-loopback bind requires
  `MIAS_API_READ_TOKEN`.
- **Configuration:** environment only, with no `.env` and no local secret file:
  - `MIAS_ARTIFACT_ROOT` and `MIAS_RECEIPT_ROOT`;
  - `MIAS_API_DOCS_ENABLED`;
  - `MIAS_BUILD_ID`;
  - tokens from a secret store.
- **Probes:** liveness at `/health/live`, readiness at `/health/ready`.
- **Shutdown:** SIGTERM is graceful; the refresher thread stops.

**Storage:**
- **Artifact root:**
  - the API needs read access only;
  - the publisher needs write access;
  - files are immutable and content-addressed, so every replica reads consistently as long as all replicas see the
    same root.
- **Receipt root:** the read API needs the complete history. The current design assumes a single instance or a fully
  shared view.

**Multi-replica:**
- **Artifact reads:** safe across replicas that share an identical root.
- **Receipts:** files are not yet a proven multi-replica delivery persistence mechanism.
- **Phase 14 decides the storage topology** before multi-replica delivery is enabled. Phase 13 deliberately chooses no
  PVC, RWX, object store or database design.

## 30. Contract-change rules

After Phase 13 closes, changing any of the following needs an explicit version or contract review:
- removing or renaming a route;
- removing or renaming a response field;
- canonical endpoint semantics;
- the artifact kind set or path scheme;
- latest or tie semantics;
- cursor semantics;
- error codes;
- authentication behaviour;
- the readiness contract;
- receipt view fields.

Additive fields may be considered later through a deliberate contract decision. They must never alter canonical
objects.

## Operator walkthrough (local, placeholders only)

`runtime/` at the repository root is runtime state. It is git-ignored (`/runtime/`) and must never be committed. Any
location works; this uses a local one.

```bash
export MIAS_ARTIFACT_ROOT="$HOME/MyRepos/MIAS/runtime/artifacts"    # operator/local runtime location only
export MIAS_RECEIPT_ROOT="$HOME/MyRepos/MIAS/runtime/receipts"
mkdir -p "$MIAS_ARTIFACT_ROOT" "$MIAS_RECEIPT_ROOT"
```

```bash
python -m artifact_store.runner publish --kind market-intelligence --file /path/to/MI.json
```

```bash
python -m artifact_store.runner verify
```

```bash
MIAS_API_READ_TOKEN='<placeholder-read-token-32+-chars>' python -m api
```

```bash
curl -s http://127.0.0.1:8080/health/live
```

```bash
curl -s http://127.0.0.1:8080/health/ready
```

```bash
curl -s -H "Authorization: Bearer <placeholder-read-token-32+-chars>" "http://127.0.0.1:8080/api/v1/market-intelligence/latest?symbol=META"
```

```bash
curl -s -H "Authorization: Bearer <placeholder-read-token-32+-chars>" "http://127.0.0.1:8080/api/v1/alerts/<alert id>/canonical"
```

This walkthrough was run against a temporary root during Phase 13E:
- publish reported `canonicalized`;
- readiness passed all four checks;
- a request without a token returned 401;
- the canonical bytes were byte-identical to the stored file;
- there was no `Server` header;
- the token never appeared in the server log;
- SIGTERM shut down cleanly.
