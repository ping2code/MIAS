# Persistence Phase 2C: macro shadow operations

This phase adds operational accounting, explicit bounded shutdown and read-only
reconciliation to the existing macro-only shadow writer. Collector integration,
config defaults, macro identities, scoring, freshness, Redis, OpenAI and Telegram
are unchanged. PostgreSQL remains non-authoritative. No other collector,
monitoring framework, scheduler, durable queue, replay or delivery state is added.
No schema migration is needed.

## Stats API

```python
from persistence.macro_shadow import get_persistence_stats, shutdown

stats = get_persistence_stats()  # Detached dict; no engine, worker or DB initialization.
result = shutdown(drain=True, timeout=2)
```

Explicit `ShadowWriter` instances expose the same methods. Stats are in-memory,
thread-safe and local to a writer/process; a new writer starts at zero. They are
not durable or aggregated across processes. Counters never affect alert decisions.

| Field | Meaning |
| --- | --- |
| queued | Accepted snapshots, including ones later failed/discarded |
| persisted | Successfully committed snapshots, including duplicates |
| duplicate | Subset of persisted: no new event/version/provenance/history row |
| failed | Tasks that raised before successful commit acknowledgement |
| dropped_queue_full | Submission rejected because the pending queue is full |
| dropped_shutdown | Accepted, not-yet-started tasks discarded during shutdown |
| rejected_shutdown | New submissions rejected after shutdown starts |
| dropped_invalid | Invalid/non-JSON or oversized submissions |
| worker_started / worker_stopped | Worker lifecycle counters |
| queue_depth | Pending tasks, excluding the current task |
| in_flight | 0 or 1; current task not yet accounted as persisted/failed |
| drain_timeouts | One per writer if a drain deadline expires |
| cleanup_failed | Engine disposal failures, separate from task failures |
| last_success_at / last_failure_at | UTC ISO timestamps, or null before an outcome |

The module API also counts `dropped_initializing` (concurrent singleton setup)
and `failed_initializing` (worker construction failure). Last-failure time includes
failed tasks, rejected/discarded work, drain timeout and cleanup failures.

A coherent per-writer snapshot obeys:

```text
queued = persisted + failed + dropped_shutdown + queue_depth + in_flight
duplicate <= persisted
```

Duplicate classification uses the database's actual INSERT RETURNING results,
not a pre-insert existence check or in-memory cache. The repository counts new
rows privately per instance; the writer reports only after commit. Reobserving
first/last observation timestamps does not make a logically identical snapshot
new. A new score, decision or provenance row is a successful nonduplicate snapshot
even when its event version already exists. The existing `persist_macro()` return
value is preserved; `report=True` opts into version-plus-duplicate diagnostics.

## Queue and logging

The default is still a FIFO queue of 64 pending snapshots, each at most 128 KiB
serialized. Tests can select a smaller queue; capacity must be 1–4096. Submission
copies inputs and does not wait for DB I/O or queue space. Short internal locks
coordinate enqueue/dequeue, counters and lifecycle only; no DB calls or log sinks
run under these locks. Queue-full drops are deterministic, counted and never
retried. A condition variable wakes the idle worker promptly at shutdown.

Fixed, payload-free logs distinguish success, duplicate, database/task failure,
queue full, invalid submission, drain timeout and worker start/stop. Repeated
outcomes are limited to one log per kind per minute per writer. Singleton
submission failures and reconciliation mismatches have their own bounded logs.
Counters still count every outcome. No exception text, connection string,
credential, event payload or hidden AI content is logged. Log-sink exceptions
cannot kill the worker. These are ordinary Python logs, not a monitoring system.

## Explicit shutdown

`writer.shutdown(drain=True, timeout=2)` and module `shutdown(...)` stop accepting
work and return a structured result containing `stopped`, `timed_out`,
`unprocessed` and `stats`. Timeout must be finite, between 0 and 30 seconds.

- With `drain=True`, queued tasks run until empty or the caller's deadline.
- On timeout, remaining unclaimed tasks are discarded and counted. The current
  task is reported as `in_flight`, never falsely counted as discarded.
