# Phase 4C: Live Provider Validation and Technical Snapshot Persistence

Phase 4C makes the Phase 4B technical engine operationally ready. It adds:

- bounded live-provider validation tooling;
- a dedicated durable schema for technical snapshots;
- shadow persistence;
- reconciliation and audit tooling;
- a scheduler registration for the technical runner, **disabled by default**.

It remains decision support only:

- no trades, broker order APIs (Application Programming Interfaces) or options;
- no Telegram, and no fusion with the News, SEC (U.S. Securities and Exchange
  Commission), Fed (Federal Reserve), Macro, Treasury or Geopolitical collectors;
- no threshold tuning.

Technical output is unchanged. Every Phase 4 and 4B snapshot is bit-identical,
and replay and incremental output are still exactly equal.

New and changed code:

| Path | Purpose |
|---|---|
| `market_data/live_check.py` | bounded read-only live check CLI (Command-Line Interface) |
| `market_data/http.py`, `market_data/providers/polygon.py` | request cap, safe diagnostics (statuses, rate-limit headers, pages, volume JSON types, `adjusted` echo) |
| `migrations/versions/0004_technical_snapshots.py` | migration **`0004_technical_snapshots`** |
| `persistence/models.py` | `technical_snapshots`, `technical_snapshot_conflicts` |
| `persistence/technical_snapshot_repository.py` | row mapping, content hash, immutable writes |
| `persistence/technical_settings.py` | persistence settings |
| `persistence/technical_shadow.py` | bounded shadow writer |
| `persistence/technical_snapshot_tools.py` | `status` / `reconcile` / `audit` |
| `technical/runner.py` | session-anchored warm-up windows, optional shadow persistence |
| `technical/levels.py` | faster clustering, bit-identical output |
| `orchestrator/config.py`, `orchestrator/registry.py`, `orchestrator/job_runner.py` | `technical` job (disabled by default), child environment allowlist |
| `migrations/versions/0005_technical_fractional_volume.py` | follow-up migration **`0005_technical_fractional_vol`**: snapshot volume becomes double precision |
| `market_data/models.py`, `technical/indicators.py`, `technical/incremental.py`, `technical/engine.py` | follow-up: exact `Decimal` bar volume (fractional allowed), float in indicator math |

## 1. Provider validation result

**First live check: EXECUTED on 2026-09-24 (20:21 America/New_York) — FAILED.**
The operator ran `python -m market_data.live_check --symbols META,NVDA --intervals
5m,1h,1d --days 5 --states` with a free-tier development key, in their own
terminal. The key never reached MIAS logs or this repository. There were two
findings.

1. **Fractional volume (contract mismatch).** The first result of every response
   that got through (META 5m, the META 30m source for 1h/1d, and NVDA 5m) failed
   with `volume must be a whole number`. The vendor's aggregates include
   fractional-share trades, so fractional volume is valid data. The adapter's
   whole-number assumption was wrong. It correctly refused to round, but it
   stopped at the first result, so nothing else could be observed.
2. **Free-tier rate limit.** The first 5 requests succeeded, and every later
   request got HTTP 429 **without** a `Retry-After` header. The 1/2/4 s backoff
   exhausted 4 attempts each time, and the run used 17 requests in total. This
   confirms the quota of about 5 requests/minute. Retries alone cannot absorb a
   burst.

**Fixes (follow-up to Phase 4C), each tested explicitly:**

- **Volume:** now an exact non-negative `Decimal` end to end. `MarketBar`
  accepts int, `Decimal` or a numeric string (never float) and rejects negative
  or non-finite values. The adapter accepts fractional JSON numbers exactly and
  never rounds. Indicators use it as float, like prices. Whole-number volumes
  give bit-identical results, since float sums of integers below 2^53 are exact,
  and every Phase 4/4B test passes unchanged. Replay and incremental output stay
  exactly equal on a fractional-volume series. Snapshot `volume` is double
  precision (migration `0005_technical_fractional_vol`).
