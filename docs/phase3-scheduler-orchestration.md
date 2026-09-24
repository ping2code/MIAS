# Phase 3: scheduler and orchestration

Phase 3 adds a scheduler that runs the existing MIAS collectors independently, on
a cadence, each in its own OS process.

It changes no collector behavior: scoring, identities, persistence, Redis
namespaces and TTLs, Telegram, OpenAI, source URLs, promotion and notification
policy stay exactly as they are. There is no schema change, API or dashboard.

## 1. Architecture

```
                   python -m orchestrator.cli {run | run-once | status-config}
                                          |
                  settings from the process environment only (never .env)
                                          |
   +--------------------------------------v---------------------------------------+
   | Scheduler (orchestrator/scheduler.py): single-threaded, non-blocking loop    |
   |   registry.py -> JobDefinition(name, argv, interval, timeout, offset, skip)  |
   |   next_due += interval (monotonic)   in-memory bounded history   health()   |
   +------+-----------+-----------+-----------+-----------+-----------+-----------+
          | Popen     | Popen     | Popen     | Popen     | Popen     | Popen
          | (own process group, allowlisted env, timeout -> TERM -> KILL)
          v           v           v           v           v           v
     news child   fed child   sec child  macro child treasury child  geopolitical child
   (existing collector entry points; each loads its own configuration as today)
          |           |           |           |           |           |
          +-----------+-----------+-----+-----+-----------+-----------+
                                        v
                    Redis (dedup/state) - PostgreSQL shadow history
                     - Telegram/OpenAI (unchanged collector behavior)
```

Modules:

| Module | Contents |
|---|---|
| `orchestrator/models.py` | `JobDefinition`, `JobStatus`, `JobResult` (structured metadata only) |
| `orchestrator/config.py` | Validated settings from the process environment, and a safe printable view |
| `orchestrator/registry.py` | The six collectors and their commands (argument lists) |
| `orchestrator/job_runner.py` | `ChildRun` and `run_job(definition) -> JobResult`: isolated child execution |
| `orchestrator/scheduler.py` | The loop, overlap prevention, shutdown, `run_once` |
| `orchestrator/health.py` | The health snapshot |
| `orchestrator/cli.py` | `run`, `run-once` and `status-config` |
| `orchestrator/entrypoints.py` | The geopolitical script entry point (the only collector without one) |

The scheduler knows each collector's name, command, cadence, timeout, enabled
state and overlap policy. It never imports collector modules or
`shared.config`, so it never loads `.env` and holds no collector business logic.

## 2. Registry

| Collector | Command (`python -m ...`) | Behavior (unchanged entry-point defaults) |
|---|---|---|
| news | `collector.multi_source_collector` | RSS sources; AI for `ALERT` candidates; delivers `ALERT` items |
| fed | `collector.fed_collector [--send-alerts]` | AI on; Telegram only with `--send-alerts` |
| sec | `collector.sec_collector` | delivers `ALERT` filings; no AI path |
| macro | `collector.macro_collector [--send-alerts]` | AI on; Telegram only with `--send-alerts`; exit 1 on fetch/data errors |
| treasury | `collector.treasury_collector [--send-alerts]` | AI on; Telegram only with `--send-alerts`; exit 1 on fetch/data errors |
| geopolitical | `orchestrator.entrypoints geopolitical [--send-alerts]` | a new wrapper with the Fed-style flags and defaults, calling the unchanged `collect_geopolitical_events` |

`<FAMILY>_SCHEDULE_SEND_ALERTS=true` adds the existing `--send-alerts` flag for
Fed, macro, Treasury and geopolitical. It defaults to false, so notification
policy is unchanged. SEC and News entry points always deliver `ALERT` items
today, exactly as when run by hand; setting the flag for them is rejected. No
shell is used; commands are argument lists with no secrets in them.

## 3. Scheduling model

- **Due times:** each enabled collector's first due time is
  `start + <FAMILY>_START_OFFSET_SECONDS` (monotonic clock). After each firing,
  `next_due = previous_due + interval`, never `now + interval`, so job runtime
  and loop latency cannot cause drift.
- **Stalls:** if the loop was delayed past several intervals, missed slots are
  collapsed to the next future slot (one run, not a burst), counted in
  `missed_slots` and logged as `job_missed_slots`.
- **Polling:** the loop polls running children every 0.25 s and sleeps until the
  next due time or tick.

## 4. Process isolation

- **One process per run:** every run is a new OS process
  (`subprocess.Popen`, no shell, `cwd` = repository root) in its own
  session/process group. Module globals, Redis clients and shadow writers never
  leak between runs, matching the Phase 2S real-process validation.
