# Phase 13C: Artifact store and read APIs

Phase 13C gives the API something durable to read: an immutable, content-addressed store of sealed MIAS objects
(`artifact_store/`, framework-free). On top of it are read-only `/api/v1` routes.

Nothing here changes an analytical contract, adds a PostgreSQL migration, or makes Redis an authority. There are no
actions, no SetupEvaluation routes, and no build, replay or restore over HTTP.

## Store (`artifact-store-v1`)

**Kinds (closed):**

| Kind | Domain validator | Id | Symbol | Sealed `as_of` |
|---|---|---|---|---|
| `market-intelligence` | `trade_setup.validation.validated_market_intelligence` | `intelligence_id` | `synthesis_ref.symbol` | `synthesis_ref.as_of` |
| `options-intelligence` | `trade_setup.validation.validated_options_intelligence` | `options_intelligence_id` | `snapshot_ref.underlying` | `snapshot_ref.as_of` |
| `trade-setup` | `trade_setup.validation.validated_assessment` | `assessment_id` | `inputs.symbol` | `inputs.assessment_as_of` |
| `invalidation-check` | `trade_setup.invalidation.validated_invalidation` | `invalidation_id` | `symbol` | `market_intelligence_ref.as_of` |
| `alert` | `alert_engine.validation.validated_alert` | `alert_id` | `subject.symbol` | `as_of` |

These are the existing pure, structural, fail-closed validators, and each recomputes the content id. The
MarketIntelligence and OptionsIntelligence validators are the Phase 10 upstream-contract checks. They're standalone and
pull in no evidence-layer, `options_data` or provider modules.

**Not stored:** EvidencePacket, EvidenceSynthesis, OptionsSnapshot, SetupEvaluation, technical snapshots, provider
payloads and Phase 6 ledger rows.

**Layout:**

```
<MIAS_ARTIFACT_ROOT>/<kind>/<64 lowercase hex>.json
```

The file holds exactly the object's canonical bytes: sorted keys, compact separators, ASCII, plus one trailing
newline, as every MIAS runner writes. It is **not** wrapped in storage metadata; the path carries the storage
metadata. Files are created with mode 0444.

**Publish (operator CLI only; the HTTP API never takes a path):**

```bash
python -m artifact_store.runner publish --kind alert --file ALERT.json [--root DIR]
python -m artifact_store.runner verify [--root DIR]
```

`--root` defaults to `MIAS_ARTIFACT_ROOT` from the process environment. The root must already exist; a missing kind
directory is created.

Publishing goes through these steps:
1. Read the source as a regular file; a symlink is refused and the file is never modified.
2. Parse it as UTF-8 JSON, rejecting duplicate keys. The size cap is 64 MiB.
3. Run the kind's domain validator, which also recomputes the content id.
4. Name the file from the verified id, then write its canonical bytes:
   - **temporary file:** created with exclusive create and no symlink following, then fsynced;
   - **link:** hard-linked into place, which fails if the name exists (no-clobber);
   - **cleanup:** the temporary name is removed and the directory fsynced.

Results:
- **Identical bytes already present:** `ALREADY_PRESENT`, a no-op.
- **A different body or a non-regular file under the name:** a conflict, which fails closed. Nothing is ever replaced.
- **Pretty-printed source:** accepted; `canonicalized: true` reports that the stored canonical bytes differ from the
  source bytes.
- **No destructive commands:** there is no delete, replace or purge.
- **Exit codes:** 0 ok; 2 invalid input; 3 store unavailable or conflict; 4 validation failure.

**Path safety:**
- **Derived paths only:** every path comes from a closed kind name and a checked id (`sha256:` + 64 lowercase hex).
  Mixed case, other prefixes, other lengths and traversal are rejected.
- **No symlink following:** files are accessed through `O_NOFOLLOW` directory descriptors (root, then kind directory,
  then file). A symlink at the root's last component, at a kind directory or at an artifact file is refused, so
  nothing can escape the root.

## Index

`build_index(root)` walks only the five kind directories. A missing kind directory is an empty kind; other entries in
the root, such as `lost+found`, aren't part of the store. Names are processed in sorted order, so listing order never
matters.

Every entry must:
- be a regular `<hex>.json` file, not a symlink or directory;
- pass its domain validator;
- be named after its own content id;
- be stored in exactly its canonical bytes.

Interrupted-publish temporary files (`.artifact-*.tmp`) are ignored. **Anything else** (an unexpected file, invalid
object, name mismatch, non-canonical bytes or duplicate id) fails the whole build. Nothing is skipped silently.

The index keeps only metadata: kind, id, symbol, the sealed `as_of` text, its UTC instant, and the SHA-256 and size of
the bytes. It never stores bodies, scores or ranks.

**Refresh:**
- **Startup:** the API builds the index when its lifespan starts.
- **Periodic:** it rebuilds every 30 seconds in a daemon thread, which is stopped at shutdown.
- **Swap:** each rebuild produces a complete new snapshot and swaps it in with a single assignment, so readers never
  see a partial index.
- **Failed refresh:** the previous snapshot keeps serving reads, but readiness fails until a refresh succeeds.
- **Not used:** filesystem watchers, Redis pointers and database catalogs.

## Query semantics (locked)

- **Lookup by id:** an exact `sha256:<64 lowercase hex>`. A malformed id is `invalid_request`; an absent one is
  `not_found`. Ids belong to their kind.
