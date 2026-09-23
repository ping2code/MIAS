# Persistence Phase 2E: Treasury shadow persistence

Phase 2E applies the Phase 2D macro readiness contract to the Treasury collector
only. It does not redesign the persistence model, add a migration, or make
PostgreSQL authoritative. Geopolitical, Fed, SEC and news collectors are not
integrated. Treasury identities, scoring, freshness, Redis keys and semantics,
AI, Telegram and alert decisions are unchanged.

## Switch and runtime

`TREASURY_PERSISTENCE_SHADOW_ENABLED` defaults to `false`; only `true`
(case-insensitive) enables it, and any other value leaves it disabled. It is
independent of `MACRO_PERSISTENCE_SHADOW_ENABLED`. When disabled, the collector
does not import the writer and creates no worker, engine, session or database
activity.

When enabled, `collector/treasury_collector.py` calls `_shadow(...)` at the same
three points as the macro collector:

- a newly processed event, after Redis caching and optional delivery;
- a cached duplicate, submitting the cached result (no rescoring and no AI call);
- a freshness skip (stale, future, missing date), unscored with `make_current=False`.

Lease-contention duplicates carry no result and are not submitted. Import,
initialization or enqueue failures are caught and logged at most once per minute
without detail. Submission is a non-blocking enqueue that never waits for the
database.

`persistence/treasury_shadow.py` provides `persist_treasury`, `submit_treasury`,
`get_persistence_stats` and `shutdown`. `TreasuryShadowWriter` subclasses the
existing `ShadowWriter` and overrides only the persist function, logger name
(`treasury_shadow`), message labels and thread name. Queueing, counters, logging
bounds, engine disposal after failure and shutdown behave exactly as in the
macro writer. Its engine is created lazily on the worker thread with the same
caps (`pool_size=1`, 2 s connect, 1 s statement, 500 ms lock timeout) and
`application_name=mias_treasury_shadow`. The Treasury module singleton, fork
reset, atexit drain and submission counters mirror the macro module and are
separate from it.

Changes to shared code, none of which change behavior:

- `ShadowWriter` gained class-level hooks (`_persist`, `logger`, `messages`,
  `thread_name`). The macro defaults are unchanged.
- `runtime_engine()` takes an `application_name` argument. Its non-PostgreSQL
  error message is now source-neutral.
- `source_order_promotion(...)` was extracted from `macro_promotion`, which now
  delegates to it with its original identity and material rules.
- `validated_ai(...)` was extracted from `adapt_macro`.
- The reconciliation comparator was factored into `_reconcile(...)`, shared by
  `reconcile_macro_event` and the new `reconcile_treasury_event`.

## Canonical adapter

`persistence/adapters/treasury.py::adapt_treasury` accepts only
`treasury_release` and `treasury_yield_observation` events with
`agency=treasury` and a collector `event_id`. It copies parser facts verbatim
and does not infer, fill in or recompute anything.

- Anchor: `source_family=treasury`, `identity_version=treasury-v1`, and
  `event_key` set to the collector `event_id`.
- Version: `treasury-content-v1:` followed by the SHA-256 of the canonical
  normalized content.
- Columns: headline, summary, source, publisher, canonical URL, event type,
  market scope, publication basis, stage (`release_stage`) and revision key
  (`revision_id`).
- Precision: `second` and `minute` keep `published_at`. For `date` precision
  (auctions, debt-limit letters), the Eastern-midnight instant becomes
  `publication_date`. Yield observations stay `unknown` with no publication
  timestamp. The observation date is kept as a fact and is never treated as a
  publication time.
- Common facts: agency, `treasury_category`, `release_id`, `release_stage`,
  `revision_id`, `reference_period`, `original_published_at`, `effective_at`
  (when present), `metrics`, symbol lists and `relevant`.
- Auction facts: CUSIP, auction date, security type (including TIPS/FRN subtype),
  security term, reopening flag and source document. Offering and result amounts,
  bid-to-cover and rate/yield stay in `metrics`. The announcement date is the
  announcement stage's publication date. The collector does not keep it on a
  result event, so the adapter does not invent it.