- **Scheduler death:** on Linux each child is started with `PR_SET_PDEATHSIG`,
  so a killed scheduler (even SIGKILL) takes its direct children with it.
- **Environment:** children receive an allowlisted environment:
  - `PATH`, `HOME`, `LANG`, `LC_ALL`, `TZ`, `VIRTUAL_ENV`,
    `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `DATABASE_URL`;
  - variables starting `MIAS_`, `ALERT_`, `DISPLAY_`, `REDIS_`, `DB_`, `FED_`,
    `MACRO_`, `TREASURY_`, `GEOPOLITICAL_`, `SEC_`, `NEWS_`, `TELEGRAM_`,
    `OPENAI_`, `NEAR_DUPLICATE_`, `HEADLINE_`, `DEDUP_` or `RSS_`;
  - `PYTHONPATH` set to the repository.

  Everything else, such as cloud credentials and agent sockets, is dropped.
  Children still load `.env` themselves exactly as today.

## 5. Intervals, timeouts and offsets

These are staging defaults and examples, **not** tuned production cadence;
production polling intervals remain a separate decision.

| Collector | Interval (s) | Timeout (s) | Start offset (s) |
|---|---|---|---|
| news | 300 | 240 | 0 |
| fed | 600 | 300 | 5 |
| sec | 900 | 300 | 10 |
| macro | 1800 | 900 | 15 |
| treasury | 1800 | 900 | 20 |
| geopolitical | 1800 | 1500 | 25 |

Per-family settings:

- `<FAMILY>_SCHEDULE_ENABLED` (true/false);
- `<FAMILY>_INTERVAL_SECONDS` (60..86400);
- `<FAMILY>_TIMEOUT_SECONDS` (at least 1, and strictly below the interval);
- `<FAMILY>_START_OFFSET_SECONDS` (0..3600);
- `<FAMILY>_SCHEDULE_SEND_ALERTS`.

`<FAMILY>` is `NEWS`, `FED`, `SEC`, `MACRO`, `TREASURY` or `GEOPOLITICAL`.

Global settings:

- `MIAS_SCHEDULER_ENABLED` (default **false**);
- `MIAS_SCHEDULER_DRY_RUN` (false);
- `MIAS_SCHEDULER_HISTORY_SIZE` (50, 1..1000);
- `MIAS_SCHEDULER_SHUTDOWN_GRACE_SECONDS` (30, 0..600);
- `MIAS_SCHEDULER_KILL_GRACE_SECONDS` (10, 1..120);
- `MIAS_SCHEDULER_CHILD_OUTPUT` (`inherit` or `discard`).

Invalid values are rejected at start-up (exit 2); the message names only the
setting. The geopolitical default timeout is long because an all-sources cycle
fetches every HTML article page (see Phase 2S).

## 6. Overlap prevention

A collector never overlaps with itself. If its next slot arrives while the
previous run is still active, the slot is recorded as `skipped_overlap` (logged
as `job_skipped_overlap`) and the schedule moves on. Different collectors run
concurrently.

## 7. Timeouts

- **Deadline:** each run has a hard deadline (`timeout_seconds`). At the
  deadline the child's whole process group gets SIGTERM.
- **Escalation:** if it is still alive after `MIAS_SCHEDULER_KILL_GRACE_SECONDS`,
  it gets SIGKILL. The run is recorded as `timed_out`.
- **Isolation:** only that collector is affected; the loop keeps scheduling the
  others.
- **Signal safety:** process groups are signalled only while their leader is
  alive, so an exited child's reused process ID is never signalled.

## 8. Failure isolation

Failures change only that run's status; the loop continues:

- a non-zero exit is `failed` with `exit code N`;
- death by signal is `failed` with `terminated by signal N`;
- a run that cannot start is `failed` with `failed to start (<ExceptionType>)`.

Tested:

- macro fails while Treasury keeps running;
- a hanging Treasury times out alone;
- News exiting non-zero does not stop Fed and SEC from keeping their cadence.

Only an unhealthy scheduler process itself stops the loop.

## 9. Shutdown

SIGINT and SIGTERM only set a stop flag. The loop then:

1. stops scheduling new runs;
2. waits up to `MIAS_SCHEDULER_SHUTDOWN_GRACE_SECONDS` for active runs to finish;
3. SIGTERMs the remaining process groups (recorded as `cancelled`);
4. SIGKILLs anything still alive after the kill grace;
5. logs `scheduler_stopped` and exits 0.

Tested with real signals: no orphans, including grandchildren, after graceful or
forced shutdown, and no orphaned children after a SIGKILLed scheduler.

A SIGTERMed collector exits immediately, so its best-effort shadow-persistence
queue is not drained. That is acceptable for shadow persistence and counted as
loss.

## 10. Dry-run

With `MIAS_SCHEDULER_DRY_RUN=true`, `run` and `run-once` compute due jobs and
record and log them as `dry_run` (`job_dry_run`) without starting any process.
This is useful for cadence validation.

## 11. Run-once

`python -m orchestrator.cli run-once` starts every enabled collector once,
concurrently and without offsets, with the same isolation and timeouts. It waits
for all of them, prints their results as JSON and exits:

- **0:** every enabled collector succeeded, or dry-run;
- **1:** any run failed, timed out or was cancelled;
- **2:** invalid configuration;
- **3:** the scheduler is disabled.

Note: with sources unreachable, macro and Treasury exit 1 by their existing
contract, so `run-once` then returns 1.

## 12. Health snapshot

`Scheduler.health()` returns plain data (internal only; no HTTP API in Phase 3):

```json
{"running": true, "stopping": false, "dry_run": false, "started_at": "...", "active_runs": 1,
 "collectors": {"news": {"enabled": true, "interval_seconds": 300, "timeout_seconds": 240,
   "currently_running": false, "last_status": "success", "last_run_at": "...", "next_run_at": "...",
   "recent_status_counts": {"success": 12}, "history_size": 12}}}
