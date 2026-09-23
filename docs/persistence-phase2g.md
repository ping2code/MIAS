# Persistence Phase 2G: lifecycle hardening and identity audit

Phase 2G does three things: it removes duplicated shadow-writer lifecycle code,
adds a read-only audit for geopolitical identity divergence, and records a
durable-identity design recommendation. There is no collector integration, no
migration, no outbox, and no change to collector output, scoring, identity,
promotion, Redis, Telegram, OpenAI or the disabled-by-default switches.

## Shared lifecycle helper

`persistence/shadow_lifecycle.py::ShadowLifecycle` replaces about 300 lines that
the macro, Treasury and geopolitical modules duplicated. It covers only the
module-level lifecycle:

- lazy singleton creation;
- non-blocking submission, with `dropped_initializing`, `failed_initializing` and
  `rejected_shutdown` accounting and a rate-limited warning;
- detached stats merged with the submission counters;
- bounded, idempotent shutdown;
- fork-child reset;
- the atexit drain.

`empty_stats` and `_validate_shutdown` moved into the helper and are re-exported
from `macro_shadow`, so every existing import still works.

Each collector module still owns:

- its writer class, adapter and persist callback;
- its logger and message text;
- its `application_name` and reconciliation function;
- its config switch in `shared/config.py` and the collector-side `_shadow` gate.

The binding is one declaration per module:

```python
_lifecycle = ShadowLifecycle(globals(), writer="TreasuryShadowWriter", label="Treasury")
submit_treasury = _lifecycle.submit
get_persistence_stats = _lifecycle.get_persistence_stats
shutdown = _lifecycle.shutdown
close_shadow = _lifecycle.close
_after_fork = _lifecycle.reset
```

The lifecycle state stays in the owning module's namespace and is read at call
time:

- `_writer`, `_lock`, `_shutdown_requested`;
- `_submission_lock`, `_submission_stats`, `_submission_last_warning`;
- the writer class and `logger`.

This is intentional. These module attributes are the established patch points
and fork-reset surface, so behavior and every prior test stay unchanged. The
helper is a small binding, not a framework. The writer class (`ShadowWriter`) is
unchanged.

### Equivalence evidence

- `tests/test_shadow_lifecycle.py` was written and passed against the
  pre-refactor modules first, then kept unchanged. For all three collectors it
  checks:
  - the public lifecycle surface;
  - that stats never initialize a writer;
  - lazy singleton reuse and `make_current` passthrough;
  - exact submission-failure counters and a single rate-limited warning with the
    exact text and logger;
  - disabled shutdown shape, idempotence and validation;
  - shutdown delegation and stats merging;
  - fork reset;
  - identical counters across modules for the same outage, queue, rejection and
    restart script;
  - identical writer message catalogues, severity, logger and thread names.
- A live PostgreSQL test drives each module's public `submit_*` and `shutdown`
  end to end, including a second, fresh-process pass that deduplicates from the
  database and reconciles cleanly.
- All 530 prior tests pass unmodified. Those tests cover disabled mode, lazy
  initialization, queue-full, drain, timeout, immediate stop, restart, DB outage
  (including real container stop and recovery), concurrent processes, the replay
  corpora, and collector output parity.

## Geopolitical identity-divergence audit

`persistence/geopolitical_audit.py::audit_geopolitical_identity_divergence(repository, *, max_events=10000)`
is read-only.

- It issues `SELECT` statements only and is validated inside a PostgreSQL
  `REPEATABLE READ, READ ONLY` transaction.
- It never touches Redis, merges, repairs or logs, and uses no text similarity,
  embeddings or AI.
- It scans at most `max_events` geopolitical events, ordered by
  `first_seen_at, id`, reports `truncated`, and queries versions and provenance
  in chunks of 500.
- Its output is deterministic and JSON-serializable, suitable for a later CLI or
  dashboard.

The audit indexes persisted facts across all versions and provenance of each
event: `policy_id`, `identity_anchors`, the analyzed `document_id`, provenance
document IDs, and canonical URLs. Every identifier shared by two or more events
forms a group. Groups with the same member set are merged, and each group gets
its strongest classification:

| Classification | Meaning |
|---|---|
| `exact_authoritative_anchor` | Same official instrument anchor (FR number, EO, OFAC notice, FTC case, MOEA release) and the same identity stage (family, stage, revision). The collector would have joined these had its alias state existed: **historical root divergence**. |
| `shared_policy_id` | Same resolved policy root under different event keys. |
| `shared_document_id` | Same official document ID and stage. **Possible** divergence only: publishers reuse native IDs/URLs for new instruments, and the collector deliberately separates them. |
| `informational_only` | Shared anchor or document across *different* stages (proposal, final, amendment and clarification are intentionally distinct), or a shared canonical URL only. |

Each group reports:

- `group_key`, `classification` and `reasons`;
- `event_ids`, `policy_ids` and `stages`;
- `shared_anchors`, `shared_policy_ids` and `shared_document_ids`;
- `provenance_urls`.

The report also carries scan counts and a per-class summary. The wording is
deliberately "possible identity divergence", never "duplicate".

### Interpreting results

- **`exact_authoritative_anchor`:** investigate as root divergence. Both events
  are genuine collector outputs, and both history chains stay intact. Nothing is
  merged. Downstream readers should treat the group as one action with two
  identities.
- **`shared_document_id`:** compare anchors. A reused BIS/White House URL with a
  new explicit instrument is expected to be distinct.