- **Pacing:** `MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS` (default **12**, the free
  tier's 5/minute; 0-120) spaces every request, retries included.
  `MARKET_DATA_RATE_LIMIT_FALLBACK_WAIT_SECONDS` (default 15, 0-600) is the
  minimum wait after a 429 without `Retry-After`.
- **Diagnostics:** the live check reports `fractional_volumes` and
  `max_fractional_volume_part`, and replaces the whole-number check with
  `volume_valid` (non-negative and finite).

**Still unverified until the check is rerun:**

- field names beyond volume;
- the `adjusted` echo, pagination (`pages`, and therefore the request budget,
  section 4) and the delayed status;
- the session grid and completed-bar behaviour on real data.

No other provider behaviour is claimed as verified.
`docs/phase4c-real-history-report.md` records the same status.

## 2. Live-check procedure

```
python -m market_data.live_check --symbols META,NVDA --intervals 5m,1h,1d --days 5 [--max-requests 40] [--states]
```

**Guarantees:**

- read-only (GET requests to the provider only);
- no Telegram, OpenAI, database or Redis;
- at most 4 symbols and 1-10 trading days;
- a hard request cap enforced inside the HTTP client, retries included
  (`error_kind=budget` once exceeded).

**Output:** one JSON summary per (symbol, interval) with metadata only:

- provider, requested and source interval, date range;
- bar and completed-bar counts, source bars fetched;
- first and last timestamp;
- validation result and error kind;
- volume JSON types, pages and pagination observed;
- statuses (`OK`/`DELAYED`), `adjusted` echo;
- 429 count and rate-limit headers (`Retry-After`, `X-RateLimit-*` only);
- latest bar age, request count.

`--states` adds the final technical state per timeframe. The output never
contains raw payloads or the key.

**Contract checks** (`checks`) map onto the Phase 4C list:

| Contract item | How it is checked |
|---|---|
| field names | every result had `t/o/h/l/c/v` of the expected JSON types (validation fails otherwise) |
| timestamp units | `t` read as milliseconds must land inside the requested window. Seconds would land in 1970 and fail |
| timezone normalization | bars are converted to America/New_York and must sit on the session grid |
| volume | non-negative and finite; fractional values are valid and kept exactly (`fractional_volumes`, `max_fractional_volume_part`); `volume_json_types` shows JSON integers vs numbers |
| pagination | pages are counted, and `next_url` is followed only to the configured host |
| rate limits | 429 responses and rate-limit headers are recorded; requests are paced (`min_request_interval_seconds`); retries are bounded |
| delayed data | `statuses` shows `DELAYED`; `latest_bar_age_seconds` is compared with `MARKET_DATA_DELAY_SECONDS` |
| adjusted | the vendor's `adjusted` echo must equal the requested flag |
| session grid | every bar maps to one session and sits on its grid |
| completed-bar filtering | every returned bar ends at or before the data cut-off |

Exit codes: 0 all passed, 1 any failure, 2 not executed (configuration/usage).

## 3. Credentials

Use only the Phase 4B variables in the **process environment**:

- `MARKET_DATA_PROVIDER=polygon` (alias `massive`);
- `MARKET_DATA_API_KEY`;
- optionally `MARKET_DATA_BASE_URL` and the timeout/retry/delay settings.

The key is sent only as an `Authorization: Bearer` header and never appears in
logs, errors, summaries or snapshots. The tests assert this with a canary key.

The scheduler's child-environment allowlist now passes `MARKET_DATA_*` and
`TECHNICAL_*` to the job. Unrelated secrets (for example cloud credentials or
agent sockets) are still dropped, and the scheduler never logs environment
values.

## 4. Request budget

Per run, the runner fetches each **source** interval once per symbol:

- **5m** covers 10 sessions;
- **30m** covers 450 sessions, and both 1h and 1d are derived from it.

The vendor's `limit=50000` applies to **base aggregates**, and the documentation
does not make clear whether that means returned bars or underlying minute bars.
Both readings are shown; the live check's `pages` field settles it.

| Reading | 5m request (10 sessions) | 30m request (450 sessions) | Requests per symbol | Requests per run (META+NVDA) |
|---|---|---|---|---|
| A: limit counts returned bars | 1,920 bars → 1 page | 14,400 bars → 1 page | 2 | **4** |
| B: limit counts minute bars (960/day incl. extended) | 9,600 → 1 page | 432,000 → 9 pages | 10 | **20** |

The free tier allows about 5 requests/minute, **confirmed by the first live
check**: the 6th request in a minute got 429 with no `Retry-After`. With the
default 12 s pacing, a run's requests are spread out rather than retried:
reading A takes about 36 s of pacing per run, and reading B about 4 minutes.
Both are inside the 900 s job timeout.

| Cadence | Runs/hour | A: requests/hour | B: requests/hour | Free tier (5/min) |
|---|---|---|---|---|
| 5 min | 12 | 48 | 240 | A: fits. B: each run needs ≥4 minutes of 429 waits; **not compatible** |
| 15 min | 4 | 16 | 80 | A: fits. B: run duration ≥4 min with default retries likely to fail; **not recommended** |
| 30 min (default) | 2 | 8 | 40 | A: fits. B: only with paced retries (see below) |
| 60 min | 1 | 4 | 20 | A: fits. B: only with paced retries |

Before pacing existed, the default retry policy (3 retries, 1/2/4 s backoff)
failed on the free tier. That was observed, not predicted. Pacing at 12 s keeps
each run under the quota, and a 429 without `Retry-After` now waits at least
15 s. Under reading B, the cadence must still leave room for about 4 minutes of
paced requests per run. A paid tier (`MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS=0`)
or the Phase 5 daily-bar cache (section 17) removes that constraint.

Real-time and intraday freshness also depend on the plan. The free tier may
provide end-of-day data only.

## 5. Scheduler decision

The technical runner is **registered, disabled by default**:

- `TECHNICAL_SCHEDULE_ENABLED=false`;
- `TECHNICAL_INTERVAL_SECONDS=1800`, `TECHNICAL_TIMEOUT_SECONDS=900`,
  `TECHNICAL_START_OFFSET_SECONDS=30`, all validated like the other families;
- overlap policy SKIP;
- `TECHNICAL_SCHEDULE_SEND_ALERTS` is rejected, since there is no alert path.

`status-config` shows `send_alerts: "never (no alert path)"`. The other six
collectors are unchanged.

It stays disabled because the live contract and the request budget (section 4)
are unverified. Enable it only after a successful live check shows `pages` (and
therefore the budget) and plan freshness.

## 6. Snapshot schema

`technical_snapshots` holds one row per completed bar, symbol, interval and engine
version.

- **Scalars** (queryable):
  - identity: symbol, interval, `snapshot_timestamp` (bar start, UTC
    (Coordinated Universal Time));
  - `engine_version`, `source_provider`, `provider_delay_seconds`,
    `is_completed_bar` (always true, check-constrained), `session_type`;
  - price, `ema9/ema20/ema50/ema200`, `vwap`, `rsi14`, `atr14`, volume,
    `average_volume`, `relative_volume`;
  - trend, `last_high_type`, `last_low_type`, `gap_type`, `gap_percent`,
    `gap_absolute`, `breakout_state`, `breakout_level`, momentum,
    `ema_alignment`, `vwap_position`;
  - `technical_state`, confidence, agreeing, conflicting;
  - `warmup_start`, `warmup_bars`, `content_hash`, `created_at`.
- **JSON** (JavaScript Object Notation) columns, stored as JSONB:
  `significant_high`, `significant_low`, `support_levels`, `resistance_levels`,
  `reasons`, `ema_state`, `vwap_state`, `evidence`. There is no whole-snapshot
  blob, and levels are not over-normalized into separate tables.
- **Constraints:**
  - a unique identity (section 7);
  - enum checks for interval, state, confidence, trend, breakout state, gap type
    and session type;
  - a completed-bar check;
  - value bounds (volume ≥ 0, price > 0, warm-up > 0).
- **Indexes:**
  - the identity index serves symbol/interval/time-range queries;
  - `ix_technical_snapshots_state (symbol, interval, technical_state)`;
  - `ix_technical_snapshots_created_at`.
- `technical_snapshot_conflicts` records rejected rewrites: the identity,
  existing and rejected hashes, the rejected material fields (no raw provider
  data) and `detected_at`. It is unique per (snapshot, rejected hash), and its
  foreign key to the snapshot uses `ON DELETE RESTRICT`.

Raw provider payloads are never stored. Only snapshots from the durable
configuration are accepted (EMA periods 9/20/50/200, RSI (Relative Strength
Index) period 14). Anything else is rejected as `invalid_row`.

## 7. Identity and idempotency

**Identity** is `(symbol, interval, snapshot_timestamp, engine_version)`.

**Content:** `content_hash` is the SHA-256 of the canonical JSON of every material
field: all technical values plus the warm-up context. It excludes the row id,
`created_at` and the run-time `provider_delay_seconds`.

| Write | Result |
|---|---|
| same identity, same content | `duplicate`: one durable row, nothing changes |
| same identity, different content | `conflict`: the existing row is kept untouched; the rejection is recorded once per distinct content hash |
| different timestamp, interval, symbol or engine version | a separate row |

Concurrent writers are safe. The tests hold one real PostgreSQL transaction open
while a second one blocks on the unique index. Identical writes give
`inserted` + `duplicate`; conflicting writes give `inserted` + `conflict`.

**Session-anchored warm-up makes idempotency real.** EMA (Exponential Moving
Average) seeds depend on where history starts. A wall-clock look-back would give
the same bar different values after midnight, and so a spurious conflict. The
runner now anchors each timeframe's window to trading sessions:

- the window starts at midnight exchange time, `WARMUP_SESSIONS - 1` trading
  days before the latest trading day whose first bar of that timeframe has
  completed;
- every run that evaluates the same latest bar therefore uses the same bars. The
  tests cover 17:00, 23:59, 00:30 the next day and 09:30.

| Target | Sessions | Rationale |
|---|---|---|
| 5m | 10 (~780 bars) | ~3.9x EMA200; far beyond RSI/ATR(14), volume(20) and pivot/level history |
| 1h | 90 (~630 bars) | ~3x EMA200 |
| 1d | 450 (~450 bars) | 2.25x EMA200 within a two-year history limit. The EMA200 seed still has about 8% weight (`(199/201)^250`). Deterministic, but not "infinite-history" converged |

## 8. engine_version

- `TECHNICAL_SNAPSHOT_ENGINE_VERSION` defaults to `phase4c-v1` and must be 1-32
  characters of `[a-z0-9.-]`.
- A rule change means a new engine version. Its rows sit beside the old ones and
  never rewrite them.
- Recomputing history is explicit tooling, not normal write behaviour. The audit
  flags any engine version not on the expected list.

## 9. Shadow persistence

`TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED=false` by default.

- **Off:** the runner does exactly what Phase 4B did, plus the new warm-up
  windows.
- **On:** after each symbol's snapshots are computed, logged and printed, one row
  per timeframe is submitted. Submission is non-blocking. After all symbols, the
  writer drains within `TECHNICAL_SNAPSHOT_DRAIN_TIMEOUT_SECONDS` (default 10,
  0-30) and one `event=technical_persistence` accounting line is logged.

A database failure never changes technical output or the exit code, and no
setting makes persistence critical. The tests prove parity with persistence off,
on, and on with the database unreachable, both in-process and in a real
subprocess.

## 10. Queue behaviour

`TechnicalSnapshotWriter` subclasses the existing generic bounded worker. Only the
persist callback and labels differ; the macro writer is not modified. It has:

- a bounded queue (`TECHNICAL_SNAPSHOT_QUEUE_SIZE`, default 256, 16-4096);
- non-blocking submit, with a full queue dropped and counted
  (`dropped_queue_full`);
- one worker with a lazy engine, capped at pool 1, 2 s connect, 1 s statement
  timeout;
- no retry and no hidden replay;
- bounded drain, with pending work on timeout discarded and counted.

Accounting:

| Counter | Meaning |
|---|---|
| `persisted` | inserted or duplicate |
| `duplicate` | idempotent duplicate |
| `conflict` | identity conflict (also counted in `failed`) |
| `failed` | database or task failure, including conflicts |
| `invalid_row` | row could not be built or failed validation |
| `dropped_*`, `rejected_shutdown`, `failed_initializing` | never attempted |

Six snapshots per run (2 symbols × 3 timeframes) are far below the default
capacity.

## 11. Reconciliation

```
python -m persistence.technical_snapshot_tools status [--symbol META] [--interval 5m]
python -m persistence.technical_snapshot_tools reconcile --symbol META --interval 5m --start 2026-09-21 --end 2026-09-24
```

- `status`: rows, first and last timestamps per symbol/interval/engine version,
  and the conflict count.
- `reconcile`: compares stored rows with the calendar's expected completed-bar
  grid (regular session; trading days for 1d). It reports missing, unexpected
  (off-grid), duplicate and conflicting identities. *Missing* is informational:
  the runner stores the latest bar per run, so coverage follows the scheduler
  cadence.
- In-run reconciliation is the runner's persistence accounting line: queued =
  persisted + failed + drops. The real-process test verifies it against the
  database row count.

Only `SELECT` statements run, and there is no destructive or repair mode.

## 12. Audit

`python -m persistence.technical_snapshot_tools audit [--engine-version …] [--provider …]`
is read-only. It exits 0 when clean and 1 when it finds problems. It reports:

- duplicate identity groups (defensive; the unique index prevents them);
- recorded conflicts;
- missing required metrics (EMA20, RSI or ATR (Average True Range) absent outside
  `insufficient_data`);
- invalid enum values, including momentum, alignment, VWAP (Volume Weighted
  Average Price) position and swing labels;
- timestamps off the calendar grid, and rows persisted before their bar completed;
- unexpected providers or engine versions;
- **content-hash mismatches**: the hash is recomputed from the stored row. The
  PostgreSQL tests confirm zero mismatches after the JSONB round trip.

## 13. Failure handling

| Failure | Behaviour |
|---|---|
| provider auth (401/403), permanent 4xx, malformed JSON, redirect | not retried; symbol fails; runner exits 1; live check reports `error_kind` |
| rate limit (429), 5xx, timeout, connection error | requests paced; bounded retries with backoff (`Retry-After` honoured and capped; at least the fallback wait for a 429 without it), then fail as above |
| empty response | zero bars; timeframes reported `unavailable`; no crash |
| duplicate or out-of-order bars, negative/non-finite/non-numeric volume, bad timestamps, off-grid or holiday bars, missing fields | explicit `MarketDataError`; nothing is repaired (fractional volume is **valid** and kept exactly) |
| pagination to another host, or too many pages | refused (`payload`) |
| request budget exceeded (live check) | `error_kind=budget` |
| configuration error (provider or persistence) | exit 2 |
| database down, slow or conflicting | counted in persistence accounting; output and exit code unchanged |

## 14. Migration

**ID: `0004_technical_snapshots`** (down revision `0003_geo_anchor_registry`).

- Purely additive: two new tables and their indexes. No prior migration or table
  is modified.
- Tested on disposable PostgreSQL 16:
  - upgrade, downgrade to `0003`, re-upgrade (twice), then full downgrade to base
    and re-upgrade;
  - `compare_metadata` is empty;
  - the existing foundation round-trip test also passes with the new tables.
- Also tested: insert, duplicate insert, conflicting insert, each check
  constraint, the indexes, range queries, and the concurrency races.
- **Head bump.** `persistence/geopolitical_tools.py` pins the migration head it
  expects (`EXPECTED_REVISION`). A test enforces that it equals the Alembic head,
  and its `status`/registry commands refuse any other revision. It is now
  `0004_technical_snapshots`. Without the bump, the geopolitical tools would
  report a schema error after this migration. Three tests and the Phase 2S
  staging harness (`HEAD`) pin the head string for the same reason and were
  updated. No geopolitical behaviour changed.

**Follow-up ID: `0005_technical_fractional_vol`** (down revision `0004_technical_snapshots`).

- Changes `technical_snapshots.volume` from BIGINT to DOUBLE PRECISION
  (`USING volume::double precision`). Whole numbers convert exactly.
- Downgrade converts back with `round(volume)::bigint`, which is **lossy** for
  fractional values; export first. The audit then reports those rows as
  content-hash mismatches.
- On SQLite (tests/dev only), which cannot `ALTER` a column type, the same change
  runs in Alembic batch mode (table recreate). All check constraints, indexes, the
  unique identity and the conflict foreign key are preserved.
- Tested on disposable PostgreSQL 16: fractional insert, an exact duplicate round
  trip, downgrade to `0004` (rounded) and re-upgrade, plus the existing
  up/down/re-up and `compare_metadata` checks.
- The pinned head (`EXPECTED_REVISION`, three tests, the staging harness `HEAD`) is
  now `0005_technical_fractional_vol`.

## 15. Real-history evaluation

**NOT YET PRODUCED.** The first live check failed before any bars could be
evaluated (section 1); see `docs/phase4c-real-history-report.md`, which contains
no substitute numbers. The tooling is ready:

- `market_data.live_check --states`;
- `evaluation.technical_replay`, which reports state counts, durations,
  transitions, 1/3/5/10-bar forward returns and MFE/MAE (Maximum Favorable /
  Adverse Excursion).

No thresholds were changed.

## 16. Support/resistance performance

`cluster_levels` now keeps a plain list of prices per cluster and averages with
`sum(list)`. That is the same compensated float summation over the same values in
the same order as the Phase 4 generator form, **so output is bit-identical**. The
proof:

- `tests/test_levels_equivalence.py` compares against a verbatim copy of the
  Phase 4 implementation over 2,000 randomized cases plus adversarial float
  inputs (exact `float.hex` equality);
- an extra 3,000-case check matched on 15,274 levels;
- every Phase 4 and 4B replay, incremental and fixture test still passes
  unchanged.

Benchmark (`python -m evaluation.performance --sessions 120`, 9,360 synthetic 5m
bars):

| | Replay | Incremental | Incremental per bar |
|---|---|---|---|
| Phase 4B | 21.64 s | 18.20 s | 1.94 ms |
| Phase 4C | 18.83 s | 13.81 s | 1.48 ms |

## 17. Known limitations

- **Live contract partly verified.** Fractional volume and the 5/minute quota with
  no `Retry-After` are confirmed and handled. Field names, the `adjusted` echo,
  pagination semantics (section 4) and plan freshness await the rerun.
- **Request budget reading B** would make free-tier cadences below 30 minutes
  impractical.
- **Stateless runner:** each run re-warms from history. A per-day cache of the
  30m history (so 1h/1d need one small incremental request) is the natural
  Phase 5 improvement.
- **Daily EMA200 convergence** is bounded by about 450 sessions of history (seed
  weight about 8%).
- **Vendor revisions** (for example split adjustments) change recomputed history.
  The next write of an affected identity is recorded as a conflict, never
  applied. Review them through `audit`.
- **Reconciliation coverage** reflects scheduler cadence, not engine
  correctness.

## 18. Rollback

1. **Stop persisting:** set `TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED=false`.
   Technical output is unaffected.
2. **Stop scheduling:** `TECHNICAL_SCHEDULE_ENABLED=false` (already the default).
3. **Schema:** `alembic downgrade 0004_technical_snapshots` reverts only the
   volume type change (lossy for fractional values). `alembic downgrade
   0003_geo_anchor_registry` drops
   `technical_snapshot_conflicts` and `technical_snapshots`, **deleting their
   data**. Export first if the rows matter. Prior tables are untouched.
4. **Code:** revert the Phase 4C commit. The technical engine outputs are
   identical to Phase 4B apart from the warm-up window anchoring.
