# Persistence Phase 2D: macro readiness contract

Phase 2D defines the operational contract for the existing macro-only shadow path.
It does not integrate another collector, alter alert behavior, or make PostgreSQL
authoritative. The fixed replay corpus and tests use only checked-in/synthetic
fixtures; no live source is fetched and `.env` is not read.

## Fixed replay corpus

`tests/fixtures/persistence/macro_readiness_v1.json` pins corpus version 1 and its
SHA-256 manifest (`macro_readiness_v1.sha256`). `tests/macro_readiness_corpus.py`
reconstructs the same deterministic rows from the existing CPI, PPI, Employment,
GDP, PCE and Retail Sales fixtures. It includes initial, exact duplicate, material,
older, cosmetic, stale, missing-date, AI-enriched, and provenance-duplicate cases,
plus explicit BLS correction cases. Every event has no symbols, as the source
fixtures provide none; no symbols are manufactured.

The replay contains 56 observations across seven logical event anchors. It creates
7 events, 37 immutable versions, 75 provenance rows and 56 score/decision/AI
history rows. The expected duplicate count is 13. The full replay is followed by
a second replay; the second run produces 56 idempotent duplicates and no additional
rows. Reconciliation runs inside one PostgreSQL repeatable-read, read-only
transaction when PostgreSQL is used, and returns a structured summary containing
counts, matched rows, mismatch labels and per-observation results.

## Promotion and late-observation contract

The collector-owned `event_id` remains the logical event key. A version is material
when canonical parser facts or normalized release prose/metrics change. A cosmetic
headline/URL/whitespace change is retained as an immutable snapshot but does not
promote current. An explicit parser correction already has a different collector
identity and therefore becomes a separate event; this phase does not merge it.

`current_version_id` follows these rules while the repository holds the event row
lock:

- First version: becomes current.
- Exact duplicate: reuses the existing version and never changes current.
- Material version with the same identity facts and a strictly newer comparable
  source publication instant/date: becomes current, regardless of arrival time.
- Older material observation: persists but retains current.
- Cosmetic version: persists but retains current, even if it arrives later.
- Stale or missing-date observation: persists only when the caller marks it as a
  historical observation; it never becomes alertable or current by itself.
- Explicit correction/revision with a distinct collector identity: first version
  of that event becomes current within that separate event.
- Conflicting version key: raises `IdentityConflict`; the transaction rolls back.
- Unknown precision, missing publication time, changed identity facts, equal source
  time, or incomparable date/sub-day precision: material snapshot is retained but
  promotion is held as `ambiguous`.

Arrival time, observation time, recording time, fetch time, content hash, numeric
metric direction and score/decision values are not promotion evidence. A late
observation cannot replace a newer current version merely because it arrived later.
The repository reports a promotion reason (`first`, `newer_material`, `older`,
`cosmetic`, `ambiguous`, `duplicate`, or `caller_disabled`); writer stats count
held/ambiguous promotions. This is a small repository policy addition, not a new
identity algorithm or schema.

## Acceptable-loss budget

Shadow persistence is non-authoritative and best effort. The normal-operation
budget is zero expected dropped writes. Any `dropped_queue_full > 0` is an
operational warning. Any `failed > 0`, initialization failure, shutdown discard,
or reconciliation mismatch requires investigation. A short database outage may
lose submitted snapshots; failed work is counted and not replayed. Abrupt process
exit can lose queued and in-flight work. There is no silent-loss category: every
known drop/failure window is represented by counters or a reconciliation gap.

Suggested review thresholds, without automated paging:

- Healthy normal operation: `failed=0`, `dropped_queue_full=0`,
  `dropped_shutdown=0`, `reconciliation mismatches=0`, and recent success.
- Investigate immediately: any queue drop, any reconciliation mismatch, repeated
  failed writes, `promotion_ambiguous > 0` for a source expected to provide ordered
  timestamps, or `drain_timeouts > 0`.
- Disable the opt-in switch before deployment if the database is unreachable,
  migration is not at the expected revision, or failures persist.

A restart resets in-memory counters; reconcile committed state after restart.
Stats are diagnostics, not a durable monitoring stream.

## Shutdown budget

Normal shutdown calls `shutdown(drain=True, timeout=2)` and allows the FIFO queue
to drain. The Phase 2C 200-snapshot sanity burst committed all 200 in about
6.3 seconds overall; the shutdown budget applies to the bounded final drain, not
that whole burst. Services should choose a grace period comfortably above their
observed final queue drain and keep the writer's timeout bounded (maximum 30 s).

If the deadline expires, unclaimed queued work is discarded and counted;
in-flight work is reported separately and is allowed to finish or roll back under
driver timeouts. `drain=False` discards pending work and waits at most 100 ms.
Shutdown is idempotent, does not create a writer when disabled, and rejects new
submissions after module shutdown. A daemon thread cannot be safely killed in a
DB call; a timeout is a bounded wait, not a forced transaction abort.

## Healthy shadow criteria

Readiness for a future deployment review means:

- shadow switch explicitly enabled and `DATABASE_URL` supplied outside source;
- worker started and not stopped unexpectedly;
- queue depth below configured capacity threshold (recommend <50% normally);
- recent successful write;
- failed and queue-drop counters remain zero during the review window;
- PostgreSQL is reachable and migration revision is expected;
- fixed-corpus reconciliation has zero mismatches;
- duplicate/revision/current-pointer rules pass;
- graceful shutdown and restart tests pass;
- no credentials or payloads occur in logs;
- collector output, Redis calls and delivery calls match disabled-mode behavior.

No health endpoint or monitoring framework is introduced. Operators use the
read-only stats API and reconciliation utility.

## Reuse contract for later collectors

Treasury or geopolitical adapters must follow the same template: preserve the
collector-owned identity; whitelist a canonical adapter; default shadow off; lazy
engine initialization; bounded non-blocking queue; credential-free bounded logs;
immutable content versions; deterministic source-based promotion; provenance
idempotency; append-only score/decision/AI snapshots; read-only reconciliation;
thread-safe operational counters; explicit bounded shutdown; transaction-local
sessions; and no change to an alert-critical path. Each collector needs its own
fixed replay corpus, source-specific promotion rules, outage tests, and PostgreSQL
concurrency tests. No collector may infer symbols, re-score, re-evaluate decisions,
make PostgreSQL authoritative, or introduce a replay/outbox mechanism under this
contract.

## PostgreSQL validation summary

Validation uses disposable PostgreSQL 16.15 with a test-only loopback database and
temporary storage, as in Phase 2C. Tests cover full replay/reconciliation, late
observations, promotion reasons, duplicates, stale/missing dates, conflicts,
restart, counters and read-only audits. The Phase 2C concurrent-process tests
remain part of the required suite. Remove the container and storage after tests;
never substitute an unknown database. Full rerun instructions are in the runbook.
