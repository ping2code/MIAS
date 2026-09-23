# Persistence Phase 2M: real-process staging validation

Phase 2M validated two features with real, separate collector OS processes,
real restarts, live official sources, and disposable PostgreSQL and Redis:

- the geopolitical durable identity lookup (Phases 2H–2J);
- Fed shadow persistence (Phase 2L).

It is operational validation only. **No product code changed.** The additions
are a test-only staging harness, its unit tests, and the evidence report:
[persistence-phase2m-staging-report.md](persistence-phase2m-staging-report.md).

## What ran

- **`tests/real_staging_worker.py`:** one OS process per collector process.
  - The collector, Redis client, shadow writer and lifecycle, and durable lookup
    are all the real ones, and HTTP is live.
  - Configuration comes only from the explicit process environment; `.env` is
    never read.
  - Guards replace the Telegram and OpenAI entry points and must count 0. Runs
    use `send_alerts=False` and `enable_ai=False`.
  - It speaks a line protocol: `live`, `controlled` (clearly labelled fixtures,
    no network), `drain_wait`, `exit`, and `hard_exit` (`os._exit` after the
    writer has drained).
- **`tests/real_staging.py`:** the orchestrator. It verifies services, migrates,
  spawns and restarts workers, uses the separate-process
  `persistence.geopolitical_tools` CLI, and snapshots Redis against a shadow-off
  control Redis. It stops and starts the verified PostgreSQL container, runs
  read-only reconciliation, and writes the report.
- **Live geopolitical sources:** a bounded subset of the API and RSS sources:
  Federal Register documents, FR public inspection, FTC and MOEA. The HTML index
  scrapers (BIS, OFAC, Treasury, USTR, White House) were excluded to keep the
  time-boxed run bounded and polite. **The live Fed source** is the official
  monetary-policy feed.

## Environment

Only the labelled, loopback-only containers below were used; `mias-redis` was
never touched. The run database `mias_test_phase2m_staging` is created by the
orchestrator, which refuses to run if it already exists.

```sh
docker run -d --name mias-test-phase2m-postgres --label mias.disposable-test=phase2m \
  --mount type=volume,destination=/var/lib/postgresql/data \
  -e POSTGRES_DB=mias_test_phase2m -e POSTGRES_USER=mias_test_user -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p 127.0.0.1:55432:5432 postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6
docker run -d --name mias-test-phase2m-redis --label mias.disposable-test=phase2m --tmpfs /data \
  -p 127.0.0.1:56379:6379 redis@sha256:c7d14d623c137a1bb6c3a6755b0b0aad499177087c2140eefcf2f122950b172d \
  redis-server --save "" --appendonly no
docker run -d --name mias-test-phase2m-redis-control --label mias.disposable-test=phase2m --tmpfs /data \
  -p 127.0.0.1:56380:6379 redis@sha256:c7d14d623c137a1bb6c3a6755b0b0aad499177087c2140eefcf2f122950b172d \
  redis-server --save "" --appendonly no
python -m tests.real_staging --report docs/persistence-phase2m-staging-report.md
docker rm -f -v mias-test-phase2m-postgres mias-test-phase2m-redis mias-test-phase2m-redis-control
```

The orchestrator refuses to run unless all of these hold:

- containers carry the `phase2m` label, loopback-only port bindings, and private
  volume or tmpfs storage;
- the database is a `mias_test_phase2m*` name on loopback;
- both Redis instances are empty at start;
- no output contains credential-like text.

Any acceptance failure raises `StagingStop`, and nothing is repaired.

## Sequence

1. **Pre-registry history:** schema `0002`, shadow on, lookup off. This includes
   a clearly labelled historical divergence.
2. **Migrate and review:** migrate to `0003`, then `status`, `backfill` (twice),
   `conflicts`, and a baseline audit snapshot, all as separate CLI processes.
3. **Lookup enabled:** after simulated alias/policy TTL expiry, three
   geopolitical processes run: graceful exit, hard exit after drain, then a fresh
   start.
