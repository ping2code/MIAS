# Persistence Phase 2Q: News/RSS shadow persistence

Phase 2Q adds opt-in, shadow-only, non-blocking persistence for News/RSS
articles (`collector/rss_reader.py`, used by `multi_source_collector`). It reuses
the existing architecture unchanged: repository, schema (no migration; `news`
was already an allowed `source_family`), bounded shadow writer, shared lifecycle,
counters and read-only reconciliation.

Nothing about news runtime behavior changes:

- scoring, source-quality weights, relevance rules and decisions;
- exact Redis dedup (`mias:news:event:*`, 24 h) and near-duplicate headline
  detection (`mias:news:headline:*`, 24 h, threshold 0.80), including fail-open;
- OpenAI invocation (ALERT candidates only), the AI quality penalties, Telegram
  delivery and the configured feeds.

## Switch and hook

`NEWS_PERSISTENCE_SHADOW_ENABLED` (default `false`; exact value `true` enables)
lives in `shared/config.py`. `.env` was not read or modified. When disabled there
is no import, worker, engine, session or DB activity (tested).

`read_feed` calls `_shadow(event, outcome)` at two points, after all collector
logic has run:

- **processed:** at the end of the relevant-article branch, after exact dedup,
  near-duplicate check, scoring, decision, optional AI enrichment and quality
  adjustment, re-evaluation, and the Telegram attempt;
- **near-duplicate suppressed:** in the near-duplicate branch, just before the
  collector skips the article.

The hook submits a copy with the collector's fingerprint and outcome added; the
event returned to the collector is unchanged. Every failure is swallowed with a
rate-limited `News shadow submission failed` warning. Irrelevant items and
Redis exact duplicates are never scored or decided by the collector, so they are
never submitted.

## Exact durable identity (only this determines identity)

`news-url-v1`: `sha256("news-url-v1|" + url.strip().lower())`. This is the URL
half of the collector's own exact dedup fingerprint (`headline|url`), normalized
exactly as the collector normalizes it. It is a documented new identity rule for
persistence: the collector's fingerprint includes the headline, so it cannot
keep one article's identity across a headline edit.

- The same article repeatedly, after Redis expiry, after restart or through
  Redis fail-open is the same durable event.
- A different URL is always a different event, whatever the headline, publisher
  or similarity.
- The same URL with a changed headline is the same event (see versions).
- Headline, summary, publisher, feed, near-duplicate similarity, AI output and
  story similarity never take part in identity. No fuzzy matching, embeddings,
  AI or clustering.
- **Fallback `news-fingerprint-v1`:** an item without an http(s) link (the
  normalizer emits `"N/A"`) uses the collector's full fingerprint, so link-less
  items never collide on the placeholder. No redirects are resolved and no
  publisher pages are fetched.

## Near-duplicates (advisory only)

Near-duplicate headline detection stays advisory and never merges events. When
the collector suppresses an article as a near-duplicate, the article is still
persisted as its own event (its own URL), with no score, decision or AI, and a
decision snapshot `{"collector_outcome": "near_duplicate_suppressed"}`.
Processed articles carry `collector_outcome: "processed"` in their decision
snapshot. The collector exposes only the outcome, not the similarity value or
the matched headline; persistence never reads Redis, so neither is stored. No
schema or relationship table was added.

## Version semantics

The version holds the article: headline, summary, publisher (as `source_name` and
`publisher`), canonical URL, publication time and the relevance facts `symbols`,
`direct_symbols`, `related_symbols` and `relevant`. Unchanged re-observations are
`duplicate` (no new version). A changed headline or summary at the same URL is a
new immutable version under the shared source-order contract (`news_promotion`):

| Change at the same URL | Result |
|---|---|
| none | `duplicate` |
| whitespace only | `cosmetic`, held |
| headline/summary changed, same or unknown publication time | `ambiguous`, held (current unchanged) |
| changed, strictly later comparable publication time | `newer_material`, promoted |
| changed, earlier publication time | `older`, held |

Arrival order never promotes.

## The two mandatory cases

