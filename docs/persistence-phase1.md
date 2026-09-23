# Persistence Phase 1

Current implementation: [Phase 2B macro shadow persistence](persistence-phase2b.md)
adds opt-in macro writes and immutable outcome snapshots. This document records
the earlier phase and its validation scope.


Standalone PostgreSQL foundation; no collector invokes it yet. Existing Redis,
scoring, identities, OpenAI and Telegram paths are unchanged. No dotenv imports,
automatic connections, migrations on startup, raw-source payload storage, or
automatic delivery retries are introduced.

## Stack and scope

SQLAlchemy 2.0.54 Core metadata and Session transactions, psycopg[binary] 3.3.6,
Alembic 1.20.0. PostgreSQL is the primary database. SQLite requires explicit
development/test opt-in and does not prove PostgreSQL locking/concurrency.

Three tables are included:

| Table | Purpose / constraints |
| --- | --- |
| events | UUID identity anchor; unique source family + identity version + exact existing event key; first/last observation and current-version pointer |
| event_versions | UUID append-only repository snapshots; unique event + explicit version key; normalized content hash, source/publication fields, schema/normalizer version, structured attributes, observation and recording times |
| event_provenance | UUID source evidence associated with a version; unique version + provenance key; public URL/document ID/hash/MIME/size/retrieval time and structured attributes |

Foreign keys prevent orphan versions/provenance and current pointers to a different
event. Indexes cover publication/observation time, identity lookups and source
document lookup. PostgreSQL uses UUID, timezone-aware timestamps and JSONB;
SQLite uses equivalent portable types with enforced foreign keys. Publication
precision explicitly distinguishes unknown, date-only, minute and second. A
date-only release is never converted to an invented midnight publication time.

Scores, decisions, AI analyses, symbols/relevance tables, delivery history, run
history, policy aliases and relationships are later milestones. This foundation
does not replace Redis or offer durable delivery guarantees. No purge is supplied;
core records are retained until a separately approved retention implementation.

## Configuration and transactions

`DatabaseSettings.from_env()` reads named process environment variables only:

| Name | Default |
| --- | --- |
| DATABASE_URL | Required for explicit runtime engine construction |
| DB_POOL_SIZE / DB_MAX_OVERFLOW | 5 / 5 per process |
| DB_POOL_TIMEOUT_SECONDS | 10 |
| DB_CONNECT_TIMEOUT_SECONDS | 5 |
| DB_STATEMENT_TIMEOUT_MS / DB_LOCK_TIMEOUT_MS | 15000 / 5000 |
| DB_APPLICATION_NAME | mias |
| DB_ALLOW_SQLITE | false |
| TEST_DATABASE_URL | Optional, explicit test target only |

PostgreSQL URLs normalize to the psycopg driver. Only `sslmode` and `sslrootcert`
URL options are supported. Production credentials should come from deployment
Secrets, using TLS verification and a least-privilege runtime role. Migration DDL
requires a separate role. Production migration/deployment wiring is deferred.
Do not log settings URLs, driver exceptions or SQL parameters. Engine SQL echo is
disabled; public transaction errors and health results are redacted. Applications
must use `transaction(engine)` rather than exposing underlying driver exceptions.

`make_engine(settings)` is lazy and creates no schema. `check_database(engine)`
explicitly probes connectivity with SELECT 1. `transaction(engine)` yields one
short-lived session, commits on success, rolls back on failure and closes it.
Do not share sessions between workers or hold a DB transaction over network calls.
Dispose engines on shutdown; create them within worker processes, not before fork.

## Repository API

`EventRepository(session)` supplies:

- `record(...)`: supplied source family, exact event key/identity version,
  explicit version key, normalized fields and aware observation timestamp.
- `add_provenance(version_id, provenance_key, ...)`: bounded source metadata.
- `current(event_id)`, `versions(event_id)`, `provenance(version_id)`: read APIs.

Repositories never commit. Use them inside `transaction`. PostgreSQL conflict-safe
identity insertion and parent-row locking serialize writes for the same event.
Do not swallow an identity conflict inside a unit of work; let it roll back.
There is no repository update/delete operation for immutable version content.
DB administrators can still alter rows: append-only enforcement is an API contract,
not a database trigger or permission policy in this phase.

Supply a consistent complete normalized field set for a version; the hash includes
normalized fields and structured attributes but excludes observation/recording
times. The same key/content returns the existing version; the same version key
with different content raises IdentityConflict. Different explicit revision keys
may legitimately have the same content. Identity is never inferred from headlines.

The first version becomes current. Later versions require `make_current=True`
to promote; historical backfills default to false. Re-observing an existing version
never moves the pointer. Promotion is a caller-owned source-specific decision;
the repository does not infer legal/release stage ordering. First/last seen times
track extrema, while immutable observed_at and recorded_at preserve the initial
observation and database recording. Repeated provenance keeps its initial retrieval
time; conflicting evidence under one provenance key is rejected. The same official
document may support several versions without a global document uniqueness rule.

Attributes accept JSON objects (64 KiB limit), reject non-finite numbers and common
raw/secret fields recursively. This is defense in depth, not a content scanner:
future adapters must whitelist parser-owned facts and public canonical URLs. Never
pass request headers, secrets, full RSS/HTML/XML/JSON bodies or generic event dumps.

## Migration safety and validation

Revision: `0001_persistence_foundation`. The migration freezes its own schema,
independent of evolving application metadata; upgrade and downgrade are defined.
Offline PostgreSQL SQL can be inspected without a connection or credentials:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m alembic upgrade head --sql
PYTHONDONTWRITEBYTECODE=1 python -m alembic downgrade 0001_persistence_foundation:base --sql
```

Online CLI migration reads only TEST_DATABASE_URL, never DATABASE_URL, and requires
the explicitly provided database name to be `mias_test` or start `mias_test_`.
Operators must supply a genuinely disposable database; naming is an additional
guard, not proof of ownership. Never point test configuration at production.
Programmatic tests supply an explicitly verified connection. Production online
migration tooling must be approved separately. Downgrade deletes foundation tables
and their data; it is for disposable tests only, not production rollback.

The PostgreSQL integration suite creates a unique schema per test in the identified
test database and removes it afterward. Migrations and repository transactions
commit independently, allowing actual concurrent connections. Without
TEST_DATABASE_URL the suite reports skips, which do not constitute live validation.
Phase 2A live validation passed on PostgreSQL 16.15; see
[persistence-phase2a.md](persistence-phase2a.md) for setup, results and limitations.
SQLite migration roundtrips and PostgreSQL offline DDL are independently tested.

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_persistence tests.test_persistence_postgres -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_fed_pipeline tests.test_macro_pipeline tests.test_treasury_pipeline tests.test_geopolitical_pipeline -v
```

## Next scope

Phase 2 should add explicit canonical adapters and event/symbol/relevance persistence
incrementally, starting with macro fixtures and shadow writes. Preserve original
identities, publication precision and parser facts. Define DB-outage behavior before
enabling collector writes. Run PostgreSQL integration and concurrent-writer tests
before production use. AI/decision history, durable delivery attempts/outbox, run
health, Redis alias migration, retention/backups and deployment follow separately.
Telegram success followed by lost confirmation remains an existing ambiguous
delivery case; this phase deliberately does not change it.
