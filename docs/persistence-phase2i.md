# Persistence Phase 2I: geopolitical identity rollout tooling

Phase 2I adds operational tooling and a rollout/rollback runbook for the
[Phase 2H](persistence-phase2h.md) durable anchor registry. It adds no migration
and does not change resolver semantics, authoritative-anchor coverage, the
collector, Redis, scoring, Telegram or OpenAI. Nothing is deployed.

## CLI

```sh
python -m persistence.geopolitical_tools status    [--json] [--max-events N]
python -m persistence.geopolitical_tools backfill  [--json] [--max-events N]
python -m persistence.geopolitical_tools conflicts [--json] [--limit N] [--after CURSOR]
python -m persistence.geopolitical_tools audit     [--json] [--page-size N] [--after CURSOR]
                                                   [--max-events N] [--batch-size N]
                                                   [--save-snapshot FILE | --compare-to FILE]
```

It is standard-library `argparse` with no framework.

- **Database:** read from the process environment (`DATABASE_URL` plus the
  existing `DB_*` limits) through `DatabaseSettings.from_env`. `.env` is never
  read, and `shared.config` is never imported, because importing it would call
  `load_dotenv`.
- **Safety:** importing the module opens no connection, and does not import
  dotenv, Redis or a DB driver; a subprocess test checks this. No command touches
  Redis or the collector.
- **Transactions:** on PostgreSQL every read command runs in a
  `REPEATABLE READ, READ ONLY` transaction (verified with
  `SHOW transaction_read_only`). `backfill` is the only writer, and it writes
  only `geopolitical_anchor_registry`.
- **Output:** errors are bounded and credential-free. A subprocess test against
  an unreachable URL containing a password confirms that neither the user nor
  the password is printed.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Check failed (unhealthy status, or acceptance not met) |
| 2 | Usage error (bad arguments, cursor or snapshot) |
| 3 | Database unavailable or not configured |
| 4 | Schema missing or behind (migration `0003_geo_anchor_registry` required) |

Example, with no credentials in scripts or history (supply `DATABASE_URL` from
your secret mechanism):

```sh
export DATABASE_URL="$(secret-tool lookup service mias-staging-db)"   # illustrative
python -m persistence.geopolitical_tools status
```

### `status`

`status` reports:

- database reachability;
- the Alembic revision against the expected `0003_geo_anchor_registry`, and
  whether the history and registry tables exist;
- registry counts: rows, active, conflicted, and latest `updated_at`;
- the switch values as seen in **this tool process's** environment;
- this process's durable-lookup counters. These are process-local; the
  collector's live counters are only available inside the collector process;
- a bounded audit summary.

The result is `healthy` (exit 0) or not (exit 1), with lists of `issues` and
`warnings`. Conflicted anchors are warnings, not failures, because historical
divergence is expected.

### `backfill`

`backfill` runs only by explicit invocation and refuses (exit 4) unless the
registry table exists at the expected revision. It wraps
`backfill_geopolitical_anchor_registry`:

- it reads persisted geopolitical versions, and is idempotent and safe to rerun;
- it never touches Redis, event history, or historical roots.

It returns `anchors_seen`, `inserted`, `already_present`, `conflicts`,
`skipped_non_authoritative`, the scan counts and `truncated`.

`conflicts` counts conflicting registration *attempts*. When historical roots
tie on `first_seen_at`, order falls back to the row ID, so the attempt count (not
the conflicted anchor set) can vary between rebuilds. Use `conflicts` to review
the anchors themselves.

### `conflicts`

`conflicts` lists conflicted registry anchors, read-only, in deterministic
registry-key order with an opaque keyset cursor (`--after`). Each entry shows:

- anchor type and value, and stage;
- the preserved `current_root` (`policy_id`, `event_key`);
- `conflicting_policy_ids` and `conflicting_event_keys`;
- source document, `first_seen_at`, `last_seen_at` and `updated_at`.

No winner is chosen and nothing is repaired. Lookups on these anchors fail closed
to today's resolver (see Phase 2H).

### `audit`

`audit` wraps the Phase 2G identity-divergence audit. Each group shows:

- classification (`exact_authoritative_anchor`, `shared_policy_id`,
  `shared_document_id`, `informational_only`) and reasons;
- event IDs and policy IDs;
- shared anchors and document IDs;
- provenance URLs.

Groups are "possible identity divergence", never "duplicates".

Paging:

- **History scan:** keyset batches of `--batch-size` over
  `(first_seen_at, id)`. No SELECT returns more than a batch of event rows, and
  version and provenance queries are chunked. Memory holds only the compact
  identifier index. `--max-events` bounds the scan and `truncated` reports it.
- **Output:** deterministic pages of `--page-size` groups, ordered by
  (classification rank, event IDs). `next_cursor` is the last `group_key`. A
  cursor that no longer exists because history changed between pages is
  rejected (exit 2), never silently skipped.
- **Compatibility:** `audit_geopolitical_identity_divergence` keeps its API and
  output. `audit_geopolitical_identity_divergence_page` adds output paging.

**Audit hardening (classification only; runtime identity unchanged).**
Same-stage anchor and document matches are now indexed per identity stage.
Previously, a later legitimate event of a different stage sharing an anchor with
a historical divergence pair could re-classify that pair's group as
`informational_only`, which hid it from a before/after comparison. Now the
same-stage group stays `exact_authoritative_anchor`, and the cross-stage sharing
is reported as a separate informational group. Every Phase 2G and 2H audit test
passes unchanged.

## Acceptance snapshots

```python
before = capture_identity_audit_snapshot(repository)   # read-only
after  = capture_identity_audit_snapshot(repository)
compare_identity_audits(before, after)                  # pure function
```