- **Same headline, different URL** (same or different publisher): separate durable
  events. The later ones are suppressed by the collector as near-duplicates
  (similarity 1.0) and recorded with that outcome. There is no durable merge.
- **Same URL, different headline:** one durable event. A small edit is also
  suppressed by the collector as a near-duplicate of the original. It is stored as
  a new, held version with the suppressed outcome. A rewrite dissimilar enough to
  be processed is also a new, held version (same RSS publication time →
  `ambiguous`), with its own score and decision history. The original stays
  current.

## Provenance

Provenance describes where the article was seen:

- `source_name` is the feed label (e.g. `Google News META`);
- the canonical URL is stored exactly as exposed;
- attributes: feed, publisher, the collector fingerprint, the identity version,
  the raw published value, precision and basis, and the retrieval basis.

A repeat in the same feed adds nothing; the same article seen via another feed
adds one provenance row to the same version. The feed label and publisher are
never collapsed.

## Timestamps

RSS dates parsed by the normalizer are stored with `second` precision
(`rss_published`). A missing date is stored as `unknown`/`no_feed_date`. An
unparseable or timezone-less value is stored as `unknown`/`unparsed_feed_date`,
with the raw value in `published_at_raw`. Nothing is guessed. News has no
freshness filter; stale articles are processed and persisted normally.

## Scores, decisions and AI

History records exactly what the collector computed. There is no rescoring and no
extra AI call.

- **Score history:** `impact_score`, `impact_level` and `score_reasons` (source
  quality appears there as the collector wrote it). `original_impact_score` and
  `quality_adjustment` are included only when AI adjustment ran.
- **Decision history:** `alert_decision`, the collector outcome and a score
  snapshot.
- **AI history:** only complete, validated enrichment (`ai_summary`,
  `ai_sentiment`, `ai_confidence`, `ai_why_it_matters`, `ai_event_type`). Partial
  or failed enrichment is not persisted. The collector sets no provider or model
  field, so none is stored. AI never affects identity or version content.
- **Penalty flips decision:** the corpus includes a prediction-article penalty
  (80 → 65), which flips `ALERT` to `DISPLAY_ONLY` before delivery. The recorded
  decision is the final one.
- **Repeat after expiry:** the collector re-scores, and on `ALERT` re-analyzes
  and re-sends (existing behavior). The recency bonus may have decayed, so a
  distinct recomputed score is appended as a new history row on the same version
  (idempotent per distinct content).

## Redis

Persistence never reads or writes Redis. Across the corpus, runs with
persistence off and on produce identical events, stats, printed output, logs,
Redis state, AI call counts and Telegram calls. Against a real disposable
Redis:

- only `mias:news:event:*` and `mias:news:headline:*` keys exist, with 24 h TTLs;
- near-duplicate scanning suppresses as before;
- persistence failure leaves Redis identical;
- a real expiry of the article's keys causes reprocessing, which PostgreSQL
  records as the same durable event;
- an unreachable Redis still fails open.

## Outage, operations and reconciliation

- **DB unavailable at start, stopped mid-run, recovered:** collector output,
  Redis, AI calls and Telegram match a persistence-off run (a reserved closed
  port; a real container stop/start). Failures are counted and logged without
  credentials. The same writer reconnects lazily. Outage-time work is not
  replayed.
- **Stats and shutdown:** the unchanged shared counter contract and
  `shutdown(drain, timeout)`: graceful drain, immediate stop, bounded timeout,
  idempotent shutdown, and restart with DB-only duplicate detection. The Phase 2G
  lifecycle-equivalence suite passes with the News module included.
- **Counters and history:** `duplicate` counts writes that added nothing. A
  repeat that appended a recomputed score is a durable duplicate (`promotion`
  `duplicate`), but it is not counted as one.
- **Reconciliation:** `reconcile_news_event(event, repository, expect_current=True)`
  is read-only (only `SELECT`). It checks the event, version, current pointer,
  provenance, score, decision (including the near-duplicate outcome) and AI
  (only when validated enrichment was present). It never repairs or replays.

## Replay corpus