```

History is in memory only: the last `MIAS_SCHEDULER_HISTORY_SIZE` results per
collector, with no database table.

## 13. Logging

Scheduler logs are structured `key=value` lines on the `orchestrator` logger.

- **Events:**
  - `scheduler_started`, `scheduler_stopped`;
  - `job_scheduled`, `job_started`, `job_succeeded`, `job_failed`, `job_timeout`;
  - `job_skipped_overlap`, `job_dry_run`, `job_cancelled`, `job_disabled`,
    `job_missed_slots`.
- **Fields** (allowlisted): collector, run_id, status, scheduled/started/finished
  timestamps, duration, exit_code, error_summary and pid.

## 14. Security

- **What is never logged or stored:** environment contents, command lines,
  secrets, or collector stdout/stderr. Only exit codes and fixed summaries.
- **Collector output:** it goes to the scheduler's stdout/stderr, as when run by
  hand (`inherit`), or is discarded (`discard`).
- **Children:** they receive only the allowlisted environment, and secrets are
  never passed as arguments.
- **`status-config`:** prints validated scheduler settings and registry module
  names only (no interpreter paths or environment values).

## 15. Known limitations

- History and health are in memory; they are lost on scheduler restart, and
  there is no HTTP endpoint.
- The parent-death signal covers direct children; a grandchild of a child of a
  SIGKILLed scheduler would survive. Collectors do not spawn subprocesses today.
- Cadence is local to one scheduler process; running two schedulers would
  duplicate collector runs. Deployment must run exactly one.
- No jitter; the deterministic offsets only spread the first runs.
- Collector exit codes are used as-is. Macro and Treasury report partial source
  failures as exit 1; Fed, SEC, News and geopolitical exit 0 after a cycle even
  if some sources failed.
- Production cadence is not tuned (these are staging examples).

## 16. Tests

42 new tests:

| Suite | Tests | Covers |
|---|---|---|
| `test_scheduler_registry` | 6 | Registry contents and wiring |
| `test_scheduler_config` | 5 | Settings, validation, safe printing, CLI config |
| `test_job_runner` | 8 | Real child processes: success, exit codes, timeouts, kill escalation, process-group kill, start failure, environment allowlist |
| `test_scheduler_loop` | 12 | Deterministic fake-clock cadence and dry-run; real overlap, concurrency, the failure-isolation matrix, secret-free logs, `run-once` policy |
| `test_scheduler_shutdown` | 8 | Graceful and forced shutdown, real SIGTERM/SIGINT, kill escalation, no orphans, SIGKILLed scheduler, the real CLI in dry-run |
| `test_scheduler_integration` | 3 | All six real collector entry points through `run-once` and the loop |

The integration tests block the network, Telegram and OpenAI, and never read
`.env`; one variant uses isolated Redis and PostgreSQL. Fixture child
processes (`tests/scheduler_fixture_worker.py`) replace collectors everywhere
else. The one-process-per-module test convention from earlier phases still
applies.

## 17. Future Phase 3B ideas (not implemented)

- A durable run history table, if operational review needs it (needs a
  migration).
- A read-only HTTP or metrics exposure of `health()`.
- A single-instance guard (lock) to prevent two schedulers.
- Per-collector jitter, and a backoff policy after repeated failures.
- A graceful child-stop signal that lets collectors drain their shadow queues.
- Container/deployment manifests.
- Tuned production cadence.
