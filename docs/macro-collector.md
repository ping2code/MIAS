# Official macroeconomic collector (Phase 1)

The macro collector is independent of the Fed, SEC, and news collectors. It
ingests only these six government release families:

| Release | Official discovery/release endpoint |
| --- | --- |
| CPI | https://www.bls.gov/feed/cpi.rss |
| PPI | https://www.bls.gov/feed/ppi.rss |
| Employment Situation | https://www.bls.gov/feed/empsit.rss |
| GDP | https://www.bea.gov/data/gdp/gross-domestic-product |
| Personal Income and Outlays / PCE | https://www.bea.gov/data/income-saving/personal-income |
| Advance Retail Sales | https://www.census.gov/retail/sales.html |

For BEA, the collector follows the labeled **Current Release** link to a
`https://www.bea.gov/news/<year>/...` document. It extracts that document's
release date and content, rather than its product page's modification date.
Census reuses its current-release URL. BLS RSS links are provenance only and are
never fetched; URLs are not event IDs.

Requests require HTTPS on the exact agency hostname, reject credentials in URLs,
do not follow redirects, enforce connect/read timeouts of 5/20 seconds, and limit
responses to 2 MB. Each source's failures are logged and counted separately.
These endpoints require no API key. No Treasury endpoints are included.

## Operation

```sh
python -m collector.macro_collector --no-ai
python -m collector.macro_collector
```

Both commands disable Telegram delivery. The first also disables OpenAI. Use
`--send-alerts` only when delivery is intended. The collector reuses the existing
Telegram notifier and formatter; it does not change their retry or destination
configuration. Non-delivery runs still cache processing results in Redis.

Programmatic entry points:

```python
collect_macro_events(enable_ai=True, send_alerts=False)
collect_all_sources(include_macro=True, macro_enable_ai=True, macro_send_alerts=False)
```

All existing `collect_all_sources()` defaults remain unchanged. Macro flags
control only macro ingestion and delivery. Existing RSS sources can still send
their normal alerts when using the multi-source entry point; use the standalone
macro collector to run only this pipeline.

`MACRO_MAX_AGE_HOURS` is a positive integer in `shared/config.py`, defaulting to
48. It uses existing environment/dotenv configuration; no `.env` edits are
necessary. Tests mock dotenv, so they never read local secrets. Ordinary runtime
configuration retains the application's existing dotenv behavior.

## Events and identity

Events preserve the existing MIAS dictionary fields, including `source`,
`publisher`, `headline`, `url`, `published_at`, `summary`, ticker lists,
`relevant`, scoring fields, and decisions. They use `event_type="macro_release"`
and `market_scope="US macro"`. Ticker lists are empty because these releases
describe the national economy. The formatter displays `Ticker: N/A` and the
market scope.

Additional metadata: `agency`, `release_category`, `reference_period`,
`release_stage`, `release_id`, `revision_id`, `original_published_at`,
`timestamp_precision`, and `event_id`.

The event ID is SHA-256 over a versioned JSON array:

```text
[1, agency, category, reference_period, stage, release_id, revision_id]
```

Agency report numbers are normalized, for example `USDL-26-1496`, `BEA-26-38`,
or `CB26-153`. If absent, a deterministic agency/category/period/stage fallback
is used. Reference periods are `YYYY-MM` or `YYYY-Qn`. GDP advance, second, and
third estimates are distinct. Retail sales uses `advance`; the other monthly
releases use `initial`.

Explicit dated correction notices create a new revision identity and publication
time while retaining `original_published_at`. If several notices exist, the
latest timestamp wins. Ordinary mentions of prior-month revisions, changed HTML,
footer timestamps, and headline/URL changes do not create new events. Undated or
unrecognized corrections do not create a new version; review source changes
before broadening the correction grammar.

One release yields one event: payrolls, unemployment, and wages stay together,
as do headline/core CPI and headline/core PCE. Release prose is cleaned and
bounded to 6,000 characters for the existing analyzer. BEA and Census preserve numeric
facts in release prose. BLS additionally provides structured `metrics` with
series IDs, reference periods, units, adjustment basis, comparison values,
derived changes, and source footnotes. No consensus surprises are inferred.

## Scoring and AI