On the CLI these are `audit --save-snapshot FILE` (refuses to overwrite) and
`audit --compare-to FILE` (exit 0 when accepted, 1 otherwise).

Groups are keyed by `group_key = sha256(classification, sorted event IDs)`. The
comparison reports:

- `historical_groups` (present in both snapshots);
- `new_groups` and `removed_groups`;
- `new_by_classification` and `new_exact_authoritative_anchor`;
- `inconclusive` (either snapshot truncated) and `accepted`.

**Acceptance rule:** historical groups may remain. No **new**
`exact_authoritative_anchor` group may appear, and a truncated snapshot can never
be accepted. A new divergent event joining a historical group changes its member
set and is therefore reported as new. History is never modified to make a
comparison pass.

## Staging rollout sequence

Each step maps to tooling; the end-to-end test automates the whole sequence on
SQLite and PostgreSQL.

1. PostgreSQL is reachable and migration `0003` is applied:
   `status` reports `healthy: True`, `revision_current: True`.
2. Enable `GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED=true` and let history
   accumulate.
3. Run `backfill` once and record the summary.
4. Run `conflicts` and review every conflicted anchor. They are historical
   divergences and lookups on them fall back to today's resolver. Nothing is
   repaired.
5. Take a baseline: `audit --save-snapshot baseline.json`.
6. Enable `GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED=true` (optionally tune
   `..._TIMEOUT_MS`).
7. Exercise a controlled geopolitical corpus or staging traffic.
8. Observe the lookup counters in the collector process
   (`get_durable_identity_stats()`):
   - `lookup_hit` and `redis_hit_bypass` are expected;
   - `lookup_timeout`, `lookup_error` and `lookup_skipped_busy` should stay near
     zero;
   - `lookup_conflict` should correspond only to the anchors from step 4.
9. Rerun the audit: `audit --compare-to baseline.json`.
10. **Acceptance:** exit 0, `new_exact_authoritative_anchor == 0`, and not
    `inconclusive`.

Validated results:

- **Enabled:** the known 2H post-expiry companion reuses the existing root. The
  comparison reports 1 historical group, 0 new `exact_authoritative_anchor`
  groups, and exit 0. On PostgreSQL the real bounded lookup counters show exactly
  one hit, no errors and no timeouts.
- **Disabled (same sequence):** the companion diverges and the comparison
  reports 1 new `exact_authoritative_anchor` group with exit 1. Prior behavior is
  reproduced and detected.

Phase 2J executed this sequence in a disposable staging-style environment;
see [Phase 2J](persistence-phase2j.md).

## Rollback

Rollback is a switch change, not a schema change:

1. Set `GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED=false` and restart the
   collector.
2. Leave the registry table and all history intact. No data is deleted.
3. The runtime returns to the exact pre-2H Redis-first resolver. The collector
   never calls the lookup (the test asserts this).
4. Shadow persistence continues independently, if it is enabled, and keeps the
   registry current for a later re-enable.

The validated rollback test shows unchanged registry row counts, unchanged
history tables, and an unchanged migration revision. The known scenario
diverges again exactly as before 2H.

`alembic downgrade 0002_macro_shadow_history` is a schema-management operation
for approved database work only. It is **not** the feature rollback (see
Phase 2H).

## Failure handling

| Situation | Tool behavior |
|---|---|
| `DATABASE_URL` missing | exit 3, "database not configured: DATABASE_URL is required" |
| DB unreachable | exit 3, "database unavailable or operation failed (ErrorType)", no URL or credentials |
| Registry table missing or migration behind | `status` exit 1 with issues; `backfill`/`conflicts` exit 4; `audit` still works |
| No schema at all | `status` exit 1 (`history_tables: false`); `backfill` exit 4 |
| Conflicted anchors | `status` warning; `conflicts` lists them; nothing repaired |
| Empty registry or history | Zero counts, empty pages, `healthy` when the schema is current |
| Large history | Keyset scan in batches; same report at any batch size; `truncated` when bounded |
| Malformed arguments, cursor or snapshot | exit 2, bounded message |

Tooling runs in its own process. It shares no state with, and cannot affect, the
collector runtime.

## Operational caveats

- **Counters are process-local.** The tool cannot read a running collector's
  counters; collect them from the collector process.
- **Audit memory and truncation:** an audit holds an identifier index for up to
  `--max-events` events (default 10,000, max 100,000). A larger history needs a
  higher bound. A truncated audit is inconclusive for acceptance.
- **Snapshots are local JSON files.** They contain public official URLs and
  identifiers, but no credentials.
- **Rerun the backfill after re-upgrading** if the registry was ever dropped.

## Tests

`tests/test_geopolitical_tools.py` has 23 SQLite tests and 24 PostgreSQL tests,
47 in all, with no skips. They cover:

- **CLI and status:** every command, JSON and text output, invalid arguments,
  inert import, and status for an empty database, conflicts, a missing
  table/revision behind, and an unmigrated schema;
- **Failure handling:** DB unavailable, both in-process and via a subprocess with
  a credentialed URL;
- **Backfill:** first run, repeat, conflict, skipped anchor, and writes confined
  to the registry;
- **Conflicts:** fields, paging and read-only behavior;
- **Audit:** empty, one group, multiple groups, paging equal to the full report,
  stale cursor, keyset scan over 120+ events at batch sizes 1/7/50, truncation,
  and the anti-masking regression;
- **Snapshots:** overwrite refusal, bad baseline, historical vs new divergence,
  and truncated-is-inconclusive;
- **Rollout and rollback:** enabled, disabled, and switch-off rollback, with the
  real bounded lookup on PostgreSQL, plus read-only transaction enforcement.
