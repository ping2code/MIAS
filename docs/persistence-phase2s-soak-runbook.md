# Persistence Phase 2S: multi-day all-source soak runbook

This runbook describes a **future multi-day shared-staging soak** of all six
persisted source families: macro, Treasury, geopolitical, Fed, SEC and News. It
has **not** been performed. Phase 2S performed only a bounded validation (minutes,
not days); see [the readiness report](persistence-phase2s-readiness-report.md).

The soak uses the same harness as the bounded validation,
`python -m tests.real_staging_all_sources`. Each collector runs in its own OS
process against disposable, labelled PostgreSQL 16 and Redis 7 containers. It is
a staging cadence only; production collector cadence is not changed.

## Prerequisites

- A checkout of `main` at or after the Phase 2S commit, with the project virtualenv.
- Docker on the host.
- Nothing else named or labelled `phase2s`: the runner refuses to reuse unknown
  containers.
- Free loopback ports 55432, 56379 and 56380.
- Outbound HTTPS to the official sources:
  - federalreserve.gov, federalregister.gov, ftc.gov and moea.gov.tw;
  - the BIS, OFAC, Treasury, USTR and White House index pages;
  - bls.gov, bea.gov, census.gov, treasury.gov and treasurydirect.gov;
  - data.sec.gov;
  - Yahoo Finance and Google News RSS.
- **No credentials:**
  - Do not export real Telegram or OpenAI values. Workers receive test-only
    canary values and every artifact is scanned for them.
  - `.env` is never read.
  - Do not point `DATABASE_URL` or Redis settings at anything shared; the runner
    builds its own environment for every worker.

## Safe environment

The runner creates and removes its own containers:

- `mias-test-phase2s-postgres`, on a private anonymous volume and
  `127.0.0.1:55432`;
- `mias-test-phase2s-redis` and `mias-test-phase2s-redis-control`, on tmpfs with
  RDB/AOF off, at `127.0.0.1:56379` and `127.0.0.1:56380`.

All carry `mias.disposable-test=phase2s`. They are removed on success, failure and
Ctrl+C/SIGTERM, and only if this invocation created them. It refuses:

- non-loopback bindings and unlabeled containers;
- database names other than `mias_test_phase2s*`;
- a non-empty Redis;
- a pre-existing staging database.

It never touches `mias-redis` or any other container.

## Start (72-hour example)

```sh
cd /path/to/MIAS
nohup .venv/bin/python -m tests.real_staging_all_sources \
    --duration-minutes 4320 --cycle-seconds 900 --no-faults \
    --report soak-2s-report.md --json-report soak-2s-evidence.json \
    > soak-2s.log 2>&1 &
echo $! > soak-2s.pid
```

- `--duration-minutes` sets the soak length (4320 = 72 h). The runner keeps
  cycling until both `--cycles` (default 3) and the duration are satisfied.
- `--cycle-seconds` is the gap between cycle starts (10..3600). 900 s means one
  fresh process per family every 15 minutes: about 288 cycles and 1,700 process
  lifecycles over 72 h.
- `--no-faults` skips the destructive fault scenarios (DB stop, Redis replacement)
  during a long soak. Run them separately with the bounded command below if needed.
- Every cycle prints one JSON line to the log (submissions and events added per
  family); nothing else is printed per item.

## Status checks

```sh
tail -n 5 soak-2s.log                                  # latest cycle summaries
ps -p "$(cat soak-2s.pid)" -o pid,etime,rss,cmd        # coordinator alive, elapsed time
docker ps --filter label=mias.disposable-test=phase2s  # three containers, loopback ports only
```

## Daily audits (read-only, while the soak runs)

Run these against the soak database from a separate shell. They never write, and
each prints JSON:

```sh
export DATABASE_URL=postgresql://mias_test_user@127.0.0.1:55432/mias_test_phase2s_all
.venv/bin/python -m persistence.family_audit --json            > day-N-family.json
.venv/bin/python -m persistence.geopolitical_tools audit --json > day-N-geo-audit.json
.venv/bin/python -m persistence.geopolitical_tools disclosure-audit --json > day-N-geo-disclosure.json
.venv/bin/python -m persistence.geopolitical_tools conflicts --json > day-N-geo-conflicts.json
.venv/bin/python -m persistence.sec_audit shared-accessions --json > day-N-sec-shared.json
.venv/bin/python -m persistence.sec_audit repeats --json        > day-N-sec-repeats.json
.venv/bin/python -m persistence.news_audit url-variants --json  > day-N-news-variants.json
.venv/bin/python -m persistence.news_audit repeats --json       > day-N-news-repeats.json
unset DATABASE_URL
```

Each day, check:

- `integrity_findings == 0` and `pointer_rule.violation_count == 0`;
- no new geopolitical `exact_authoritative_anchor` groups, and disclosure
  `flagged_count == 0`;
- unexpected SEC shared accessions and new News URL-variant groups are reviewed,
  not corrected.

## Restart procedure

Every cycle already starts a fresh process per family; no collector state
survives a cycle. To restart the whole soak (for example after a host reboot),
stop it (below), let it clean up, and start it again. It begins from an empty
database; a soak is evidence, not durable production history.

## Stop

```sh
kill -TERM "$(cat soak-2s.pid)"   # finishes the current process step, kills only its own workers,
                                  # removes the phase2s containers, then exits
```

After a stop, the report is written only if the run completed acceptance. An
interrupted run records `interrupted: true` in the evidence.

## Cleanup check

```sh
docker ps -a --filter label=mias.disposable-test=phase2s   # expect nothing
docker volume ls -q -f dangling=true                       # expect no new volumes
```

## Acceptance criteria (soak)

- Every cycle completes for every family, or failures are live-source outages
  recorded in the evidence (`fetch_errors`, HTTP statuses).
- Process health:
  - no writer failures outside deliberate fault scenarios;
  - `queue_depth` and `in_flight` are 0 after every drain;
  - reconciliation reports zero unexplained integrity mismatches;
  - `dropped_queue_full == 0` in every healthy cycle for every family. Phase 2S
    originally found Treasury dropping 50 observations per cycle; Phase 2S-A fixed
    this with `TREASURY_PERSISTENCE_QUEUE_SIZE` (default 256; other families 64).
    Any non-zero value means a burst outgrew its configured capacity.
- Durable history:
  - every new durable event is explained by a newly stored identity
    (`unexplained_growth` is empty);
  - diagnostic only: a queue-full drop appears in reconciliation as "not found"
    for the dropped item. That explains the missing row, but any drop still fails
    acceptance (see Process health).
- Daily audits show no integrity findings, pointer-rule violations or new
  divergence groups.
- Resource sanity:
  - per-process RSS stays under 1.5 GB and threads under 128;
  - PostgreSQL connections return to baseline;
  - database and Redis size grow only with explained history.
- Hygiene: the credential scan reports 0 hits, and there are no Telegram sends
  or OpenAI calls (tripwires 0).

## Evidence to capture

- `soak-2s-evidence.json` and `soak-2s-report.md` (counts only; no credentials,
  article text or the SEC contact).
- The daily audit JSON files.
- `soak-2s.log` (per-cycle summaries).
- A short note: start and stop times, interruptions, and any live-source outages
  observed.

## Bounded validation command (what Phase 2S ran)

```sh
.venv/bin/python -m tests.real_staging_all_sources --cycles 3 --cycle-seconds 30 \
    --report docs/persistence-phase2s-readiness-report.md --json-report /tmp/phase2s.json
```

This includes the fault scenarios: cross-family look-alikes, a PostgreSQL outage
with recovery, and Redis replacement.