| Category/stage | Score | Level |
| --- | ---: | --- |
| CPI | 90 | HIGH |
| PPI | 80 | HIGH |
| Employment Situation | 90 | HIGH |
| GDP advance | 85 | HIGH |
| GDP second / third | 70 | HIGH |
| Personal Income and Outlays / PCE | 90 | HIGH |
| Advance Retail Sales | 80 | HIGH |

These scores represent release importance, not expected equity direction.
`evaluate_alert()` applies the existing configurable decision thresholds.
Only deterministic `ALERT` candidates invoke the existing lazy OpenAI analyzer.
Validated AI enrichment is copied to the event; invalid output or service errors
retain the deterministic event. No opinion/prediction quality penalty is applied:
`quality_adjustment` stays zero and the original score is preserved. The final
decision is evaluated again before optional delivery.

## Freshness and Redis state

Freshness is based on publication, never the data's reference period or fetch
time. Eastern release timestamps use `America/New_York`, including DST. Date-only
publication is conservatively interpreted as midnight Eastern and explicitly
marked `timestamp_precision="date"`. This may expire an event earlier than a
precise timestamp would. Missing dates, future timestamps, and releases older
than the cutoff are logged/counted and skipped before Redis or AI processing.
Exactly-at-cutoff events are eligible. Delivery rechecks freshness after analysis.

| Key | Role |
| --- | --- |
| `mias:macro:event:<id>` | Atomic processing lease, 900-second TTL |
| `mias:macro:processed:<id>` | Cached final event |
| `mias:macro:event:<id>:delivery` | Separate delivery lease, 900-second TTL |
| `mias:macro:delivered:<id>` | Confirmed Telegram success marker |

Lease ownership is checked atomically when writing the processing cache and when
releasing leases, so an expired worker cannot overwrite a newer worker's cache
or delete its lease. These scripts target the existing single-instance Redis
client; a Redis Cluster deployment would require a compatible key-slot design.
Cached results and delivered markers expire after the greater of
`DEDUP_TTL_SECONDS` and the freshness window, plus one second.

Non-delivery runs never write delivery success. A later delivery-enabled poll
uses the cached final event without repeating scoring or AI, including when the
original processing disabled AI. Failed sends release the delivery lease and
remain retryable while fresh. Retry discovery currently depends on successfully
fetching the current release again; there is no independent background outbox.

Macro processing stops on Redis failure instead of sending without trustworthy
state. This does not change the other collectors' Redis failure behavior.
No news near-duplicate logic or existing source namespaces are used.

## Validation and limits

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_fed_pipeline tests.test_macro_pipeline -v
```

All HTTP, Redis, OpenAI, Telegram, and dotenv access is mocked. The suite includes
the existing 31 Fed tests and mocked existing smoke scripts. Do not use unrestricted
test discovery: some older repository smoke scripts call live services on import.
See `tests/fixtures/macro/README.md` for fixture provenance.

The source review fetched BEA and Census successfully. **BLS Public Data API
ingestion is live-validated:** the API returned HTTP 200 with all nine required
series. **BLS RSS publication timing is blocked by HTTP 403 in this environment.**
RSS parsing uses synthetic fixtures, not a claimed successful live RSS poll.
**BLS events remain non-alerting when `published_at` cannot be verified.** API
observations alone do not establish a release publication timestamp.
**No unofficial source or anti-bot workaround is used.**

HTML layouts and correction wording may change. Unknown required structures are
reported as invalid rather than emitted as high-impact events. The collector polls
only the current release for each series, with no archive catch-up or scheduler.
An outage spanning a subsequent release can therefore miss the earlier release.

Telegram delivery cannot be exactly-once: ambiguous timeouts, process crashes
after a successful send, or Redis success-marker write failures can cause repeat
delivery. The notifier's existing retries are preserved. Leases reduce concurrent
duplicates but do not remove external-service uncertainty. AI timeouts/retries
remain those of the shared analyzer; a processing run that outlives its lease
cannot publish its result and will need a subsequent poll.

The standalone CLI prints counters and exits nonzero for fetch, invalid-release,
state, BLS numeric-data, or delivery errors. Analysis errors are counted but use the deterministic
fallback. The multi-source API retains its original four aggregate counters;
call `collect_macro_events()` to obtain macro-specific diagnostics.


## BLS machine-readable ingestion

The production BLS path uses the three RSS feeds above, followed by a JSON POST to
`https://api.bls.gov/publicAPI/v1/timeseries/data/`. It never requests BLS
`news.release/*.htm` pages. The older pure HTML normalizer remains available for
historical fixture compatibility but is not called by BLS collection; the HTML
fetcher explicitly rejects agency `bls`.