`tests/news_readiness_corpus.py`, with manifest
`tests/fixtures/persistence/news_readiness_v1.json` and a pinned SHA-256. Rows
are exactly what `read_feed` submits for synthetic entries. The cases are:

- **Articles and publishers:** META direct (Reuters), NVDA direct (Yahoo feed),
  related-symbol (CNBC), a Yahoo Finance publisher, a low-quality publisher, and
  no-symbol (not submitted).
- **Duplicates and identity:** exact duplicate, cross-feed duplicate, repeat after
  Redis expiry, repeat after restart, same headline with a different URL (same and
  other publisher), same URL with a small edit or a rewrite, near-duplicates below
  and above threshold, Redis fail-open, and duplicate and cross-feed provenance.
- **Outcomes and AI:** ALERT, DISPLAY_ONLY, suppressed; AI present, AI absent, AI
  failure, and AI penalty.
- **Timestamps:** stale, missing and unparseable timestamps, and a missing link.

24 submissions → 17 events (16 URL identities, 1 fallback), 19 versions,
20 provenance rows, 43 history rows. Promotions: 17 `first`, 5 `duplicate`,
2 `ambiguous`. Every row reconciles; a repeat replay is idempotent.

## Live validation (2026-09-24, disposable services)

One live read of each configured feed through the real `read_feed`, then an
immediate second read. It used disposable loopback Redis and PostgreSQL 16, a
recording Telegram stub (nothing sent), and a guard on the OpenAI entry point, so
no live OpenAI call was made. ALERT candidates took the collector's existing
AI-failure path. The report records counts only (no headlines or bodies).

| Feed | HTTP | Items | Fetched (limit) | Relevant | Processed | Submissions | Pass 2 exact duplicates |
|---|---|---|---|---|---|---|---|
| Yahoo Finance META/NVDA | 200 XML | 20 | 10 | 9 | 9 (2 ALERT) | 9 | 9 |
| Google News META | 200 XML | 100 | 10 | 10 | 10 (2 ALERT) | 10 | 10 |
| Google News NVDA | 200 XML | 101 | 10 | 9 | 9 (1 ALERT) | 9 | 9 |

- **Duplicates:** there were no live near-duplicates.
- **Persistence:** 28 submissions → 28 events (all `news-url-v1`, all
  second-precision) and 56 history rows. The writer persisted 28 with 0 failed and
  0 dropped; 28/28 reconciled clean.
- **Redis:** 28 exact and 28 headline keys, TTL 24 h.
- **Side effects:** 5 blocked OpenAI attempts, 5 would-be Telegram sends, and 0
  real sends or AI calls. The schema and Redis keys were removed afterwards.

## Testing notes

New suites: `test_news_persistence_adapter` (14), `test_news_shadow_persistence`
(22), `test_news_persistence_readiness` (6, one against a disposable real
Redis) and `test_news_persistence_postgres` (22, live). The pre-existing
one-process ordering interaction in `test_geopolitical_identity_stats` is
unchanged; suites run one process per module, as in prior phases. The earlier
news "tests" (`test_full_alert_flow`, `test_headline_similarity`,
`test_openai*`, `test_telegram`) are scripts, not unittest suites; the parity
tests above are the first automated news-pipeline regression coverage.

## Remaining risks

- **Repeat work after expiry:** the collector re-analyzes (OpenAI) and re-sends
  `ALERT` articles after Redis expiry or loss (existing behavior). Persistence
  records a durable duplicate only.
- **URL variants:** tracking-parameter or redirect variants of one article are
  different URLs, and therefore different durable events (conservative, never
  merged). Google News article links are opaque per-item URLs; no redirect is
  resolved.
- **Suppressed near-duplicates:** they carry no similarity value or matched
  headline, since the collector does not expose them. A story shared across
  outlets is several durable events, by design.
- **Unordered headline edits:** feeds usually keep the publication time when
  editing, so edited headlines are held as `ambiguous` versions and never become
  current automatically.
- **Link-less items:** the fingerprint fallback is headline-dependent, so an
  edited link-less item becomes a new event.
- **Scope:** counters are per process; this was local validation, not a soak.
