# Official Treasury collector

Treasury ingestion is opt-in and independent of news, SEC, Fed and macro ingestion.
It has three source paths: official release documents, TreasuryDirect auctions,
and daily Treasury yield observations. Default Telegram delivery is disabled.
All changes are based on main commit `364e510`; existing collector code is unchanged.

## Sources and live validation

Validated on September 22, 2026, without dotenv, Redis, OpenAI or Telegram calls:

| Source | HTTP | Content type | Observed contract |
| --- | --- | --- | --- |
| `https://www.treasurydirect.gov/TA_WS/securities/search?format=json&auctionDate=2026-09-01,2026-09-22` | 200 | application/json | Bare array, 26 records, 120 fields per sampled record |
| `https://www.treasurydirect.gov/TA_WS/securities/search?format=json&announcementDate=2026-09-17,2026-09-22` | 200 | application/json | Seven records; includes future auctions already announced |
| `https://www.treasurydirect.gov/TA_WS/securities/search?format=json&auctionDate=2026-09-21` | 200 | application/json | Two completed bill auctions |
| `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month=202609` | 200 | text/xml; charset=UTF-8 | Atom/OData feed, 14 observations through September 21 |

No HTTP redirects occurred for these endpoints. The implementation also successfully
queried individual announcement and auction dates and August/September yield months.

Auction fields include `cusip`, `securityType`, `securityTerm`, `announcementDate`,
`auctionDate`, `offeringAmount`, `totalAccepted`, `bidToCoverRatio`, rate fields,
TIPS/FRN/reopening flags, release document filenames, and `updatedTimestamp`.
Amounts and rates arrive as strings; missing fields often use empty strings.
Announcement records can exist before result numbers and result documents exist.
The collector never infers completion from the auction calendar alone.

Auction dates are date labels encoded as midnight without a timezone. A result
requires both numeric fields and an `R_YYYYMMDD_N.pdf` identifier matching the
auction date. Announcements require their corresponding `A_YYYYMMDD_N.pdf`
identifier. Publication is conservatively represented as midnight Eastern on the
verified document date with `timestamp_precision="date"`. This is not a claimed
precise release time. `updatedTimestamp` is not used to refresh publication or ID.
Rows with unsupported document names/date mismatches are rejected for review.

The JSON response has no pagination envelope or total-count field. A hard server
query cap/rate limit could not be verified: TreasuryDirect's developer page now
links to a Fiscal Service community portal that requires browser rendering.
Do not interpret successful small queries as proof of unlimited access.
The collector issues separate daily announcement-date and auction-date queries
covering the freshness window plus one day; it rejects filter mismatches and
responses reaching its own 1,000-record bound. This bound is not a documented
server limit. Conflicting snapshots during a poll cause a retryable source failure.

Yield XML contains Atom entries, `m:properties`, `d:NEW_DATE`, `d:Id`, and `BC_*`
tenor values. Values are percent, not basis points. Optional null tenors remain
missing. `Id` and entry URLs are not used as identity: dataset/date is stable.
The observed feed and every entry had the same current `<updated>` timestamp,
including old observations. It therefore cannot establish publication time.

Treasury documents pagination for all-history XML: page zero by default, 300 rows
per page. This collector uses current and previous monthly queries instead,
checks returned month membership, and does not request all-history pages.
No formal request-rate allowance is assumed.

Official reference documents:
- https://www.treasurydirect.gov/legal-information/developers/web-api-security/
- https://home.treasury.gov/treasury-daily-interest-rate-xml-feed

Release discovery uses only:
- https://home.treasury.gov/news/press-releases
- https://home.treasury.gov/policy-issues/financing-the-government/quarterly-refunding/most-recent-quarterly-refunding-documents
- https://home.treasury.gov/policy-issues/financial-markets-financial-institutions-and-fiscal-service/debt-limit

