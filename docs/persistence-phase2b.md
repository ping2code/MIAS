# Persistence Phase 2B: macro shadow writes

Only the macro collector submits shadow persistence. Existing scoring, alert
thresholds, source identities, freshness, Redis operations, Telegram delivery and
OpenAI calls are unchanged. Other collectors do not submit persistence. PostgreSQL
is not authoritative for alerts or delivery.

## Opt-in and operational behavior

`MACRO_PERSISTENCE_SHADOW_ENABLED=false` is the shared-config default. Only the
case-insensitive value `true` enables it; invalid values stay disabled. Supply
configuration through the process environment; no `.env` changes are required.
The existing shared-config dotenv behavior is unchanged; no persistence module
loads dotenv. Tests patch that existing loader and never read `.env`.

Disabled collection does not import the shadow module, start a worker, construct
an engine or create a session. Enabled collection lazily submits a deep copy to
one daemon worker per process. Submission follows existing delivery attempts for
processed/cached events. Freshness-skipped events submit without scoring, AI,
Redis or delivery calls. The collector's signature, return values and stats remain
unchanged. Source/state failures and lease-losing workers do not fabricate outcomes
or perform extra source calls merely to produce persistence records.

The queue holds at most 64 snapshots, each at most 128 KiB serialized. Submission
never waits for database I/O or queue space. On overflow, invalid snapshot,
initialization failure or DB failure, the snapshot is dropped. There is **no
retry** for that job. A later ordinary collection may submit the cached result
again; that is not a new Redis, API, AI or scoring pass.

The worker constructs its PostgreSQL engine from explicit `DATABASE_URL` using
existing `DatabaseSettings`. It uses one pooled connection, no overflow, a
1-second pool timeout, 2-second connect timeout, 1-second statement timeout and
500-ms lock timeout. These waits occur only in the worker. DNS/OS network stalls
may exceed driver timeouts, but never hold the collector waiting on a DB result.
Sessions are transaction-local and never shared with the collector. Forked child
processes reset worker state and create their own engine lazily.

Failures use fixed credential-free messages, rate-limited to one warning per
minute per worker (and separately for submission setup failures). Successful
commits log an idempotent-snapshot success message, without payloads or URLs.
At normal interpreter exit, shutdown attempts a bounded two-second drain. A
caller can explicitly invoke `close_shadow()` at shutdown; it is not a delivery
prerequisite. Abrupt exit, a long backlog, queue overflow and outages can lose
shadow observations. This is deliberately best effort, not a durable queue/outbox.

## Adapter and immutable versions

`persistence/adapters/macro.py` accepts normalized macro dictionaries and uses the
existing `event_id` unchanged as `events.event_key`, with `source_family=macro`
and an explicit `macro-v1` identity namespace. It never invokes `macro_identity`,
a parser, scoring, decision evaluation, AI or an external service.

Canonical columns retain headline, summary, source, publisher, official URL,
event type, market scope, publication precision and release stage/revision. A
whitelist in version attributes retains agency, category, reference period,
release ID/revision ID, original publication time, metrics/series identifiers,
source endpoints, symbols and relevance facts. Optional effective time, native
IDs and relevance evidence are retained only if supplied. No META/NVDA symbols or
relevance is inferred. Empty symbol arrays remain empty.

The adapter version is `macro-shadow-v1`. A `macro-content-v1:<sha256>` version key
hashes canonical parser-owned fields, independently of the collector event ID.
The repository also computes its own immutable content hash. Observation/fetch
times, scores, decisions and AI do not enter that version key. Identical facts
reuse a version; changed facts under the same event ID append a version. Cosmetic
headline/URL changes may produce a new snapshot, but never a new logical event.

The existing macro identity includes release stage and explicit correction ID.
Consequently actual corrected releases/GDP stages that already have different
collector IDs remain separate logical events. This phase does not merge them.
Material BLS metric changes under an unchanged ID produce a new immutable version
under the same event. Tests cover both cases. Freshly processed new versions are
promoted. Re-observing an existing old version never moves the pointer backward.
Freshness-skipped observations do not promote over an existing richer version;
the repository still sets the first version as current for a new event. A current
pointer is not alert eligibility. Concurrent promotions use repository lock order,
not a new semantic revision ordering policy.

Aware publication timestamps round-trip as UTC. The current HTML normalizer
encodes date-only Eastern midnight as an instant: the adapter retains the local
calendar date in `publication_date` and leaves canonical `published_at` NULL.
Original parser publication strings remain in provenance. Unknown publication
stays NULL; observation time is never substituted for it. A missing publication
basis is labeled `unspecified`, not falsely attributed to a source statement.

## Provenance and outcome history

Release URL, BLS feed URL and BLS structured-data URL are separately identified
provenance roles where present. Records retain source/publisher, release/native
ID, source publication precision/time and revision ID. Existing release source
hash is preserved when supplied; no raw-response hash is invented. Provenance keys
hash whitelisted evidence excluding retrieval time. Duplicate evidence is
idempotent and retains first retrieval time. If `fetched_at` is absent, retrieval
uses shadow observation time with explicit `retrieval_basis=shadow_observation`.
No raw RSS/HTML/API response, headers, secrets or arbitrary event dump is stored.

