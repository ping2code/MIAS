# Persistence Phase 2A: PostgreSQL validation

Validated on PostgreSQL **16.15 (Debian 16.15-1.pgdg13+2)**, x86_64,
using Docker image `postgres:16`, digest
`sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6`.
No persistence implementation defects were found. Only tests and documentation
changed; collector, alert, Redis, Telegram, OpenAI, scoring and identity behavior
remain unchanged. No `.env` file is read by this workflow.

## Disposable setup and safe rerun

The container is dedicated to this validation: `mias-test-phase2a-postgres`,
label `mias.disposable-test=phase2a`, database `mias_test_phase2a`, dedicated
container role `mias_test_user`, temporary in-memory database storage, no host
mounts, and port 55432 bound exclusively to 127.0.0.1. Trust authentication avoids
stored credentials for this short-lived local test. Any local user can access this
port while the container runs; use only on a trusted development host and stop it
when finished. This is not a deployment configuration.

Do not substitute an existing database, even one with a test-like name. A name
check alone does not establish that a database is disposable. If this container
name or port is occupied, investigate rather than deleting an existing resource.

```sh
docker run --detach --rm --name mias-test-phase2a-postgres \
  --label mias.disposable-test=phase2a --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_DB=mias_test_phase2a -e POSTGRES_USER=mias_test_user \
  -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55432:5432 \
  postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6
docker inspect --format '{{json .Config.Labels}} {{json .HostConfig.Tmpfs}} {{json .NetworkSettings.Ports}}' mias-test-phase2a-postgres
docker exec mias-test-phase2a-postgres pg_isready -U mias_test_user -d mias_test_phase2a
export TEST_DATABASE_URL=postgresql://mias_test_user@127.0.0.1:55432/mias_test_phase2a
export PYTHONDONTWRITEBYTECODE=1
python -m alembic upgrade head
python -m alembic downgrade -1
python -m alembic upgrade head
python -m unittest tests.test_persistence tests.test_persistence_postgres -v
python -m unittest tests.test_fed_pipeline tests.test_macro_pipeline tests.test_treasury_pipeline tests.test_geopolitical_pipeline -v
git diff --check
docker stop mias-test-phase2a-postgres
unset TEST_DATABASE_URL
```

Stopping this `--rm` container removes it and its temporary database. Do not use
these destructive migration commands against any persistent/non-test database.
The suite itself creates and drops only its generated `mias_phase2a_<uuid>` schemas.

## Results and schema review

- Alembic CLI upgrade head, downgrade -1 and re-upgrade head: passed.
- Live tests independently commit downgrade and re-upgrade, verify only the
  Alembic version table/primary key remain after downgrade, and compare final
  schema with SQLAlchemy metadata (including indexes, types and unique keys).
- JSONB verified by PostgreSQL reflection and nested Unicode/value round-trip.
- UUID identifiers, timezone-aware timestamps, UTC conversion, date-only values,
  NOT NULL flags and absence of server defaults verified. UUIDs and recording
  timestamps are application supplied; direct SQL callers must supply them.
- Circular current-version foreign key is DEFERRABLE INITIALLY DEFERRED and
  enforces event ownership at commit. Parent foreign keys use ON DELETE RESTRICT.
  Orphans, invalid current pointers, duplicate versions and parent deletion fail.
- String/check-constraint publication precision and source-family values work on
  PostgreSQL; no native enum lifecycle is required. Migration names fit PostgreSQL
  identifiers and schema comparison succeeds without truncation drift.
- 32 persistence unit tests and 28 live PostgreSQL tests pass (60 total).
- All 190 existing collector regression tests pass. `git diff --check` passes.
- Five concurrent-writer tests pass: same event, same version, identical
  provenance, conflicting version and two current-version promotions.

## Transactions and concurrency

Tests use separate sessions/connections for workers. The first transaction holds
its write open until `pg_stat_activity` confirms the second transaction waits on a
PostgreSQL lock; these are actual overlapping writes, not mocked or sequential
calls. READ COMMITTED is the observed default and is unchanged.

Unique identity insertion uses ON CONFLICT DO NOTHING; parent event row locks
serialize version creation and pointer updates. Same key/content resolves to the
same durable row. Provenance uniqueness likewise resolves identical inserts.
Conflicting version content raises IdentityConflict and rolls back the losing
transaction, preserving the committed winner. Explicit promotions serialize;
the second writer in lock order becomes current. This is not publication-date or
semantic revision ordering. Re-observing an old version does not promote it.

Integrity errors escape `transaction()` as a redacted PersistenceError and the
session closes. New sessions remain usable. Application exceptions roll back the
entire logical unit, including prior writes within it. Failed appends preserve
previously committed immutable history and its current pointer. Callers must let
IdentityConflict unwind the transaction rather than swallowing it.

Security checks cover redacted settings/errors, disabled SQL echo/hidden
parameters, lazy engine creation and fresh-process imports without engine
creation, connections or dotenv loading. Explicit TEST_DATABASE_URL config is
used throughout. Raw driver errors are not a public API: runtime callers must use
`transaction()` or the redacted health probe, and must never log URL objects.

## Limitations and next scope

This validates PostgreSQL 16.15 on one local Docker host, not every supported
PostgreSQL release, sustained load, crash recovery, backups or failover. Committed
visibility is tested across sessions; container storage is deliberately ephemeral.
No automatic retry policy for deadlocks, lock timeouts or serialization failures
is introduced. Higher isolation levels are unvalidated. Sessions must not be
shared between threads/processes; create engines after worker fork. Append-only
history remains a repository contract, not a database trigger/role restriction.

Recommended Phase 2B scope: implement and fixture-test one explicit **macro**
canonical adapter, then opt-in shadow persistence for that collector only. Before
wiring writes, define DB-outage handling and transaction boundaries; preserve
existing event/version identities, publication precision and parser-owned facts.
Keep Redis, scoring, alert decisions and Telegram behavior unchanged. Test disabled
mode, success, duplicate collection, revisions, provenance and DB failures against
PostgreSQL and rerun all collector regressions. Defer other collectors, new
symbol/relevance schema, AI/decision history, outbox/delivery guarantees, retention,
production migration permissions and deployment to separately scoped work.
Phase 2B integration is not part of this change.