- **Symbol:** the MIAS symbol rule `[A-Z][A-Z0-9.\-]{0,9}`. There is no case folding and no fuzzy matching.
- **Instants:** RFC 3339 with an explicit offset (`Z` or `±HH:MM`). They are normalized to UTC **for comparison only**;
  canonical content is never changed. In a query string, `+` must be sent as `%2B`.
- **`latest`** (one kind and one required symbol): the greatest sealed `as_of` ≤ `as_of`, or the greatest overall if
  `as_of` is omitted.
  - **Ties:** if two or more distinct artifacts share that exact instant (equal instants count even when written with
    different offsets), the result is **409 `ambiguous_latest`**. None is picked by id.
- **History:** ordered by sealed `as_of` descending, then id ascending. Optionally filtered by `symbol` and an
  inclusive `as_of_from`/`as_of_to` window.
- **Pagination:** `limit` defaults to 50, maximum 200. `cursor` is opaque (base64url JSON of the last position plus a
  fingerprint of the query), strictly validated, and bound to the query that produced it. There are no page numbers
  and no total count.
  - **Strict parameters:** any unknown or repeated parameter is `invalid_request`.

## Routes (all GET, all under the read policy)

| Kind | Path |
|---|---|
| MarketIntelligence | `/api/v1/market-intelligence` |
| OptionsIntelligence | `/api/v1/options-intelligence` |
| TradeSetupAssessment | `/api/v1/trade-setups` |
| InvalidationCheck | `/api/v1/invalidation-checks` |
| AlertEvent | `/api/v1/alerts` |

Each path has the same four routes:
- `GET <path>?symbol=&as_of_from=&as_of_to=&limit=&cursor=`: history (a list view);
- `GET <path>/latest?symbol=[&as_of=]`: the latest view;
- `GET <path>/{id}`: the view for one id;
- `GET <path>/{id}/canonical`: the exact stored bytes.

Alerts also have `GET /api/v1/alerts/{id}/deliveries`, the delivery receipt view.

**Canonical endpoints:**
- **Body:** the stored bytes untouched, with no pydantic, no re-serialization, no reordering and no whitespace change.
- **Headers:** `Content-Type: application/json`, `ETag: "<content id>"`, and
  `Cache-Control: private, max-age=31536000, immutable`.
- **Re-checked on every read:** the file is re-read through the symlink-proof walk, and its SHA-256 and size must
  equal what was recorded at indexing. A changed or vanished file is `artifact_invalid`; bytes that have since changed
  are never served.

**Views** (`<kind>-summary-v1`) are projections of fields already in the validated object. The only derived values
are counts of lists the object already holds. There are no scores, ranks, recommendations or "best" picks. The
response shape is:

```json
{"data": {...}, "meta": {"api_version": "v1", "view": "...", "request_id": "...", "served_at": "..."}}
```

Lists add `meta.limit` and `meta.next_cursor`. `served_at` and `request_id` are runtime API metadata and never appear
in a canonical body.

**Delivery receipt view** (`alert-deliveries-v1`):
- **Source:** the receipts under `MIAS_RECEIPT_ROOT`, read with `alert_engine.receipts.read_receipts` and filtered to
  this alert, in sequence order. Redis delivered markers are not used.
- **Fields:** `alert_id`, `channel`, `sequence`, `status`, `provider_message_id`, `attempts`, `safe_error_code`,
  `attempted_at`, `completed_at`, `delivery_contract_version`, `render_version`. Receipts never contain tokens, chat
  ids, URLs or rendered text.
- **Errors:** malformed history is `artifact_invalid`; an unset or missing receipt root is `dependency_unavailable`; an
  unknown alert is `not_found`.

## Readiness

`/health/ready` reports these checks:
- `settings`;
- `artifact_root`: configured, and a real directory, not a symlink;
- `artifact_index`: built, and the last refresh succeeded;
- `receipt_root`: only when it's configured; the directory exists and the whole receipt history validates.

Corruption fails readiness. The body holds only check names and `pass`/`fail`: no paths, file names or validation
text. Redis, PostgreSQL, Telegram and providers are not checked.

## Errors and auth

| Situation | Code |
|---|---|
| Invalid id, query or cursor | `invalid_request` |
| Absent | `not_found` |
| Tie at latest | `ambiguous_latest` |
| Revalidation failure or malformed receipts | `artifact_invalid` |
| Store, index or receipt root unavailable | `dependency_unavailable` |
| Anything else | `internal` |

Error messages are the fixed 13B templates. Every `/api/v1` route uses the 13B read dependency, health stays open, and
only GET exists.

## Boundaries and limits

- **Operator vs HTTP:**
  - operator CLI: publish, verify, and the Phase 12 build, replay, restore and run-once;
  - HTTP: read-only.
- **Single instance:** receipt views are complete only where the API sees the whole receipt root (local storage).
  - **Not durable across replicas:** this is not a multi-replica design.
  - **Phase 14 must decide shared receipt persistence** before more than one replica serves delivery views or runs
    delivery workflows: either a verified RWX volume, or an append-only database table in a later, approved migration.
- **Artifact root in Phase 14:** the API only reads it, so it can be a read-only mount. Publishing needs a writable
  shared volume, or a single publisher.
- **No PostgreSQL change:** the migration head stays `0007_technical_evidence_ledger`.
- **Redis:** not read by any 13C route.