- **`informational_only`:** this is normal, for example a final rule and its
  clarification. It is also the natural input for future cross-event
  relationship rules.

### Alias-expiry validation (real collector identity behavior)

- An event persisted with its alias/policy mapping. After all Redis state expires,
  the same document resolves to the same `event_id`, persistence records a
  duplicate, and the audit reports nothing.
- A BIS release anchored `fr:2026-99901` and an FR companion `[eo:99980,
  fr:2026-99901]` joined one action. After expiry, the companion alone resolves
  to a new root. The audit flags both as `exact_authoritative_anchor`, sharing
  anchor and document `fr:2026-99901` in the same stage.
- The audit leaves every table byte-identical and never touches Redis.
- Unrelated events do not group. The 14-event Phase 2F corpus produces only one
  `informational_only` group (final vs. clarification).
- Two independent divergences produce two groups in deterministic order.
- A reused-URL new instrument is `shared_document_id`, and a shared policy root
  is `shared_policy_id`.

## Durable identity: options for a future phase

Current state: Redis alias and policy keys (365-day TTL) are the only runtime
identity coordinator. PostgreSQL is a historical record and is never read at
runtime.

| | Option 1: Redis runtime identity, PostgreSQL audit only (current) | Option 2: PostgreSQL anchor registry, consulted read-only when Redis misses | Option 3: PostgreSQL is the identity authority, Redis a cache |
|---|---|---|---|
| **Correctness** | Divergence possible after alias expiry or a Redis loss. It is detected afterwards by the audit and never corrected. | Closes the post-expiry gap: a Redis miss falls back to the durable root chosen for an anchor and stage. It needs a precise rule for when a registry hit is trusted, and conflicts must still fail closed. | Strongest single source of truth. Every resolution becomes a DB transaction, and correctness depends on DB consistency and on migrating the existing roots. |
| **Latency** | Unchanged (one Redis Lua call). | Adds one bounded, indexed read, and only on a Redis alias miss, which is rare. The hot path is unchanged. | Adds a DB round trip or transaction on every resolution. |
| **Availability** | DB outage has no effect on alerting. | A DB outage must degrade to today's behavior (Redis-only resolution) with a strict timeout. Otherwise persistence becomes alert-critical. | A DB outage blocks identity resolution, and therefore alerting, unless a cache fallback is designed, which reintroduces Option 1's gap. |
| **Migration risk** | None. | Moderate: an additive `identity_anchors` registry table and a backfill from persisted versions. Existing event IDs keep working. | High: roots and aliases migrate to PostgreSQL, and a dual-write/cutover period brings risk of identity drift. |
| **Collector behavior impact** | None. | Only on Redis-miss resolutions, which may now reuse an old root. That changes event IDs in exactly the divergence cases, so it needs explicit approval and tests. | Changes the resolution path for every event. Freshness, lease and cache semantics would need re-validation. |
| **Operational complexity** | Low: run the audit periodically. | Moderate: registry maintenance, timeout tuning and a degraded-mode runbook. | High: identity-DB SLOs, schema-migration coordination, cache invalidation. |

**Recommendation: Option 2 in a future, explicitly approved phase.** It fixes
the one confirmed correctness gap with the smallest blast radius, keeps Redis as
the hot path, and can be made strictly non-critical: a bounded read on a Redis
miss that falls back to today's resolution on any DB error. Build it with this
phase's audit as the acceptance test: after rollout, `exact_authoritative_anchor`
groups should stop appearing for new events. Until then, Option 1 (this phase)
stays in force, and Option 3 is not recommended while alerting must survive a
PostgreSQL outage.

## Why no `event_relationships` table

It was intentionally not added. Current collectors do not emit trustworthy,
deterministic cross-event relationships:

- same-action companion documents (BIS/FR, White House/FR, public
  inspection/published) are already one event, with multiple provenance rows
  linked to the `policy_id`;
- proposal, final, amendment and clarification are distinct collector events,
  and no collector fact links them;
- the geopolitical collector has no correction or revision concept
  (`revision_id` is always `original`).

A future relationship table needs explicit derivation rules first. For example,
it might accept only "same authoritative anchor, different stage, stage order
proposed < adopted < amended", with an explicit reviewed direction. It should be
validated against the audit's `informational_only` groups before any migration.

## Remaining risks

- Divergence after alias expiry is still possible at runtime. It is now
  detectable, but not prevented.
- The audit scans at most `max_events` events per call (default 10,000; ordered
  and reported as `truncated`). A larger history needs paging by
  `first_seen_at` in a later CLI.
- `shared_document_id` can be a legitimate reused native ID, so it needs
  judgment.
- The lifecycle binding depends on module-namespace names. Renaming a module
  attribute such as `_writer` would break it, and the equivalence tests guard
  those names.
- There is still no outbox. Snapshots submitted during an outage are counted and
  lost, as in 2B–2F.

## Validation

- **PostgreSQL:** 16.15, disposable container (Phase 2C recipe), removed with its
  volume afterwards.
- **Prior tests:** all 530 pass unmodified (153 Phase 1/2A–2D, 101 Phase 2E,
  86 Phase 2F, 190 collector regressions).
- **New tests:** 28 (`tests.test_shadow_lifecycle`: 10;
  `tests.test_geopolitical_identity_audit`: 9 SQLite + 9 PostgreSQL).