- With `drain=False`, pending work is discarded immediately and thread join waits
  at most 100 ms (or the shorter supplied timeout). No additional queued task runs.
- Repeated shutdown is safe; it does not double-count discarded work/timeouts.
- Idle/disabled shutdown is safe and does not create a worker/engine/session.
- The singleton stays shut down until process restart. Explicit new writer
  instances can be created after an old writer has stopped.
- `close(timeout)` remains a boolean compatibility wrapper; interpreter-exit
  cleanup remains a fallback, not the only shutdown mechanism.

`unprocessed` reports cumulative shutdown discards plus pending/in-flight work in
that returned snapshot. A later snapshot can show the in-flight task completed.
Failed tasks are separately counted; a successfully drained queue can contain
failed attempts, so operators must also inspect `failed` and drop counters.

Python cannot safely kill a thread in a database call. An in-flight transaction
may commit after shutdown returns `stopped=False`; it finishes/rolls back and
releases its session normally. Runtime worker connect/statement/lock timeouts
remain 2 seconds / 1 second / 500 ms, with no application retries. OS/DNS stalls
can exceed driver limits; the caller's bounded join still returns and the daemon
thread cannot hold interpreter exit indefinitely. Tests release deliberately
blocked tasks and verify the worker exits, rather than pretending thread
cancellation is safe. Timeout bounds the wait for the worker, not arbitrary OS
scheduling or slow external logging sinks.

## Outages, restart and failure recovery

A failed task rolls back and closes its session; the worker survives and consumes
later tasks. There is no replay of failed work and no retry loop. Existing engine
pool health checks reconnect for newly submitted work once the database returns.
This was tested with the server genuinely stopped, not only a mocked driver.

A restarted writer needs no previous in-memory state: identical snapshots
deduplicate in PostgreSQL and new material versions append. Existing history and
current pointers survive worker recreation. Spawned processes create independent
engines/sessions and coordinate through existing PostgreSQL unique constraints
and row locks, not process-local locks. Forked children reset singleton state;
spawn is the tested multi-process startup method.

Current-version ordering is unchanged: new promotable snapshots serialize in
lock order; reobserving an old version does not promote it. This phase does not
invent a source-revision ordering policy or turn freshness-skipped events into
alerts. Connection loss around COMMIT can leave an ambiguous outcome: `failed`
means no success acknowledgement, not proof that the server committed nothing.
A subsequent identical observation remains safe to submit.

## Read-only reconciliation

```python
from persistence.database import transaction
from persistence.repository import EventRepository
from persistence.reconciliation import reconcile_macro_event

with transaction(engine) as session:
    result = reconcile_macro_event(expected_event, EventRepository(session))
```

Results include `event_found`, `version_found`, `version_match`,
`current_version_match`, `provenance_match`, `score_match`, `decision_match`,
`ai_match` and a list of mismatch names. Unexpected/missing facts, content hashes,
evidence and expected history snapshots are compared using the same canonical
adapter. Outcomes not expected are null, not fabricated. Retrieval time is not
compared because duplicate evidence retains its first retrieval time.

Use `expect_current=False` for historical/skipped observations: the actual pointer
match is reported, but its mismatch is not an error. Additional history is allowed;
the audit asks whether the expected snapshot exists. The function does not mutate
its input or write, repair, score or replay anything. It does not create an engine
or own the caller's transaction. Tests verify SELECT-only behavior and success
inside PostgreSQL `SET TRANSACTION READ ONLY`. For a consistent multi-query audit
under concurrent writes, callers can explicitly choose a read-only repeatable-read
transaction; this phase does not change default READ COMMITTED isolation.

## PostgreSQL validation and safe rerun

PostgreSQL **16.15 (Debian 16.15-1.pgdg13+2)**, using the exact Phase 2A/2B image
digest. The disposable container is `mias-test-phase2c-postgres`, labeled
`mias.disposable-test=phase2c`, with dedicated database `mias_test_phase2c` and role
`mias_test_user`, on `127.0.0.1:55432` only. A private anonymous Docker volume is
used instead of tmpfs so committed rows survive container stop/start. The
container and its volume must both be removed after validation. Trust auth is for
this temporary local test on a trusted development host only.