| Measure | SA series | NSA series |
| --- | --- | --- |
| Headline CPI-U, all items | `CUSR0000SA0` | `CUUR0000SA0` |
| Core CPI-U, all items less food and energy | `CUSR0000SA0L1E` | `CUUR0000SA0L1E` |
| PPI final demand | `WPSFD4` | `WPUFD4` |
| Total nonfarm payroll employment | `CES0000000001` | — |
| Civilian unemployment rate | `LNS14000000` | — |
| Average hourly earnings, all employees, total private | `CES0500000003` | — |

SA index series provide month-over-month changes; NSA indexes provide
12-month changes. Payroll levels are thousands of persons and the monthly
change is converted to persons. Unemployment changes are percentage points.
Earnings are nominal dollars per hour; percent changes are derived from API
observations. Computed index changes may differ slightly from published rounded
figures. Source footnotes (including preliminary/revision notices) are retained.

RSS item `pubDate` provides publication time. Neither channel `lastBuildDate`,
HTTP response dates, observation months, nor fetch times substitute for it.
The reference month comes from the RSS headline or release description. A month
without a year is resolved against publication year (including January/December
rollover), never against the current clock. Missing or ambiguous metadata is
rejected or skipped before numeric retrieval. The API cannot independently
establish publication freshness.

BLS identities use the existing versioned `macro_identity()` with release ID
`bls:<category>:<YYYY-MM>:initial`. They are independent of URL, cosmetic title,
RSS GUID, and numeric revisions. A feed title explicitly labeled Correction or
Corrected uses its publication timestamp as revision ID; if the original time
is unavailable, `original_published_at` is null rather than invented. Undesignated
API revisions do not create new releases. The old HTML adapter's USDL-based IDs
are not reused; this uncommitted macro collector has not been deployed. A future
migration from a deployed HTML collector would need dedup-state reconciliation.

One API batch per release includes all its required series and the reference year
plus the preceding year. Every current and comparison observation must exist for
the exact release period. Missing values, partial updates, duplicate observations,
API-level failures (even HTTP 200), warning messages, and malformed responses
prevent processing. Annual-average M13 observations are excluded. Responses served
as `text/plain` are accepted only if valid JSON with successful BLS status.

API enrichment occurs after freshness checks and reliable Redis lease acquisition,
before scoring or AI. Cached releases and delivery retries do not refetch the API.
Numeric failure leaves the release unprocessed and creates a four-hour cooldown
at `mias:macro:event:<id>:api-retry`; the next poll after expiry may retry while
fresh. The processing lease is released, and no delivered marker is consumed.
Redis failures still fail closed, including failure to establish retry state.
`data_errors` counts numeric failures and polls deferred by this cooldown.

API v1 needs no registration and supports 25 series/query, 10 years/query, and
25 queries/day. That covers nine series across these three release families when
requests are release-driven and retries are sparse. The cooldown limits routine
lag retries to at most six per release/day, but is not a global quota manager:
other applications sharing the same public IP or many correction versions can
still exhaust the quota. There are no automatic HTTP retries or API fallbacks.

Registered v2 offers 500 queries/day, 50 series/query, 20 years/query, calculated
changes and optional descriptive metadata. It would materially help frequent
polling or broader ingestion, but adds credential management and does not solve
publication timing or provide immutable release vintages. Phase 1 uses v1 only.
BLS documents possible API availability lag after publication; matching the
requested month prevents an old observation from being presented as a new release.
A prolonged lag, blocked RSS feed, or outage can still cause a missed 48-hour window.

Official references:
- https://www.bls.gov/feed/
- https://www.bls.gov/developers/api_signature.htm
- https://www.bls.gov/developers/api_faqs.htm
- https://www.bls.gov/developers/api_signature_v2.htm
- https://www.bls.gov/developers/api_release_notes.htm