The release indexes and sampled press release returned HTTP 200. Press bodies
come from `field--name-field-news-body`, and publication from the official
`field--name-field-news-publication-date` time element. Debt letters require
successful PDF text extraction and a letter date. Failed/blank/encrypted PDF
extraction never falls back to inferred content from link labels, filenames or AI.

Live validation found the press index's HTML next-page link repeats. The index
advertises `/news-data/press-releases/manifest.json`, which returned HTTP 200
application/json and lists year-based `searchShards`. Its current-year shard at
`https://home.treasury.gov/news-data/press-releases/search/2026.json` returned
HTTP 200 application/json, with category/count/items/range and 261 release items.
The collector uses this advertised official manifest and the two newest year
shards, verifies counts and source links, then selects the newest 40 documents.
Publication and content still come from each release document, not shard metadata.
Other index paths follow bounded official pagination; repeated links preserve
already discovered documents with a warning instead of losing valid releases.
Debt-document discovery is capped at 40 links in source order, with a warning.

All source fetches use exact allowed HTTPS hosts, no redirects, 5/20-second
connect/read timeouts and a 5 MB body limit. PDF extraction is limited to 20 pages.
There are no unofficial sources, browser impersonation or anti-bot workarounds.

## Identity and event schema

Events preserve MIAS fields and add `treasury_category`, `release_id`,
`release_stage`, `revision_id`, `reference_period`, `timestamp_precision`,
`publication_basis`, `event_id` and `metrics`. Symbols are empty and market scope
is `US Treasury / rates`. Release events use `event_type="treasury_release"`;
yield records use `event_type="treasury_yield_observation"`.

SHA-256 identity input:
`[1, "treasury", release_id, release_stage, revision_id]`.

| Family | Release identifier | Stage |
| --- | --- | --- |
| Press/refunding/borrowing | `press:<official-release-id>` | release |
| Debt letter | `letter:<official-path>:<letter-date>` | letter |
| Auction | `auction:<CUSIP>:<auction-date>:<security-type>` | announcement or result |
| Yield | `yield:daily_treasury_yield_curve:<observation-date>` | observation |

Same document links across indexes are fetched once. Auction stages are distinct,
as are later reopenings of the same CUSIP. Cosmetic titles and numeric changes do
not change identity. Explicit dated press corrections create a revision, retaining
the original publication time. Unversioned API/series corrections never reset
freshness or create a new alert. Cached data is a processing snapshot, not a
continuously revised time-series database. Separate official documents describing
the same policy may remain separate events; no fuzzy/news near-duplicate logic
is applied.

## Scores and AI

| Category | Score |
| --- | ---: |
| Debt-limit funding-risk development | 95 |
| Extraordinary-measures action | 90 |
| Quarterly refunding statement / issuance-policy change | 85 |
| Borrowing estimates / qualifying financial-stability action | 80 |
| Nominal note/bond auction result | 70 |
| TIPS/FRN auction result | 60 |
| Routine auction announcement / bill result | 40 |
| Routine yield observation | 30 |
| Valid benchmark yield movement crossing configured threshold | 70 |
| Routine/unclassified release, remarks, TBAC advice | 10 |

Classification is intentionally conservative and requires substantive rule matches.
Policy recommendations and historical remarks are not treated as adopted policy.
Scores measure release importance, not expected price direction. No auction tails,
surprises, or demand comparisons are derived without an authoritative benchmark.

`evaluate_alert()` retains existing thresholds. `quality_adjustment=0` for all
Treasury releases and data. Only deterministic, fresh, publication-verified ALERT
candidates reach the lazy OpenAI wrapper. Only validated AI enrichment fields are
copied back; parser metrics, dates, identity, scores and summary cannot be replaced.
AI is instructed not to infer missing auction comparisons. Official metrics remain
visible in Treasury alert formatting independently of AI commentary. AI text is
not a verified data source and may still be inaccurate. Failures retain the
deterministic event without news opinion/prediction penalties.

## Yield time-series and freshness

