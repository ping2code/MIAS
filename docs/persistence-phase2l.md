# Persistence Phase 2L: Federal Reserve shadow persistence

Phase 2L adds opt-in, shadow-only persistence for the Federal Reserve collector,
using the existing persistence model, repository, promotion contract, shared
writer, shared lifecycle helper and reconciliation core. There is no migration,
and SEC and news are not integrated.

Fed identities, scoring, freshness, Redis processed/delivered/claim semantics,
delivery retry, Telegram, OpenAI, collector defaults and function signatures are
unchanged. No existing Fed test was modified.

## Shadow switch

`FED_PERSISTENCE_SHADOW_ENABLED` defaults to `false`. Only `true`
(case-insensitive) enables it, and it is independent of the other switches.

When disabled, the collector does not import the writer and creates no worker,
engine, session or database activity.

When enabled, `persistence/fed_shadow.py` works exactly like the other families.
It uses the shared `ShadowWriter` (queue, counters, bounded logs, lazy engine,
disposal after failure) and the shared `ShadowLifecycle`: lazy singleton,
bounded idempotent shutdown, fork reset, atexit drain. Engine caps match, with
`application_name=mias_fed_shadow`. The Phase 2G lifecycle-equivalence suite is
re-run with the Fed module included, and a Fed-vs-macro script produces
identical counters.

`_shadow(...)` in `collector/fed_collector.py` runs after the collector's own
Redis writes and delivery, in four places:

| Collector path | Submitted | `make_current` |
|---|---|---|
| Newly processed (scored, optional AI) | the processed event | `True` |
| Cached processed duplicate (dry-run later delivered, retry, repeat poll) | the cached JSON result, parsed for the shadow copy only | `True` |
| Stale (older than `FED_MAX_AGE_HOURS`) | the unscored normalized event | `False` |
| Missing or invalid publication date | the unscored normalized event | `False` |

The `is_duplicate` processing-claim path carries no result and is not
submitted. Any import, initialization, parse or enqueue failure is caught and
logged at most once per minute without detail.

## Adapter mapping (`persistence/adapters/fed.py`)

- **Identity:** the collector's own Redis fingerprint
  (`deduplicator.create_fingerprint`: SHA-256 of lower-cased
  `headline|url`) is computed **in the collector** and passed as
  `fed_fingerprint`. The adapter requires it (64 hex characters) and never
  recomputes it, so persistence never imports the Redis deduplicator or config.
  The anchor is `source_family=fed`, `identity_version=fed-v1`, and
  `event_key` = the fingerprint.
- **Version:** `fed-content-v1:` followed by the SHA-256 of the normalized
  content.
- **Columns:** headline, summary, source and publisher (`Federal Reserve`),
  URL, `event_type=fed_policy`, market scope (`US macro`), published time, and
  stage (`fed_category`). There is no revision key, because the collector has
  no revision concept.
- **Precision:** Fed events carry no precision field. The normalizer either
  produces a parsed feed timestamp or nothing. Precision is `second` with
  publication basis `fed_rss_published_or_updated` when a timestamp exists, and
  `unknown` with `unverified` otherwise. It is labelled as derived
  (`timestamp_precision_basis`), never claimed by the source.
- **Facts copied verbatim:** `fed_category`, the symbol lists (empty unless a
  company is actually mentioned; nothing is invented), `relevant`, and the
  collector's feed URL (`fed_source_feed`).
- **Provenance:** one row per version for the official release URL. It records
  role `fed_monetary_policy_release`, the feed URL, published time, precision,
  basis and retrieval basis. `document_id` is null because the collector keeps
  no native entry ID. It is idempotent, and new evidence (for example a
  `source_hash`) appends a row.
- **Outcome history:** the score history holds `impact_score`,
  `original_impact_score`, `impact_level`, `quality_adjustment` and
  `score_reasons`; the decision history holds `alert_decision` with a score
  snapshot. Both are appended only when present. The AI history holds only
  already-validated enrichment (sentiment enum, integer confidence 0–100,
  non-empty strings). Persistence makes no AI call and never re-scores or
  re-decides; tests enforce this.

**Quality adjustment is recorded as the collector computed it.** The
deterministic Fed score sets no `quality_adjustment`, so it is absent without
AI and not invented. After AI, the existing collector applies
`adjust_alert_quality`, which can penalize some AI event types: an "opinion
article" gives `-10`, turning 85 into 75. Persistence records exactly that
value. Forcing Fed events to `quality_adjustment=0` would change Fed scoring,
which is out of scope for this phase.

## Identity and promotion