Revision `0002_macro_shadow_history` adds only `event_history`; the foundation
migration is unchanged. It has a RESTRICT foreign key to the version, kind check
(`score`, `decision`, `ai`), JSONB attributes, recorded time and a unique
(version, kind, content hash) constraint. Repository operations append/read only.
Identical snapshots deduplicate; changed outcomes append without rewriting facts.
No delivery status is represented by this table.

- Score snapshots copy impact score/level, original score, quality adjustment and
  reasons. Engine/version metadata is included only when already supplied.
- Decision snapshots copy the existing `alert_decision` plus its score snapshot
  and any supplied decision metadata/context. No decision is recomputed.
- AI snapshots copy only complete validated visible summary, sentiment, confidence,
  why-it-matters and event type; provider/model are retained if already supplied.
  No hidden reasoning is stored. Invalid/incomplete AI is omitted. AI does not
  overwrite parser facts, and persistence makes no additional OpenAI calls.
- Freshness-skipped unscored events have no invented score, decision or AI history.

One short transaction writes event/version, provenance and symbol/relevance
attributes, then appends available outcome snapshots. All commit together or all
roll back. Failed new versions cannot change committed prior history/current
pointers. `transaction()` closes sessions on success/failure and redacts database
exceptions; the worker logs no exception text. Isolation stays READ COMMITTED.

## Validation and safe rerun

Validated using **PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2)** and the exact
Phase 2A Docker image digest. `mias-test-phase2b-postgres` is disposable, uses a
`mias.disposable-test=phase2b` label, tmpfs storage, a dedicated test database/role,
and loopback-only port 55432. No existing database is used. Local trust auth is
only for this temporary container on a trusted development host, not deployment.

```sh
docker run --detach --rm --name mias-test-phase2b-postgres \
  --label mias.disposable-test=phase2b --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_DB=mias_test_phase2b -e POSTGRES_USER=mias_test_user \
  -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55432:5432 \
  postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6
docker inspect --format '{{json .Config.Labels}} {{json .HostConfig.Tmpfs}} {{json .NetworkSettings.Ports}}' mias-test-phase2b-postgres
docker exec mias-test-phase2b-postgres pg_isready -U mias_test_user -d mias_test_phase2b
export TEST_DATABASE_URL=postgresql://mias_test_user@127.0.0.1:55432/mias_test_phase2b
export PYTHONDONTWRITEBYTECODE=1
python -m alembic upgrade head
python -m alembic downgrade -1
python -m alembic upgrade head
python -m unittest tests.test_persistence tests.test_persistence_postgres \
  tests.test_macro_persistence_adapter tests.test_macro_shadow_persistence \
  tests.test_macro_shadow_postgres -v
python -m unittest tests.test_fed_pipeline tests.test_macro_pipeline \
  tests.test_treasury_pipeline tests.test_geopolitical_pipeline -v
git diff --check
docker stop mias-test-phase2b-postgres
unset TEST_DATABASE_URL
```

Verify container ownership/storage/port before running; do not replace an occupied
name/port by deleting another resource. Test database naming alone is not proof of
disposability. CLI migration still accepts only a guarded TEST_DATABASE_URL, never
DATABASE_URL. No startup DDL or production migration tooling is introduced.
Integration tests create/drop isolated schemas within the disposable database.

With two migrations, `downgrade -1` removes only history and retains foundation
rows. Foundation full-removal tests now target `base` explicitly; all original
repository/concurrency assertions remain. New tests separately check the one-step
downgrade preserves existing event versions and re-upgrade restores history schema.

Validation includes:

- Six fixture families: BLS CPI, PPI, Employment; BEA GDP/PCE; Census Retail Sales.
- Field fidelity, empty symbols, with/without AI, source evidence, duplicate and
  material/correction versions, date-only and missing-date representation.
- Atomic rollback, preservation of committed history, constraints and concurrent
  idempotency of a complete event/provenance/score/decision transaction.
- Unchanged collector output, stats, Redis call/state traces, Telegram calls and
  AI call count with enabled/disabled/failing persistence; blocked DB/full queue
  still permits delivery to finish. Actual refused DB connections are tested on
  a reserved local non-listening port, avoiding other services.
- 32 foundation unit tests, 28 foundation PostgreSQL tests, 16 adapter unit tests,
  11 shadow behavior tests, 20 Phase 2B PostgreSQL tests: **107 persistence tests**.
- All **190 existing collector regressions** pass: **297 total tests**.
- `git diff --check` passes; no `.env` reads/changes in validation.

## Remaining risks and recommended Phase 2C

Shadow coverage is intentionally incomplete during outages or process termination.
Cached collector results are the source of truth for duplicate observations: this
phase does not fetch fresh metrics or rerun AI/scoring to fill database gaps.
Immutability is a repository contract, not DB trigger/role enforcement. JSONB
symbol/relevance facts are snapshots, not a new relational symbol model. Validation
covers one local PostgreSQL version, not sustained production load or failover.

Recommended Phase 2C: **macro-only shadow operational verification**. Add bounded
submitted/committed/dropped/failed counters and a read-only reconciliation report
comparing saved fixture/replay observations with persisted facts/outcomes. Test
worker shutdown, restart/backlog loss and multi-process observation ordering under
load. Define acceptable shadow-loss and source-version promotion rules before
widening rollout. Keep PostgreSQL non-authoritative for alerts/delivery; do not add
other collectors, new identity algorithms, delivery retries/outbox or production
migration automation without separately scoped approval.