- Yield facts: `observation_date`, yields, prior observation date and yields,
  benchmark movement in bps, and the 2s10s spread. `dataset` is the
  collector-owned `release_id` component (`yield:<dataset>:<date>`).
- AI fields never enter the version facts, so AI output cannot change numeric
  facts, identities, dates, auction/yield facts, score or stage.

The configured yield movement threshold is not part of the Treasury event. The
movement result is kept as the collector's computed `impact_score` (70 when the
threshold is met, 30 otherwise), together with `movement_bps`.

## Identity

The collector identity stays authoritative:
`sha256([1, agency, release_id, release_stage, revision_id])`.

- Auctions: `release_id = auction:<CUSIP>:<auction date>:<security type>`, with
  stage `announcement` or `result`. The two stages are separate events.
- Reopening: a later auction date of the same CUSIP is a separate event with the
  same CUSIP and `reopening=true`.
- Yields: `release_id = yield:<dataset>:<observation date>`.
- Press releases: `press:<slug>`.
- Letters: `letter:<path>:<date>`.
- An explicit correction changes `revision_id`, which gives it a distinct
  collector identity and therefore a separate event. This matches Phase 2D, and
  this phase does not merge them.

## Promotion

`treasury_promotion` uses the shared Phase 2D `source_order_promotion`.

- Identity facts compared: agency, category, release/stage/revision, reference
  period, CUSIP, auction date, security type, observation date and dataset. Any
  difference is `ambiguous`.
- Material content: whitespace-normalized summary, parser facts (excluding
  `original_published_at` and `source_document`), event type, market scope,
  stage and revision.

| Case | Outcome |
|---|---|
| First version | current |
| Exact duplicate | reused, current unchanged |
| Headline/URL/whitespace change | `cosmetic`, retained, current unchanged |
| Material change with strictly newer comparable source time | `newer_material`, promoted regardless of arrival |
| Material change with older source time | `older`, retained, current unchanged |
| Stale, repost or missing date (caller `make_current=False`) | `caller_disabled` or `duplicate` |
| Yield observation revision (precision `unknown`) | `ambiguous`, never promoted |
| Auction record update on the same stage and date | `ambiguous`, never promoted |

Arrival, observation, fetch and recording time, numeric direction, content hash
and score are never used as ordering evidence. Treasury semantics fit the
existing contract without redesign. The only consequence is conservative: the
first-seen yield or auction version stays current until an explicit new identity
appears.

## Provenance, outcomes and AI

Each version gets one provenance row per official document URL. The role is
`release`, `debt_limit_letter`, `auction_record` (the TreasuryDirect securities
query, with the result/announcement PDF filename as `document_id`) or
`yield_dataset`.

Provenance attributes carry:

- publication basis and precision;
- category, release, stage and revision;
- CUSIP, auction date, observation date and dataset;
- `retrieval_basis` (`fetched_at` or `shadow_observation`).

`source_hash` and `fetched_at` are used only when the event supplies them.
Provenance is idempotent. New evidence on the same version appends a row.

The score history holds `impact_score`, `original_impact_score`, `impact_level`,
`quality_adjustment` (always 0 for official Treasury data) and `score_reasons`.
The decision history holds `alert_decision`, `initial_decision`,
`alert_eligible` and a score snapshot. Both are append-only and idempotent.

AI history is written only when the event already carries validated enrichment
(sentiment enum, integer confidence 0–100, non-empty strings). No OpenAI call is
made for persistence.

## Operations

The writer statistics are the Phase 2C/2D set:

- `queued`, `persisted`, `duplicate`, `failed`;
- `dropped_queue_full`, `dropped_shutdown`, `rejected_shutdown`, `dropped_invalid`;
- `worker_started`, `worker_stopped`;
- `drain_timeouts`, `cleanup_failed`;
- `promotion_held`, `promotion_ambiguous`;
- `queue_depth`, `in_flight`;
- `last_success_at`, `last_failure_at`.

