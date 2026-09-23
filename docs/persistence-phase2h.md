# Persistence Phase 2H: durable geopolitical anchor registry

Phase 2H implements Option 2 from [Phase 2G](persistence-phase2g.md). A durable
PostgreSQL anchor registry is consulted with a bounded, read-only lookup, and only
when Redis cannot resolve a geopolitical identity. Redis stays first, and
PostgreSQL does not become the resolver. The feature is off by default.

Nothing else changes: no Fed, SEC or news integration, no `event_relationships`,
no outbox, no historical merge, and no change to scoring, relevance, thresholds,
Telegram, OpenAI, Redis key formats or TTLs.

## Switches

| Variable | Default | Meaning |
|---|---|---|
| `GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED` | `false` | Only `true` enables the lookup. |
| `GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS` | `250` | Must be 10–5000, otherwise config fails at import like other invalid settings. |

When the lookup switch is off, the collector never imports the lookup module and
calls `resolve_identity(..., durable=None)`. That is the pre-2H code path, with
the same Redis calls in the same order, and tests check the Redis call sequence
exactly.

Registrations are written by geopolitical shadow persistence
(`GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED`) or by the backfill. The lookup switch
alone only reads.

## Registry schema (migration `0003_geo_anchor_registry`)

The migration is additive and alters no existing table. The revision ID is short
because `alembic_version.version_num` is `VARCHAR(32)`; live PostgreSQL
validation caught this.

`geopolitical_anchor_registry` columns:

- `id`
- `anchor_type`, `anchor_value`
- `event_type`, `policy_stage`, `revision_id`
- `policy_id` (the resolver root), `event_key` (the collector `event_id`),
  `source_document_id`
- `status` (`active` or `conflicted`)
- `first_seen_at`, `last_seen_at`, `created_at`, `updated_at`
- `attributes` (JSONB: `conflicting_policy_ids`, `conflicting_event_keys`)

Constraints and indexes:

- unique `uq_geopolitical_anchor_registry_key` on
  `(anchor_type, anchor_value, event_type, policy_stage, revision_id)`, which is
  the lookup index;
- `ix_geopolitical_anchor_registry_policy` on `policy_id`;
- checks on anchor type, status, root shape and observation order.

**The key includes the identity stage.** The collector's Redis aliases are
stage-scoped (`digest([anchor, event_type, policy_stage, revision_id])`). An
anchor-only key would merge a proposal, final rule and amendment, which would
change identity semantics. The invariant is therefore:
`(anchor, stage) → at most one active root`.

## Authoritative anchors

Only the instrument anchors the existing resolver already emits are registered.
They are validated by exact pattern:

| Type | Pattern |
|---|---|
| `fr` | FR document number `\d{4}-\d{4,6}` |
| `eo` | Executive order number |
| `ofac` | Notice ID `\d{8}(_\d+)?` |
| `ftc-case` | FTC case number |
| `moea` | MOEA disruption release ID (the resolver appends it as an anchor) |

Normalization lower-cases the type and trims whitespace; nothing else is guessed.
Headlines, summaries, URLs, BIS/USTR native page IDs, RINs, dockets, similarity
and AI are never keys. The resolver does not treat BIS/USTR page IDs as
instrument anchors, so neither does the registry. Anything else is counted as
`skipped_non_authoritative`.

## Redis-first lookup flow

In `collector/geopolitical_identity.resolve_identity`, with the switch on:

1. Compute the same alias keys as today.
2. `GET` each of this request's alias keys; these are extra reads only.
3. If any alias exists, the lookup is bypassed and `redis_hit_bypass` is
   incremented. The unchanged `RESOLVE` script decides and PostgreSQL is never
   touched.
4. If no alias exists, run a durable lookup of `(anchors, stage)`:
   - exactly one consistent active root: it becomes the resolver's candidate
     root;
   - no match: today's candidate is used;
   - conflict: today's candidate is used (fail closed for durable
     reconciliation; see below);
   - timeout, error, or an invalid or garbage result: today's candidate is used.
5. Run the unchanged `RESOLVE` Lua script with that candidate. The script
   re-checks every alias atomically, so a concurrent Redis resolution still wins
   races.

