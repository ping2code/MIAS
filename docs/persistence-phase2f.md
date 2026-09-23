# Persistence Phase 2F: geopolitical shadow persistence

Phase 2F applies the Phase 2D/2E shadow contract to the geopolitical collector
only. There is no migration and no persistence-model redesign. Fed, SEC and news
are not integrated. Geopolitical identity algorithms, alias/policy resolution,
source classification, relevance, scoring, thresholds, freshness, Redis keys and
semantics, AI and Telegram behavior are unchanged.

## Scope decisions

- **No `event_relationships` table exists.** The schema has `events`,
  `event_versions`, `event_provenance` and `event_history`; Phase 1 listed
  relationships as a later milestone. No table or migration was added. The
  relationships the collector actually establishes are all within one resolved
  action, and they are stored as `event_provenance` rows:
  - document belongs to policy/action;
  - companion official documents;
  - Federal Register public inspection → published edition.
- **No cross-event relationships are persisted.** Proposal, final, amendment and
  clarification are separate collector identities, because the stage is part of
  the alias key. The collector emits no fact linking two such events, so
  persistence does not invent `proposal→final`, `final→amendment` or
  `correction-of` links.
- **No correction concept exists in the collector.** `revision_id` is always
  `original`. A correction document is persisted as whatever the collector
  resolves: a separate event when it carries its own anchor.
- **Some documents are never persisted.** Unresolved identities, freshness skips
  before resolution (missing, future or stale dates) and irrelevant documents
  have no collector-owned `event_id`. Persistence does not assign one. A "broad
  technology event with no symbols" is therefore never persisted: every relevant
  collector event has symbols, and irrelevant documents are dropped before
  identity resolution.

## Switch and hooks

`GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED` defaults to `false`. Only `true`
(case-insensitive) enables it. It is independent of the macro and Treasury
switches. When disabled, the collector does not import the writer and creates no
worker, engine, session or database activity.

`_shadow(...)` in `collector/geopolitical_collector.py` is called in three places:

- a newly processed resolved event, after Redis caching and optional delivery;
- a cached duplicate or companion document, submitting the cached result with the
  collector's merged earliest disclosure and provenance (no reanalysis, no AI call);
- a resolved event withheld as stale, future or missing-date, submitted unscored
  with `make_current=False`.

`persistence/geopolitical_shadow.py` mirrors the Treasury module.
`GeopoliticalShadowWriter` subclasses the shared `ShadowWriter` and adds one
override: it drops the full source `body` (up to 500 KB) before the snapshot
copy. Only the 1,800-character summary and per-document SHA-256 hashes are
persisted. Engine caps are unchanged, with
`application_name=mias_geopolitical_shadow`.

The only shared-code change is an optional `extra` callback on the read-only
reconciliation comparator. The macro and Treasury paths are unchanged.

## Canonical adapter

`adapt_geopolitical` accepts only events with a known event family, a
collector-resolved `event_id` and `policy_id`, and `identity_status=resolved`.

- **Anchor:** `source_family=geopolitical`, `identity_version=geopolitical-v1`,
  and `event_key` set to the collector `event_id`.
- **Version:** `geopolitical-content-v1:` followed by the SHA-256 of the
  canonical normalized content.
- **Columns:** headline, summary, source, publisher, URL, event family, market
  scope, disclosure time and precision, publication basis, stage
  (`policy_stage`) and revision key.
- **Facts copied verbatim:**
  - agency, `document_id`, `document_type`, `policy_id`, `identity_status`,
    `identity_anchors`;
  - `geopolitical_category`, `policy_action`, `policy_stage`, `legal_status`,
    `revision_id`;
  - `original_published_at`, `effective_at`, `scheduled_publication`,
    `legal_references`;
  - symbols, direct and related symbols, `relevant`, `relevance_reasons`;
  - the full structured `evidence`: symbol, rule, matched entities, products and
    jurisdictions, policy scope, document ID, source URL, exact quote and offsets;
  - `matched_*`, `policy_scope` and `metrics`.
- **Precision:** a date-only disclosure is stored as a source-local date
  (Asia/Taipei for MOEA, America/New_York otherwise). Unknown precision with a
  timestamp is rejected rather than guessed.
- **AI isolation:** AI fields never enter the facts. The corpus enrichment stub
  tries to overwrite `policy_stage`, `symbols` and `impact_score`, and none of
  those changes is persisted.

Persistence never calls relevance, scoring, `evaluate_alert` or
`resolve_identity`, and tests enforce this. Generic China, Taiwan, AI or
semiconductor mentions cannot gain relevance through persistence.

## Alias/policy durability

Redis `alias:*` and `policy:*` keys are runtime coordination. PostgreSQL keeps the
history:

- the version attributes hold `policy_id`, `identity_anchors` and the analyzed
  `document_id`;
- every official document ever seen for the action becomes an idempotent
  provenance row. Its attributes are `relation=policy_document`, `policy_id`,
  `event_id`, `document_id`, `agency`, `published_at`, `analyzed_document` and,
  for the Federal Register only, `fr_edition` (`public_inspection` or
  `published`, derived from the official URL). The row also carries the
  collector's `content_sha256`.

Redis keeps only the latest provenance entry per document ID, so the public
inspection entry is replaced when the published edition arrives. PostgreSQL
keeps both editions.

Validated behavior:

- Expiring all Redis alias, policy and processing state leaves every PostgreSQL
  event, version, provenance and history row intact, and reconciliation still
  matches.
- The collector then resolves the same document to the same `event_id`. Redis has
  lost the companion document; PostgreSQL still links it. The resubmission is a
  duplicate, and no second durable logical event is created.
- Nothing is written back into Redis. Persistence and reconciliation never touch
  the Redis client.
- **Documented limit:** if an action was joined through a companion whose first
  sorted anchor differs, such as a BIS release anchored only by `fr:X` followed
  by an FR document with `[eo:N, fr:X]`, a post-expiry runtime resolution of the
  FR document alone picks a new root. The collector produces a new `event_id`,
  and persistence records a second logical event rather than merging. Both carry
  the shared anchor, so they are auditable. Changing this would change runtime
  identity, which is out of scope.
  Phase 2G adds a read-only audit that flags these groups; see
  [Phase 2G](persistence-phase2g.md).

## Promotion

`geopolitical_promotion` uses the shared Phase 2D `source_order_promotion`.

- **Identity facts:** `policy_id`, `identity_status`, stage, legal status,
  revision, category and action. Any difference is `ambiguous`.
- **Material content:** normalized summary, facts (excluding
  `original_published_at`) and the disclosure time.

| Case | Outcome |
|---|---|
| First version | current |
| Exact duplicate or cached companion with no new content | reused |
| New companion document | new provenance row only |
| Cosmetic headline/whitespace change | `cosmetic`, held |
| Strictly newer comparable material version | `newer_material`, promoted |
| Companion revealing an earlier disclosure, or older late observation | `older`, held |
| Resolved stale repost | `caller_disabled`, held, unscored |
| Equal time or changed identity facts | `ambiguous`, held |

Proposal, final, amendment, clarification and correction never compete with each
other; each is its own logical event. The collector cache pins the first
analysis within a resolved action, so natural flow yields `first` or
`duplicate`. The `older` and `caller_disabled` outcomes appear in the replay.
`newer_material`, `cosmetic` and `ambiguous` are covered with direct synthetic
versions.

## Outcomes and operations

The score history holds `impact_score`, original score, level,
`quality_adjustment` (always 0) and reasons. The decision history holds
`alert_decision`, `initial_decision` and a score snapshot. AI history is written
only for enrichment the collector has already validated.

Stats, bounded shutdown (`drain`, `timeout<=30`), lazy engine, disposal after
failure, credential-free rate-limited logs and restart deduplication from
database constraints are identical to macro and Treasury. There is no durable
outbox, and failed jobs are not replayed.

`reconcile_geopolitical_event(event, repository, expect_current=True)` is
read-only and returns the macro/Treasury structure plus two fields:

- `relevance_match`: stored symbols, rules and evidence;
- `relationship_match`: every expected document is linked to the event's
  `policy_id`.

## Replay corpus

`tests/geopolitical_readiness_corpus.py` runs 27 synthetic document passes
through the real collector against the in-memory Redis test double, at fixed
clocks. It captures exactly the shadow submissions, giving 24 observations. The
build is pinned by `tests/fixtures/persistence/geopolitical_readiness_v1.{json,sha256}`.

The corpus covers:

- **Actions:** adopted export controls, a semiconductor Entity List action,
  technology sanctions, a Section 301 tariff, an FTC platform/data remedy, an FTC
  complaint, a proposed rule, a substantive clarification, a MOEA production
  interruption, and an outbound investment restriction.
- **Companions:** public inspection → published edition, BIS + FR companions,
  White House action + FR, and an FR notice that reveals an earlier disclosure.
- **Stage variants:** an amendment, a correction document, and the final rule of
  a proposal.
- **Repeats:** a duplicate, a cosmetic change and a stale repost.
- **Never persisted:** a missing date, an unresolved identity, and a broad
  mention with no symbols.
- **Relevance:** direct META, indirect NVDA and direct NVDA.
- **AI:** present and absent.
- **Redis expiry:** alias/policy-only expiry and full-state expiry.

Replay results (SQLite and PostgreSQL 16.15):

```
events 14, event_versions 16, event_provenance 21, event_history 44
duplicates 5, matched 24/24, mismatches []
promotions: first 14, duplicate 8, older 1, caller_disabled 1
```

A second replay produces 24 duplicates and no new rows. The replayed collector
outputs, including Redis state, Telegram calls and AI call counts, are identical
with the switch disabled and enabled, and during a DB outage.

## Tests and PostgreSQL validation

The new test modules are:

- `tests/test_geopolitical_persistence_adapter.py` (15 tests)
- `tests/test_geopolitical_shadow_persistence.py` (17 tests)
- `tests/test_geopolitical_persistence_readiness.py` (14 tests)
- `tests/test_geopolitical_persistence_postgres.py` (40 tests)

The live module reruns the adapter and readiness classes on PostgreSQL, including
alias-expiry durability. It adds:

- worker commits of the full collector sequence;
- rollback on a repository failure;
- the database unavailable at start;
- restart with companion history;
- out-of-order observations through the worker;
- recreation after a worker exception;
- spawned-process races for an identical write, companion-provenance
  (relationship) contention, conflicting revisions and distinct histories;
- a real container stop, mid-run outage and recovery.

Use the Phase 2C disposable container recipe and the `MIAS_PHASE2C_TEST_CONTAINER`
opt-in. Remove the container and its volume afterwards.