The module adds `dropped_initializing`, `failed_initializing` and
`rejected_shutdown`.

`shutdown(drain=True|False, timeout<=30)` has the same bounded contract. A
timeout discards and counts pending work and reports `in_flight`. `drain=False`
waits at most 100 ms. Shutdown is idempotent and rejects later submissions.
Deduplication after a restart comes only from database constraints.

During a database outage, failures are counted and logged without credentials
or payloads. The worker disposes its engine and reconnects lazily for the next
task. Failed jobs are not replayed, and there is no outbox. Treasury output,
Redis calls, AI calls and Telegram calls stay identical to the disabled mode.
The acceptable-loss budget, healthy criteria and runbook steps from
[Phase 2D](persistence-phase2d.md) and its
[runbook](persistence-phase2d-runbook.md) apply, with the Treasury switch and
the `treasury_shadow` logger substituted.

`reconcile_treasury_event(event, repository, expect_current=True)` is read-only
and returns the same structure as the macro version. It checks:

- the event and its version exist;
- the version content matches;
- the current pointer (can be skipped for historical observations);
- provenance;
- score, decision and AI history (`None` when not expected).

## Fixed replay corpus

`tests/treasury_readiness_corpus.py` builds 27 deterministic observations from
checked-in fixtures and synthetic variants, with no network. The build is pinned
by `tests/fixtures/persistence/treasury_readiness_v1.{json,sha256}`. Scores and
decisions come from the collector's own `_analyze` step at a fixed clock. AI
text is synthetic.

The corpus covers:

- quarterly refunding, borrowing estimates, a debt-limit letter and an
  issuance-policy release;
- a bill auction announcement and result, a note result, and a reopening at a
  later auction date;
- routine and threshold yield observations;
- a duplicate, a material revision, an older late arrival and a cosmetic change;
- a stale repost and a missing publication time;
- AI present and absent;
- provenance duplication and new source evidence;
- an explicit correction;
- a yield revision and an auction result update without source ordering.

Replay results (SQLite and PostgreSQL 16.15):

```
events 12, event_versions 19, event_provenance 20, event_history 36
duplicates 6, matched 27/27, mismatches []
promotions: first 12, duplicate 8, newer_material 1, older 1, cosmetic 1,
            caller_disabled 2, ambiguous 2
```

A second full replay produces 27 duplicates and no new rows. Reconciliation runs
in one PostgreSQL `REPEATABLE READ, READ ONLY` transaction.

## Tests and PostgreSQL validation

The new test modules are:

- `tests/test_treasury_persistence_adapter.py`
- `tests/test_treasury_shadow_persistence.py`
- `tests/test_treasury_persistence_readiness.py`
- `tests/test_treasury_persistence_postgres.py`

The live module reruns the adapter and readiness classes on PostgreSQL. It adds:

- actual worker commits of collector results;
- rollback on a repository failure;
- the database unavailable at start;
- restart with a duplicate and a new material version;
- out-of-order observations through the worker;
- recreation after a worker exception;
- spawned-process races for an identical snapshot, a material revision against
  an old observation, conflicting material versions, provenance contention and
  distinct outcome histories;
- a real container stop, mid-run outage and recovery.

Use the Phase 2C disposable container recipe (same image digest, label, private
volume and loopback port) and the same explicit
`MIAS_PHASE2C_TEST_CONTAINER` opt-in. That opt-in lets the macro and Treasury
outage tests both run against the verified container. Remove the container and
its volume afterwards.

```sh
python -m unittest tests.test_treasury_persistence_adapter tests.test_treasury_shadow_persistence \
  tests.test_treasury_persistence_readiness tests.test_treasury_persistence_postgres -v
```

## Remaining limits

- A Treasury yield or auction data revision without a new collector identity is
  kept but never promoted, because the sources give no publication ordering.
  The first-seen version stays current. This is intentional and should be
  reviewed if an ordered revision signal becomes available.
- The configured yield threshold value is not recorded, only its result.
- An auction result event does not carry its announcement date.
- There is no durable outbox. Outage-window snapshots are lost, but counted.