**No reverse sync.** Nothing is copied from PostgreSQL into Redis. The durable
root only replaces the *candidate* of the resolver's own request. The script then
writes this request's alias and policy keys, exactly as it does for every
resolution today. No other key is repaired or populated.

## Timeout and fallback

`DurableIdentityLookup` is created lazily on the first Redis miss. No engine,
session or connection exists before then. Importing the module opens nothing and
reads neither `shared.config` nor dotenv; a subprocess test checks this.

The query runs on one worker thread. The collector waits at most `timeout_ms`
(`future.result(timeout)`). The engine is capped: pool of 1, 1 s pool timeout,
2 s connect (libpq's minimum), `statement_timeout = lock_timeout = timeout_ms`,
`application_name=mias_geopolitical_identity`, and each query runs in
`SET TRANSACTION READ ONLY`.

- If a lookup is still running, later lookups skip immediately
  (`lookup_skipped_busy`) instead of queueing. That is a natural backoff during
  an outage.
- An error disposes the engine, and the next lookup creates a fresh one.
- There is no retry loop and no replay.
- Logs are rate-limited to one line per kind per minute and never include URLs,
  credentials or payloads.

In every failure case the collector continues with today's resolver. Alert
processing never depends on PostgreSQL.

## Conflict semantics

- **Registration:** an attempt to register a different root for an existing
  `(anchor, stage)` never overwrites it. The row keeps its original `policy_id`
  and `event_key`, becomes `status=conflicted`, and records the other root(s) in
  `attributes`. The operation is idempotent, and re-registering the original root
  does not re-activate the row.
- **Lookup:** any conflicted row, or two anchors mapping to different active
  roots, returns `conflict`. `lookup_conflict` is incremented, a bounded warning
  is logged, and **no durable root is applied**. The current event resolves
  exactly as it would today.
- **Why fall back rather than mark unresolved:** marking the event unresolved
  would let registry state withhold alerts, which would make alerting depend on
  PostgreSQL.
- No merge or destructive repair is ever done. `AnchorRegistryRepository.conflicts(limit=...)`
  lists conflicted anchors, read-only.

## Registration and backfill

- **Live registration:** `persist_geopolitical` registers the event's own
  resolved anchors under its root and stage, inside the same transaction but in a
  **savepoint**. A registry failure is counted (`registry_error`) and never loses
  the event history or affects the collector. `last_seen_at` is updated
  idempotently.
- **Backfill:** `backfill_geopolitical_anchor_registry(session, max_events=100000)`
  is an explicit, caller-owned transaction and is not run at migration time. It
  reads persisted geopolitical versions in deterministic order (events by
  `first_seen_at, id`; versions by `observed_at, id`) and registers each resolved
  version's stored anchors, stage and root through the same code. It never
  modifies history tables or Redis, never merges, and is safe to rerun.

It returns `events_scanned`, `versions_scanned`, `versions_skipped`,
`anchors_seen`, `inserted`, `already_present`, `conflicts`,
`skipped_non_authoritative` and `truncated`.

Results on the Phase 2F corpus (SQLite and PostgreSQL 16.15): 14 events, 16
versions, 16 anchors seen, 14 inserted, 2 already present, 0 conflicts,
0 skipped. A rerun gives 0 inserted and 16 already present. The backfill
reproduces live registration row for row. A historical divergence pair backfills
as 1 conflict, and a forged `rin:` anchor as 1 skipped.

## Migration validation (PostgreSQL 16.15)

1. Upgrade to head. The test verifies the columns, unique constraint, both
   indexes, four check constraints, and the `alembic_version` value. The existing
   `compare_metadata` test confirms that models and migrations match.
2. Backfill.
3. Downgrade to `0002_macro_shadow_history`. Only the registry is removed, and
   the history tables are byte-identical.
4. Upgrade again.
5. Rerun the backfill: identical summary.
6. Rerun again: idempotent.

The existing Phase 2B test `test_history_migration_downgrade_preserves_foundation`
used the relative target `"-1"`, which assumed `0002` was head. It now names
`0001_persistence_foundation` explicitly. This is the only change to a prior test;
the test's intent and assertions are unchanged.

## Counters

`persistence.geopolitical_durable_identity.get_durable_identity_stats()` returns
a detached, thread-safe, process-local snapshot:

- lookups: `lookup_attempted`, `lookup_hit`, `lookup_miss`, `lookup_timeout`,
  `lookup_error`, `lookup_conflict`, `lookup_skipped_busy`;
- `redis_hit_bypass`;
- registry: `registry_inserted`, `registry_existing`, `registry_conflict`,
  `registry_error`;
- `last_error_at`, `last_conflict_at`.

There is no Prometheus or OpenTelemetry.

## Acceptance: divergence prevention

This uses the real collector identity behavior.

1. A BIS release anchored `fr:2026-99901` and an FR companion
   `[eo:99980, fr:2026-99901]` join one action. Both are shadow-persisted, which
   registers `fr:2026-99901` for that stage and root.
2. All Redis alias, policy and cache state expires.
3. The companion arrives alone. Before 2H, its first-sorted anchor `eo:99980`
   produced a new root.
4. **Lookup enabled:** a Redis miss leads to a registry hit, and the companion
   resolves to the existing root and `event_id`. Persisting it creates no second
   logical event, and the audit reports **0** `exact_authoritative_anchor`
   groups. This is validated with the real bounded lookup on PostgreSQL and with
   an inline double on SQLite.
5. **Lookup disabled (same scenario):** a new root and a second event appear, and
   the audit still reports the divergence (1 group). The feature is genuinely
   opt-in.
6. **Historical divergence plus enabled lookup:** the second root's registration
   conflicts on `fr:2026-99901`. A later post-expiry companion falls back to
   today's resolver, lands on the existing second event, and creates no new
   event. The audit output is identical before and after, and still reports the
   historical group exactly once.
7. **Enabled with an empty registry:** across the full 27-step Phase 2F corpus,
   the collector's events, stats, Redis state, Telegram calls and AI calls are
   identical to disabled mode.
8. **Outage cases:**
   - Redis miss with the DB unavailable, timing out, or returning no match:
     today's result, same stats, deliveries and AI calls, finishing within the
     bound.
   - Redis hit with the DB unavailable: the DB is never touched, and the event
     is a normal duplicate.
9. **Concurrency:** two processes contend on real PostgreSQL row locks. With the
   same root they produce one row (one inserted, one existing). With conflicting
   roots they produce one row, `conflicted`, holding both roots, and lookup
   returns `conflict`. There is no duplicate row and no silent overwrite.
10. **Index:** 4,000 anchors are registered. `EXPLAIN` of the lookup query uses
    `uq_geopolitical_anchor_registry_key` with no sequential scan, and the lookup
    SQL touches only the registry table.

## Known limitations

- **Registry population.** The registry only knows anchors from shadow-persisted
  (or backfilled) resolved events. For a cached companion document, the
  persisted analysis carries the first document's anchors, so a companion's
  extra anchor (for example `eo:99980`) is not registered. The shared anchor
  still produces the hit.
- **Coverage is historical.** Prevention covers roots that were persisted and
  registered before the Redis expiry. Divergence that already happened stays
  historical and conflicted, is never merged, and remains visible in the audit.
- **An enabled DB hit can change dedup outcomes.** It can make a post-expiry
  companion reuse an old `event_id`. That is the intended identity preservation:
  its processing and delivery then dedup exactly as if the Redis aliases had not
  expired.
- **Cost of the switch.** With the switch on, every resolution adds one `GET`
  per alias key. A Redis `MGET` optimization was not introduced, because the
  test double and the existing Redis usage are call-by-call.
- **Stats** are process-local diagnostics, like the writer stats.

## Rollback procedure

1. Set `GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED=false` and restart. The
   resolver is then the exact pre-2H path; no data changes.
2. Optionally leave the registry in place; it is inert when unused. Registration
   continues while shadow persistence is enabled.
3. Only if the schema itself must be removed, and only through approved database
   operations: `alembic downgrade 0002_macro_shadow_history`. This drops only the
   registry. Existing shadow code keeps persisting history. Registry writes then
   fail inside their savepoint and are counted as `registry_error`, and history
   is unaffected. Disable shadow persistence first if those errors are
   undesirable. Rebuild the registry later with the backfill after re-upgrading.
