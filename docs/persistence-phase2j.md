# Persistence Phase 2J: staging rollout validation and counter visibility

Phase 2J executes the [Phase 2I](persistence-phase2i.md) rollout runbook in a
disposable, local staging-style environment and adds opt-in collector-side
visibility of the durable-identity counters. It changes no identity semantics,
adds no migration, API, server or monitoring framework, and deploys nothing.
Evidence: [staging report](persistence-phase2j-staging-report.md).

## Counter visibility

| Variable | Default | Meaning |
|---|---|---|
| `GEOPOLITICAL_IDENTITY_STATS_LOG_ENABLED` | `false` | Only `true` enables it. |
| `GEOPOLITICAL_IDENTITY_STATS_LOG_INTERVAL_SECONDS` | `300` | 10–86400, otherwise config fails at import. |

When enabled, `collect_geopolitical_events` emits **at most one** INFO line per
interval, at the end of a collection cycle, on logger
`geopolitical_identity_stats`.

- There is no thread or timer, so nothing happens between cycles.
- When disabled, the helper is not even imported.
- Logging failures are swallowed. Tests show identical collector output, Redis
  state, deliveries and AI calls with logging on and off across the full Phase
  2F corpus.

Example line, taken from the staging run:

```
event=geopolitical_identity_stats scope=process lookup_enabled=true lookup_attempted=1 lookup_hit=1 lookup_miss=0 lookup_timeout=0 lookup_error=0 lookup_conflict=0 lookup_skipped_busy=0 redis_hit_bypass=0 registry_inserted=0 registry_existing=0 registry_conflict=0 registry_error=0
```

Field names and order are fixed (`STATS_LOG_FIELDS`). The line contains only
integer counters, never anchors, event bodies, URLs, DB settings or credentials.

**Counter semantics:**

- Counters are process-local operational telemetry. They reset when the process
  restarts or forks; the rate limit and warning limiter reset too.
- Counters are not persisted. The durable historical truth is the PostgreSQL
  history, the registry and the audit.
- The line shows what one collector process has done since it started. Use
  `persistence.geopolitical_tools` for durable state.

## Staging environment

These are the exact settings used for the recorded run. Nothing depends on
`.env`, because the runner disables `load_dotenv` before any configuration
import and passes only explicit process environment.

- **PostgreSQL:** 16.15 from the pinned Phase 2C image digest.
  - Container `mias-test-phase2c-postgres`, label `mias.disposable-test`.
  - Bound to `127.0.0.1:55432` only, with a private anonymous volume.
  - Dedicated `mias_test_phase2j_staging` database, created for the run.
- **Redis:** a separate disposable `mias-test-phase2j-redis` container.
  - Pinned local `redis:7-alpine` digest, label `mias.disposable-test=phase2j`.
  - Bound to `127.0.0.1:56379` only, tmpfs data, with RDB and AOF disabled.
  - The existing development `mias-redis` container was never touched.
- **Collector:** the real collector code paths (Redis-first resolver with the
  Lua script on a real Redis server, shadow writer, bounded durable lookup,
  registry, audit and CLI).
  - HTTP, OpenAI and Telegram are stubbed, and the clock is fixed.
  - Documents are the synthetic Phase 2F corpus.
  - Collector restarts between phases are simulated by draining and resetting the
    process-local writer and lookup singletons.

**Safety:** `tests/staging_rollout.py` refuses to run unless all of these hold:

- the database is `mias_test*` on a loopback host;
- Redis is loopback and **empty at start**;
- no credential-like token appears in operational output.

Any acceptance failure raises `StagingStop`, and history is never repaired.

```sh
TEST_DATABASE_URL=postgresql://mias_test_user@127.0.0.1:55432/mias_test_phase2j_staging \
MIAS_PHASE2J_REDIS_URL=redis://127.0.0.1:56379/0 \
python -m tests.staging_rollout --report docs/persistence-phase2j-staging-report.md
```

## Rollout procedure as executed

1. **Pre-2H history:** the schema was at `0002`, with shadow on and lookup off.
   The joined BIS+FR action and the joined USTR+FR action were persisted. Redis
   expiry was then simulated, the USTR companion arrived alone, and a
   **historical divergence** was created. Registry writes failed inside their
   savepoint (`registry_error=5`) and history persisted fully (`failed=0`).
2. **Migrate:** `alembic` to head (`0003_geo_anchor_registry`); `status` reported
   healthy.
3. **Backfill once:** 3 events, 5 anchors seen, 3 inserted, 1 already present,
   1 conflict attempt. A rerun inserted 0; it is idempotent.
4. **Conflicts:** exactly one, `fr:2026-99920` (adopted stage, 2 roots), which is
   the historical divergence. It is preserved, and lookups on it fail closed.
5. **Baseline snapshot:** 1 `exact_authoritative_anchor` group (historical).
6. **Enable lookup** after simulated Redis alias/policy expiry, then send
   controlled traffic:
   - the FR companion alone: Redis miss, then a durable hit, reusing the
     existing root as a cached duplicate. No new event;
   - the BIS release: a Redis hit, so the lookup is bypassed;
   - an unrelated new Entity List action: a Redis miss, then a durable miss, and
     it is processed and delivered normally.
