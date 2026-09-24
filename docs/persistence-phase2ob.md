# Persistence Phase 2O-B: SEC shadow persistence

Real-process operational validation, read-only shared-accession and repeat-observation audits
(`python -m persistence.sec_audit`): [Phase 2P report](persistence-phase2p-sec-operational-report.md).

Phase 2O-B adds opt-in, shadow-only, non-blocking persistence for SEC filing
events. It reuses the existing persistence architecture unchanged: the
repository, schema (no migration; `sec` was already an allowed
`source_family`), bounded shadow writer, shared lifecycle, counters and
read-only reconciliation. It builds on the importable collector from
[Phase 2O-A](follow-up-issues.md).

Nothing about SEC runtime behavior changes:

- scoring and form weights, including the default score for amended forms;
- the collector fingerprint, the Redis `sec:event` namespace and its TTL, and
  fail-open dedup;
- Telegram delivery, CIK mappings, source URLs and the User-Agent;
- the normalized event contract. SEC has no AI path, and none is added.

## Hook and switch

`SEC_PERSISTENCE_SHADOW_ENABLED` (default `false`, exact value `true` enables)
lives in `shared/config.py`. `.env` was not read or modified.

`process_sec_filings` calls `_shadow(event)` at the end of each processed
filing. By then the event has been normalized, deduplicated, scored, decided and
(for `ALERT`) sent or failed on Telegram. The hook:

- returns immediately when disabled: no import, worker, engine, session or DB
  activity (tested);
- submits a copy with `sec_fingerprint` added, so the event returned to the
  collector is unchanged;
- swallows every failure (import, initialization, enqueue) with a rate-limited
  `SEC shadow submission failed` warning;
- never runs for Redis-skipped duplicates, which are not scored or decided.

The writer (`persistence/sec_shadow.py`) is the shared `ShadowWriter` with SEC
labels: bounded queue (64), lazy engine (`application_name=mias_sec_shadow`,
pool 1, 2 s connect, 1 s statement, 500 ms lock timeout), no retries, and the
standard counters and `shutdown(drain, timeout)`. The script entry point relies
on the existing atexit drain (as Fed does).

## Adapter mapping (`persistence/adapters/sec.py`)

| Stored | From the normalized event |
|---|---|
| `events.event_key` | `sec_fingerprint`: the collector's own `create_fingerprint` (sha256 of `headline|url`) |
| `identity_version` | `sec-v1` |
| headline, summary, canonical URL | verbatim |
| source / publisher | `SEC EDGAR` / `SEC` |
| `event_type` | `sec_filing` |
| `publication_date` | `published_at` (the submissions filing date, `YYYY-MM-DD`) |
| `timestamp_precision` / basis | `date` / `sec_submissions_filing_date`; `unknown` / `unverified` if the date is empty |
| attributes | `sec_form`, `accession_number`, `symbols`, `direct_symbols`, `related_symbols`, `relevant`, `primary_document` |
| provenance `document_id` | accession number |
| provenance attributes | role, publisher, accession, form, raw filing date, primary document, precision/basis, retrieval basis |
| score history | `impact_score`, `impact_level`, `score_reasons` (+ `quality_adjustment` only if present) |
| decision history | `alert_decision` plus a score snapshot |

**Accession and primary document** are taken from the event and its URL only.
The URL must have exactly the archive shape the normalizer builds
(`https://www.sec.gov/Archives/edgar/data/<cik>/<18-digit accession>/<primary document>`),
and its accession segment must equal `accession_number` without dashes, or the
write fails closed (counted as a task failure; the collector is unaffected).
The primary document is everything after the accession segment. It may contain
a path: every live insider filing in the validation run did, e.g.
`xslF345X05/form4.xml`. It is stored as `None` when empty.

**Not invented:** CIK (present only inside the stored URL), report date, item
codes, amendment flag, amendment relation and filing category. Filing dates are
never upgraded to times. Any AI-looking fields on the event are ignored; no AI
record is ever written.

## Runtime identity vs accession

- **Runtime identity** stays the collector fingerprint. Because it is
  deterministic (headline is `"<symbol> filed SEC Form <form>"`; the URL
  contains CIK, accession and primary document), it is also the durable key.
- **Accession number** is the strongest native filing fact. It is recorded as
  the provenance `document_id` (indexed with `source_name`) and a version
  attribute, and reconciliation checks it explicitly.

| Observation | Collector | PostgreSQL |
|---|---|---|
| first observation | processed | new event (`first`) |
| repeat within Redis TTL (24 h) | skipped by Redis | nothing written |
| repeat after Redis TTL expiry | processed (and re-alerted, existing behavior) | same event, `duplicate`; no new version, provenance or history |
| repeat after restart with lost Redis state | processed | same event, `duplicate` |
| Redis unavailable (fail-open) | processed every time | same event, `duplicate` |
| same form, different accession | separate | separate events |
| same company, date and form, different accession | separate | separate events |
| 8-K vs 8-K/A (separate accessions) | separate | separate events; no relation inferred |
| same accession under two watchlist issuers | two fingerprints | two events, sharing `document_id` (queryable) |

Persistence never merges by accession, headline similarity or fuzzy matching.

## Version semantics

SEC filings are immutable: **one filing (fingerprint) → one logical event → one
durable version**. The corpus and the live run produced exactly one version per
event. A second version can only appear if the normalizer's output for the same
fingerprint changes (e.g. a future normalizer change). `sec_promotion` then
applies the shared source-order contract: same filing date is `ambiguous` and
held; a changed form or accession is `ambiguous` and never merged;
whitespace-only changes are `cosmetic`. No version chains across filings.