```sh
docker run --detach --name mias-test-phase2c-postgres \
  --label mias.disposable-test=phase2c \
  --mount type=volume,destination=/var/lib/postgresql/data \
  -e POSTGRES_DB=mias_test_phase2c -e POSTGRES_USER=mias_test_user \
  -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55432:5432 \
  postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6
docker inspect --format '{{json .Config.Labels}} {{json .Mounts}} {{json .NetworkSettings.Ports}}' mias-test-phase2c-postgres
docker exec mias-test-phase2c-postgres pg_isready -U mias_test_user -d mias_test_phase2c
export TEST_DATABASE_URL=postgresql://mias_test_user@127.0.0.1:55432/mias_test_phase2c
export MIAS_PHASE2C_TEST_CONTAINER=mias-test-phase2c-postgres
export PYTHONDONTWRITEBYTECODE=1
python -m unittest tests.test_persistence tests.test_persistence_postgres \
  tests.test_macro_persistence_adapter tests.test_macro_shadow_persistence \
  tests.test_macro_shadow_postgres tests.test_macro_persistence_operations \
  tests.test_macro_persistence_operations_postgres -v
python -m unittest tests.test_fed_pipeline tests.test_macro_pipeline \
  tests.test_treasury_pipeline tests.test_geopolitical_pipeline -v
git diff --check
docker stop mias-test-phase2c-postgres
docker rm -v mias-test-phase2c-postgres
unset TEST_DATABASE_URL MIAS_PHASE2C_TEST_CONTAINER
```

Do not remove/replace an occupied container name or port. Prove ownership first;
a test-like name alone is insufficient. The destructive outage test requires the
extra explicit container opt-in and checks the name, label, database, role,
volume type and matching loopback port before stopping anything. It restarts that
same container in cleanup. Without the opt-in only that outage test is skipped;
without TEST_DATABASE_URL the live suite is skipped. Skips do not establish live
validation. Tests create/drop their own generated schemas and never access `.env`.

Results:

- All previous **297 tests** remain passing.
- **17 new operational unit tests** and **15 new PostgreSQL tests** pass:
  **329 total**, including all **190 collector regressions**.
- Real outage before a worker's first connection, outage after successful writes,
  then recovery: counted failures, unchanged collector/Redis/Telegram results,
  newly submitted work committed, failed-only event absent, prior history intact.
- Four independent spawned-process contention cases pass: same full snapshot,
  old observation versus material revision, identical new provenance, concurrent
  score/decision appends. The parent observes both processes waiting on actual
  PostgreSQL locks before releasing them. No duplicate logical rows or corrupt
  current pointers; duplicate metrics agree with committed rows.
- The bounded 200-snapshot burst queued **200**, committed **200**, dropped **0**;
  observed elapsed time approximately **6.2–6.6 seconds** on this host. This is a
  sanity check, not a benchmark or throughput SLA.
- Read-only audit matches, missing/mismatching data, rollback, restart, queue
  pressure, bounded drain, immediate shutdown and worker survival pass.
- `git diff --check` passes. No collector, scoring, identity or schema changes.

## Remaining risks and Phase 2D recommendation

Unpersisted queue/in-flight data can be lost on abrupt exit. Outages, queue-full,
shutdown discards and initialization contention remain explicit loss windows.
Stats themselves reset on process restart; there is no durable metric/replay
system. Current-version ordering across workers remains acquisition order for
new versions. Validation is modest local PostgreSQL 16 load, not production
failover, crash recovery or comprehensive deployment certification. Immutability
is still an API contract rather than a DB trigger/permission policy.

Recommended Phase 2D: **macro shadow readiness and observation-order review**.
Use a fixed macro replay corpus to measure reconciliation coverage and explicitly
define acceptable shadow loss, graceful-stop budgets and promotion ordering for
late observations. Produce a deployment runbook covering runtime role permissions,
manual migration review, configuration, rollback-by-disabling and log/stat review.
Keep the switch off by default and PostgreSQL non-authoritative. Do not implement
Treasury/geopolitical/other collector persistence, durable replay/outbox or
DB-driven alerting as part of that work without separate authorization.