7. **Counters (after writer drain):** `lookup_attempted=2`, `lookup_hit=1`,
   `lookup_miss=1`, `redis_hit_bypass=1`, and 0 timeout, error, conflict or
   busy. No warnings were logged, and exactly one rate-limited stats line was
   emitted.
8. **Post-rollout comparison:** 1 historical group, **0 new
   `exact_authoritative_anchor`**, `accepted: true`, exit 0.

## Acceptance criteria and result

| Criterion | Result |
|---|---|
| Zero new `exact_authoritative_anchor` groups | 0 (PASS) |
| No collector behavior regression | Rollout traffic is duplicate or processed as expected; failure cases match lookup-off control runs |
| No identity conflicts introduced | Conflicted anchors: 1 before and 1 after (historical only) |
| No DB lookup on the Redis-hit path | `redis_hit_bypass=1`; hit-with-DB-down gives `lookup_attempted=0` |
| No healthy-run timeout or error | 0 / 0 |

## Failure interpretation

Failure injection was run with shadow persistence off, so nothing was persisted.
Each case was compared with a lookup-off control run on identical Redis state.

| Case | Counter | Warning (bounded) | Processing vs control |
|---|---|---|---|
| DB unavailable on Redis miss (reserved, never-listening port) | `lookup_error=1` | "...lookup failed (PersistenceError); existing resolver used" | identical |
| Lookup timeout (registry held under `ACCESS EXCLUSIVE` lock, 50 ms timeout) | `lookup_timeout=1` | "...lookup timed out; existing resolver used" | identical |
| Conflicted durable anchor | `lookup_conflict=1` | "...conflict on 2 anchor(s); durable root not applied" | identical, landing on the existing historical root |
| Redis hit while DB unavailable | `redis_hit_bypass=1`, `lookup_attempted=0` | none | identical |

How to read the counters:

- **Healthy:** `lookup_hit` and `redis_hit_bypass` increase, and `lookup_miss`
  increases only for actions never seen before.
- **`lookup_error` or `lookup_timeout` above zero:** check database
  reachability, latency or lock contention. Alerting is unaffected, but new
  divergence becomes possible again while lookups fail.
- **`lookup_conflict`:** it should map only to anchors listed by
  `persistence.geopolitical_tools conflicts`. A new conflicted anchor means a
  new divergence was recorded; audit it.
- **`lookup_skipped_busy`:** a slow database; lookups are being shed rather than
  queued.
- **`registry_error`:** the registry table is missing, or its writes are
  failing. History is unaffected.

## Rollback (validated)

Rollback is switch-only. With `GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED=false`
the staging run showed:

- no lookup instance created and no resolver call with a durable lookup;
- `lookup_attempted=0`;
- normal duplicates;
- registry rows (4 → 4) and conflicted anchors (1 → 1) unchanged;
- revision still `0003_geo_anchor_registry`, with no downgrade needed;
- the audit comparison still accepted.

No data was removed. See Phase 2I for the full procedure.

## Staging evidence

The [staging report](persistence-phase2j-staging-report.md) records:

- PostgreSQL version and migration revision;
- pre-registry history;
- backfill and rerun;
- conflicts;
- baseline audit;
- rollout counters and the stats line;
- post-rollout comparison;
- rollback;
- failure injection;
- the final comparison.

It is deterministic and credential-free: a second independent run on a fresh
database produced a byte-identical report.

## Tests

`tests/test_geopolitical_identity_stats.py` has 10 tests:

- **Counter logging:** disabled by default, config validation,
  interval rate limiting, fixed fields and order, identical collector behavior
  with logging on, and counters visible for hit, miss, conflict, error, timeout
  and bypass with bounded, secret-free warnings;
- **Process-local reset:** in-process and in a fresh subprocess;
- **Staging safety:** refusals for a non-test or non-loopback database, a
  non-empty or non-loopback Redis, and credential-like report content;
- **Full runbook on PostgreSQL:** once with the in-memory Redis double, and once
  with the disposable real Redis server (only with the explicit
  `MIAS_PHASE2J_REDIS_URL` opt-in; unknown Redis state is refused).

## Known limitations

- Counters are per process. With several collector processes, each logs its own
  line, and there is no aggregation (intentionally: no monitoring framework).
- The stats line is emitted only at the end of a collection cycle. A collector
  that is not cycling emits nothing.
- The staging run uses synthetic documents and stubbed HTTP, AI and Telegram.
  It validates identity, persistence and tooling behavior, not live source
  coverage or production latency.
- Lock-induced timeout injection relies on the lookup's client-side wait
  expiring before the server-side `lock_timeout`, which is the same value. The
  client timer starts earlier (before connect), so the timeout is observed
  first.

## Next phase recommendation

See the phase completion report. In short: a controlled, time-boxed rollout in a
real shared staging environment using this runbook, with operators capturing the
stats line and audit snapshots over several collection cycles, before any
production enablement decision.