| Case | Behavior |
|---|---|
| Exact repeat (cached path) | same event and version; duplicate |
| Summary-only or headline-case change | same fingerprint, so the collector returns the **cached** result; duplicate, no new version |
| Headline or URL text change (beyond case and outer whitespace) | new collector fingerprint, so a new logical event (this is the collector's identity) |
| Stale rediscovery of a known release | unscored, `make_current=False`; exact duplicate, no refresh or promotion |
| Missing date | stored with `unknown` precision, non-current, never alerting |

Promotion uses the shared Phase 2D `source_order_promotion`, with `fed_category`
as the identity fact. The material content is the normalized summary, the facts,
event type, market scope and stage.

- A strictly newer comparable timestamp gives `newer_material`.
- An older one gives `older`, and whitespace-only changes give `cosmetic`.
- Equal time, unknown precision, or a changed category gives `ambiguous`.

The collector never re-analyzes a cached event, so natural Fed flow produces
`first` or `duplicate`. Synthetic revisions derived from a collector event
exercise `newer_material` and `older`. Fed semantics fit the contract without
redesign.

## Redis processed/delivered preservation

Persistence never touches Redis. Across the full 16-step corpus the collector
produced identical output with shadow disabled and enabled: events, stats,
every Redis key, value and TTL, Telegram call counts and AI call counts. The
same holds with shadow enabled and submit failing, with a real writer
succeeding, with the DB unavailable, and with a blocked DB and a full queue.
Only the `mias:fed:event|processed|delivered:` namespaces are ever written.

The corpus also confirms each specific delivery behavior:

- **Dry run:** processed, shadowed, and no delivered marker. A later
  delivery-enabled poll delivers the cached result with no second AI call.
- **Failed Telegram delivery:** no marker, so it stays retryable. The retry
  delivers from cache with no AI call, and the marker then prevents a resend.
- **Invalid cached JSON:** the collector outputs are unchanged. The shadow copy
  is simply skipped with a bounded warning.

## Reconciliation

`reconcile_fed_event(event, repository, expect_current=True)` is read-only and
uses the shared comparator. `event` is the submitted snapshot, including
`fed_fingerprint`. It checks:

- the event and version exist, and the version content matches;
- the current pointer;
- provenance;
- score, decision and AI history (`None` when not expected).

It never repairs or replays. The mismatch warning is bounded
("Fed shadow reconciliation mismatch").

## Replay corpus

`tests/fed_readiness_corpus.py` runs 16 steps of the real Fed collector against
an in-memory Redis at fixed clocks. It stubs the feed, AI and Telegram and makes
no network calls. It captures exactly the shadow submissions and adds two
synthetic revision rows. The build is pinned by
`tests/fixtures/persistence/fed_readiness_v1.{json,sha256}`.

The corpus covers:

- **Categories:** policy statement, policy action, economic projections,
  minutes (with AI disabled), and policy communication, which is the
  normalizer's fallback category (unknown categories score 40 in
  `score_fed_event`).
- **Symbols:** a direct NVDA mention.
- **Repeats and changes:** a duplicate, a summary-only change, a headline-case
  change, and a provenance duplicate.
- **Delivery:** a dry run followed by cached delivery, and a delivery failure
  followed by a retry.
- **Dates:** a stale event, a missing date, and a stale rediscovery.
- **Revisions:** a synthetic material revision and a synthetic older
  observation.
- **AI:** present and absent.

Replay results (SQLite and PostgreSQL 16.15):

```
events 8, event_versions 10, event_provenance 10, event_history 21
duplicates 8, matched 18/18, mismatches []
promotions: first 8, duplicate 8, newer_material 1, older 1
```

A second replay produces 18 duplicates and no new rows.

## Outage behavior and PostgreSQL validation

These cases were validated on a disposable PostgreSQL 16.15 using the Phase 2C
recipe, with real transactions and zero skips:

- **Outages:** the DB unavailable before the writer starts (a reserved,
  never-listening port); the real container stopped mid-run and restarted; the
  same writer reconnecting lazily and deduplicating; no automatic replay of
  outage-window work.
- **Collector parity:** Redis processed/delivered state, Telegram and OpenAI
  identical to shadow disabled throughout.
- **Worker behavior:** full-corpus worker commits and reconciliation; rollback
  on a repository failure; restart deduplication; out-of-order revisions through
  the worker; recreation after an exception.
- **Concurrency:** spawned-process races for an identical write, concurrent
  material revisions and provenance contention.

Test modules:

- `tests/test_fed_persistence_adapter.py` (14 tests)
- `tests/test_fed_shadow_persistence.py` (22 tests)
- `tests/test_fed_persistence_postgres.py` (23 tests)

## Remaining risks

- The Fed fingerprint is headline-and-URL based. A material headline edit on
  the same release becomes a new collector event, and therefore a new durable
  event. This is the collector's existing behavior, recorded faithfully.
- There is no native feed entry ID or source hash, so provenance relies on the
  canonical URL. Timestamp precision is derived, not source-declared.
- AI quality penalties can move Fed scores and decisions, as they already do
  today. Persistence records this; any exemption is a scoring decision.
- There is no outbox, as in 2B–2J. Snapshots submitted during an outage are
  counted and lost.

## Next phase recommendation

Carry this and the geopolitical rollout evidence into the pending real-staging
rollout (the Phase 2K scope) before production enablement. Keep SEC and news out
of scope until that evidence exists. If Fed exemption from AI quality penalties
is desired, handle it as a separate, explicitly approved scoring change.