## Amendments

Amended filings (e.g. `8-K/A`) remain their own accession and event, score the
existing default (10 + 25 direct = 35, `IGNORE`), and are not linked to the
original filing; the collector exposes no deterministic relation. No `/A`
scores were added.

## Outage behavior and operations

- **DB unavailable at start, down mid-run, recovery:** collector output, Redis
  dedup and Telegram calls are identical to a shadow-off run (tested with a
  reserved never-listening port and a real container stop/start). Failures are
  counted and logged without credentials; the same writer reconnects lazily
  after recovery; outage-time work is not replayed.
- **Queue full / slow DB:** submissions drop (`dropped_queue_full`) without
  blocking; all Telegram sends completed while the DB was blocked.
- **Stats:** the unchanged shared contract (`queued`, `persisted`, `duplicate`,
  `failed`, `dropped_queue_full`, `queue_depth`, `in_flight`, `worker_started`,
  `worker_stopped`, `last_success_at`, `last_failure_at`, …). No SEC-specific
  counters.
- **Shutdown:** graceful drain, immediate stop (discard count), drain timeout
  (in-flight reported), idempotent shutdown and restart with DB-backed
  duplicate detection are tested. The Phase 2G lifecycle-equivalence suite
  passes with the SEC module included.

## Reconciliation

`reconcile_sec_event(event, repository, expect_current=True)` is read-only (only
`SELECT` statements, tested). It checks event, version, current pointer,
provenance, `accession_match` (version attribute and provenance `document_id`),
score history and decision history. `ai_match` is `None` (not applicable). It
never repairs or replays.

## Replay corpus

`tests/sec_readiness_corpus.py` with manifest
`tests/fixtures/persistence/sec_readiness_v1.json` and a pinned SHA-256. Rows
are exactly what the real `process_sec_filings` submits when run over synthetic,
EDGAR-shaped filings against an in-memory Redis with a controllable clock. It
covers META 8-K, 10-Q, 10-K, Form 4, 144, 3, N-PX and 8-K/A; NVDA 8-K; same
accession repeated (Redis skip); same form or same day with a different
accession; Telegram failure; missing optional metadata (empty filing date and
primary document); Redis fail-open; repeat after TTL expiry; repeat after
restart with lost Redis; and duplicate provenance.

19 submissions → 13 events, 13 versions, 13 provenance rows, 26 history rows;
6 duplicates; decisions 11 `ALERT`, 6 `DISPLAY_ONLY`, 2 `IGNORE`;
0 reconciliation mismatches, and a repeat replay is fully idempotent.

## Live SEC validation (2026-09-24 UTC)

One request per configured company to `https://data.sec.gov/submissions/CIK{cik}.json`
with the existing headers:

| Company | HTTP | Content type | Recent filings | Newest filing | Forms in collector window |
|---|---|---|---|---|---|
| META | 200 | `application/json` | 1002 | 2026-09-23 (0 days) | 4, 144 |
| NVDA | 200 | `application/json` | 1003 | 2026-09-23 (0 days) | 4, 144 |

Representative rows: META Form 4 `0000950103-26-014403` (2026-09-23), META
Form 144 `0001921094-26-001042` (2026-09-21), NVDA Form 4
`0001696841-26-000014` (2026-09-23), NVDA Form 144 `0001921094-26-001040`
(2026-09-21). All 20 windowed filings had a path-style primary document.

The same live filings were then run through the real `process_sec_filings` with
shadow on, a disposable loopback Redis and a dedicated schema in the disposable
PostgreSQL 16.15 (Telegram replaced by a counting stub; OpenAI guarded):
20 processed, all `DISPLAY_ONLY`; a second pass skipped all 20 by Redis dedup;
20 `mias:sec:event:*` keys with 24 h TTL; 20 persisted, 0 failed, 0 duplicate;
20 events and versions (all `date` precision), 20 provenance, 40 history;
0 reconciliation mismatches; 0 Telegram sends, 0 OpenAI calls. The schema and
Redis keys were removed afterwards. Live data was fresh, but the 10-filing
window held only insider forms, so live `ALERT` forms were exercised only by
the corpus.

## Sensitive header

The configured User-Agent contains a personal contact. It is unchanged and still
passed to `requests.get` by reference. Tests assert (with messages that never
include the value) that it does not appear in collector outputs, logs, request
error logs, shadow snapshots, writer logs or stats, or any persisted row; the
live run refused to write evidence containing it. No report or doc includes it.

## Testing and the one-process ordering limitation

New suites: `test_sec_persistence_adapter` (12), `test_sec_shadow_persistence`
(24), `test_sec_persistence_postgres` (20, live), `test_sec_persistence_readiness`
(6, including one against a disposable real Redis). Running every test module
in a single Python process triggers a known, pre-existing ordering interaction
in `test_geopolitical_identity_stats` (it also fails without any SEC tests).
It is not fixed here; suites are run one process per module, as in prior
phases.

## Remaining risks

- The collector re-alerts the same filing after the 24 h Redis TTL or lost Redis
  state (existing behavior; persistence records it as a duplicate but does not
  suppress delivery).
- Only a 10-filing window per company is read; busy insider-filing days can push
  8-K/10-Q/10-K filings out of the window before they are seen.
- An accession shared by both watchlist issuers is two runtime identities and
  two durable events (queryable by `document_id`).
- A future normalizer change would change the fingerprint (new durable events)
  or add held versions; `sec-v1` identity assumes today's headline and URL shape.
- Shadow counters are per process; this was local validation, not a
  multi-day soak.