`TREASURY_MAX_AGE_HOURS` defaults to 48.
`TREASURY_YIELD_MOVE_BPS` defaults to 15 and must be positive and finite.
Both use the existing shared configuration mechanism; no .env edits are needed.
Tests mock dotenv and live validation imports only pure source modules.

Routine observations remain low impact. Promotion requires an actual previous
official observation, matching tenors, and absolute movement at least the configured
threshold at 2, 10 or 30 years. Changes use decimal arithmetic and convert percentage
point differences to basis points. The prior observation must be no more than four
calendar days earlier; longer gaps suppress promotion. The 2s10s spread is retained
when both tenors are present. No zero filling or synthetic observations are used.

Yield `observation_date` is separate from `published_at`. The current XML adapter
cannot verify publication, so `published_at` stays null. Recent observations can
be retained/scored in the time-series path using an explicitly conservative
observation-date age check. A promoted score may have `initial_decision="ALERT"`,
but final decision is `DISPLAY_ONLY` with `alert_eligible=false`; it never invokes
OpenAI or Telegram. Low-impact observations remain IGNORE. This distinction keeps
data ingestion useful without presenting feed rebuilds as fresh releases.

Other events require a verified publication date. Missing dates, future publication,
and age beyond 48 hours are skipped/logged before state processing. Delivery
rechecks freshness. There is no weekend/holiday extension and no fetch-time fallback.
Date-only timestamps can expire earlier than the actual publication time would.

## Redis and delivery

| Key | Purpose |
| --- | --- |
| `mias:treasury:event:<id>` | Processing lease, 900 seconds |
| `mias:treasury:processed:<id>` | Cached final event |
| `mias:treasury:event:<id>:delivery` | Delivery lease, 900 seconds |
| `mias:treasury:delivered:<id>` | Confirmed Telegram success |

Cache writes and lease releases verify ownership atomically with Lua. Cache and
success markers live for max(dedup TTL, freshness window) plus one second.
Redis failures intentionally fail closed; they do not change existing collectors.
Dry runs do not write delivered markers. Failed sends remain retryable while fresh;
cached retries repeat neither parsing-owned decisions nor AI analysis.

This targets the existing single-instance Redis client. Cluster key-slot handling
is not provided. Ambiguous Telegram timeouts or marker-write failures can still
cause duplicate delivery. There is no independent outbox; retries depend on a
later successful source poll. A lease that expires during analysis cannot publish
its stale result.

## Operation and tests

```sh
python -m collector.treasury_collector --no-ai
```

Telegram is off unless `--send-alerts` is explicitly passed. Programmatic entry:
`collect_treasury_events(enable_ai=True, send_alerts=False)`.

The aggregate collector adds `include_treasury=False`, `treasury_enable_ai=True`,
and `treasury_send_alerts=False`. Existing defaults and four aggregate counters
are preserved. Existing news collection still has its existing behavior; use the
standalone Treasury entry point for isolated Treasury operation.

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_fed_pipeline tests.test_macro_pipeline tests.test_treasury_pipeline -v
```

HTTP, Redis, OpenAI, Telegram and dotenv are mocked. Fixtures include captured
public auction and yield excerpts, synthetic release/letter content, and generated
in-memory PDFs. Tests cover schema, identifiers, release stages, revisions,
freshness, valid/missing yield comparisons, source failures, AI mutation attempts,
PDF failures, Redis lease/cache failures, dry runs and delivery retries, plus the
existing 86 Fed/macro regression tests. No live Telegram or OpenAI validation was
performed. See the fixture README for provenance.

## Remaining limits

Source schemas, filenames and publication conventions can change. Unknown required
structures fail closed. Source HTTP/date/size errors are reported without raw
exception details. Fetches and discoveries are bounded, not an archive catch-up
service; document/page caps and broken pagination can limit coverage. Source limits
and polling frequency must be reviewed before deployment. This collector has no
scheduler or server-wide rate-budget manager. PDF decompression/text extraction
can still be resource intensive despite compressed-body/page limits. OCR and
document-layout inference are deliberately absent.