4. **Fed restarts:** three Fed processes run: graceful, hard exit, then fresh,
   with live and labelled fixture cycles and a parity check against a shadow-off
   control Redis.
5. **Failure injection across process boundaries:**
   - the DB unavailable at process start (a reserved, never-listening port);
   - a PostgreSQL stop between cycles inside long-lived processes, after the
     writer drained, followed by recovery;
   - a restart after recovery.
6. **Post-run:** audit comparison, conflicts, status, and final Redis and DB
   evidence.

## Result

Acceptance was **PASS**. The run shows:

- **Live sources:** 150 live geopolitical documents per cycle, all HTTP 200,
  none relevant or fresh today. 15 live Fed entries, of which the configured 10
  were evaluated; all were stale and were shadowed as non-current.
- **Identity and conflicts:** 0 new `exact_authoritative_anchor` groups, and
  conflicts unchanged (only the historical `fr:2026-99920`).
- **Healthy lookup:** exactly 1 hit, 1 miss and 1 Redis-hit bypass. Restarted
  processes made no PostgreSQL lookups on the Redis-hit path.
- **Redis:** Fed state identical to the control, with no delivered markers and
  processed TTLs within contract.
- **Durability and outages:** Fed durable rows stable across restarts. Outage
  failures were counted and logged, outage-only work was not replayed, and
  repeat polls persisted after recovery.
- **Integrity:** 0 reconciliation integrity mismatches, and 0 Telegram or OpenAI
  calls.

Live timing produced no fresh geopolitical actions and no fresh Fed releases.
Identity-lookup and Fed processed-path behavior was therefore exercised with
clearly labelled synthetic fixtures inside the same real processes. No
live-event claims are made for those paths.

## Finding (not repaired): geopolitical disclosure time promoted after Redis expiry

Fixed in [Phase 2N](persistence-phase2n.md) (disclosure-only promotion rule, audit and
operator-applied correction); the regression rerun is in
[persistence-phase2n-staging-regression.md](persistence-phase2n-staging-regression.md).

Consider this sequence:

1. An action is persisted with an earliest disclosure of 12:00 (BIS release)
   and a companion document at 13:00.
2. Redis alias and policy state expires.
3. The companion arrives alone. The durable lookup correctly reuses the same
   event.

The collector's cached result now carries 13:00, because Redis no longer
remembers 12:00. The Phase 2D promotion rule treats a strictly later disclosure
as newer material, so the durable current version becomes the 13:00 one. A
later re-observation of 12:00 is a duplicate of the older version and cannot
re-promote.

Identity, history, audit, Redis and alerting are unaffected, and the collector
itself also uses 13:00 at runtime (pre-existing cold-start behavior). The
durable *current* disclosure is less accurate than the history it holds, so
this should be fixed deliberately in Phase 2N.

## Harness lessons (fixed in the harness, not the product)

- **Synthetic fixtures must not reuse official identifier patterns.** A Fed
  fixture URL (`monetary20260916a`) matched the real 2026-09-16 FOMC statement.
  Persistence behaved safely: it held the synthetic version as `ambiguous` and
  kept the real one current. Fixtures now use `mias-staging-fixture-*` URLs.
- **Import modules that clear the environment before threads start.** Importing
  a module that clears `os.environ`, while a background writer is lazily
  creating engines, produces misleading configuration failures. Fixture modules
  are now imported at process start.
- **Stop the database between cycles, not during queued writes.** A database
  stop immediately after a cycle can race the async writer. That is
  acceptable best-effort loss (counted), but the planned between-cycles outage
  now waits for the writer to drain.

## Remaining risks and limits

- This was time-boxed local staging. A multi-day soak in real shared staging,
  with collectors running on a real schedule, remains future operational work.
- Live traffic today exercised fetch, parse, freshness and stale shadowing only.
  Fresh live geopolitical actions and fresh Fed releases were not available.
- The HTML-index geopolitical sources were not part of the live subset.
- Counters and stats are per process.
