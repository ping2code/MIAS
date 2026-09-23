# Official geopolitical collector

Implemented on `codex/geopolitical-collector`, based on main commit `a03206e`.
Independent opt-in ingestion; no existing collector is replaced or reconfigured.
Taiwan v1 covers MOEA economic/supply-chain releases only. It does not provide
comprehensive military/security coverage, or infer risk from Taiwan mentions.

## Live source contract validation

Read-only HTTP requests were made on September 22, 2026 America/Chicago (some
responses were already dated September 23 UTC). No dotenv, Redis, OpenAI or
Telegram client was used. The network sandbox initially denied DNS resolution;
authorized requests outside that sandbox succeeded. No browser impersonation,
unofficial mirror, or anti-bot bypass was used.

All nine discovery endpoints returned **200**, with no redirects:

| Source | Exact endpoint | Content-Type |
| --- | --- | --- |
| FR documents | https://www.federalregister.gov/api/v1/documents.json?per_page=2&order=newest | application/json; charset=utf-8 |
| FR public inspection | https://www.federalregister.gov/api/v1/public-inspection-documents.json | application/json; charset=utf-8 |
| FTC RSS | https://www.ftc.gov/feeds/press-release.xml | application/rss+xml; charset=utf-8 |
| MOEA RSS | https://www.moea.gov.tw/Mns/english/news/NewsRSSdetail.aspx?Kind=6 | text/xml |
| BIS | https://www.bis.gov/news-updates | text/html; charset=utf-8 |
| OFAC | https://ofac.treasury.gov/recent-actions | text/html; charset=UTF-8 |
| Treasury | https://home.treasury.gov/news/press-releases | text/html; charset=utf-8 |
| USTR | https://ustr.gov/about-us/policy-offices/press-office/press-releases | text/html; charset=UTF-8 |
| White House | https://www.whitehouse.gov/presidential-actions/ | text/html; charset=UTF-8 |

### Federal Register documents

The response has `description`, `count`, `total_pages`, `next_page_url`, and
`results`. A `per_page=2` request and a page-two request each returned two distinct
documents. Unfiltered metadata reported count 10,000 and 50 pages despite the
requested page size. The collector follows validated `next_page_url` values,
including cursor parameters; it does not calculate pages from that metadata.

The official OpenAPI contract at
https://www.federalregister.gov/api/v1/documentation.json specifies a default
page size of 20 and maximum of 1,000 for both document APIs. No guaranteed request
rate quota was established by this validation. The collector requests 100 rows,
uses a four-day discovery lookback, limits pages to 20 and bounds document work.

Stable document identifier example: `2026-19417`. Canonical document link:
https://www.federalregister.gov/documents/2026/09/22/2026-19417/restoring-american-saltwater-angling-and-recreation

Detail endpoint:
https://www.federalregister.gov/api/v1/documents/2026-19417.json

The detail has `publication_date`, `signing_date`, `effective_on`,
`executive_order_number`, docket fields, and linked text/XML/HTML/PDF URLs.
`publication_date` is date-only; signing/effective dates are not substituted for
public disclosure. Full content requires following a link; list abstracts are
not sufficient. The collector follows the official `raw_text_url`. It retains
source legal references without treating the informational API as the official
legal edition (the linked govinfo PDF is that edition).

### Federal Register public inspection

Observed count 100, five pages, 20 rows on the default page. Page two returned
different records. `next_page_url` uses the underscore path
`/api/v1/public_inspection_documents`; both documented hyphen and returned
underscore spellings are accepted for the same endpoint.

Example identifier `2026-19554`:
https://www.federalregister.gov/public-inspection/2026-19554/immigration-nonimmigrant-workers-restriction-on-entry-of-certain-proc-11069

`filed_at=2026-09-22T15:15:00Z` represents public inspection disclosure;
`publication_date=2026-09-23` is the separate scheduled publication date.
`pdf_updated_at` is not publication. Linked raw text or PDF is needed for full
content. The collector uses linked raw text and never infers an action from an
unread PDF. The document number joins public inspection to published versions.

### FTC RSS

Observed 10 items, no parser warnings, and no feed pagination link. Ten is an
observed rolling-window size, not a promised permanent service limit. The feed
requires frequent polling to avoid missed releases during bursts.

The first item had native GUID `337085` (`isPermaLink=false`), a canonical HTTPS
article link, and item `pubDate=Tue, 22 Sep 2026 08:00:00 -0400`. Feed summaries
are incomplete, so linked article HTML is required; legal attachments may supply
additional detail that this v1 does not claim to extract.

One article request with requests' default User-Agent returned 403. A subsequent
request using the collector's ordinary identifying User-Agent returned 200 and
parsed its body. Access failures remain per-document errors; no summary-only
alert fallback. RSS publication metadata is used conservatively, independently
of later article modification metadata. FTC case links supply cross-document
anchors when present; absent an authoritative anchor, alerts are withheld.

### Taiwan MOEA RSS

Observed 123 items, 624,863 bytes, no parser warnings and no pagination link.
Channel TTL was 60 minutes. No supported pagination parameter or guaranteed
retention window was established; the collector does not invent either.

Item identity is `news_id` in the canonical article URL, not `dc:identifier`
(which identified the publishing organization). Example:
https://www.moea.gov.tw/MNS/english/news/News.aspx?kind=6&menu_id=176&news_id=124028

Item `pubDate=Tue, 22 Sep 2026 08:00:00 GMT` corresponds to 16:00 Taipei.
The channel publication date is feed generation, not an event timestamp.
Descriptions contain announcement text; the sampled `content` field was shorter
than the description. The collector preserves the description as source text.
HTML/PDF attachments can contain further details; only facts present in the feed
body are eligible for this path. Routine export-order statistics are not treated
as geopolitical disruptions.

### HTML discovery details

BIS returned about 3.6 MB of rendered HTML with article links. Article body class
is `press-release-container`; a specific release h2 must be selected instead of
the agency banner heading. Date spans supply release dates.

OFAC supplies dated notice IDs, a source-specific body/date block, and a next-page
link. Treasury exposes year-sharded official press-release discovery already
supported by the existing pure Treasury source helper; the new collector reuses
that helper without changing it. USTR has release-specific dates in `ul.listing`
items even when article publication metadata is absent. Those dates are bound
only to the single release link in the same item. White House pages have
`article:published_time`, article content, and sometimes an `eo-NNNNN.pdf` link
that can join the corresponding FR executive-order metadata.

## Event and evidence contract

Existing MIAS schema fields are retained. Additional fields include agency,
document identity, resolved action identity, policy stage, legal status,
effective/scheduled dates, legal references, content-hash provenance, category,
matched scope and structured evidence. Event families remain separate:
`policy_action`, `sanctions_action`, `trade_action`, `regulatory_action`, and
`operational_disruption`.

Relevance requires an action and covered product/company scope in the same
clause. Generic China, Taiwan, AI, chip, semiconductor, sanctions or export-control
terms alone never suffice. Discovery keywords only reduce retrieval work; they
do not classify alerts. Each evidence record contains symbol, rule version,
matched entity/product/jurisdiction, policy scope, document ID, source URL, exact
quote and body offsets. Negated, hypothetical, historical, speech and ambiguous
multi-action content is conservatively excluded.

NVDA indirect relevance uses `nvda_advanced_compute_supply_v1`; META platform
relevance uses `meta_platform_data_scope_v1`. Explicit company action clauses
use `explicit_company_action_v1`. No generic AI action automatically tags META.

| Category | Importance |
| --- | ---: |
| Confirmed MOEA semiconductor production/supply interruption | 95 |
| Advanced-computing export controls | 90 |
| Entity list, sanctions, investment restrictions, trade restrictions | 85 |
| Regulatory remedy/order | 80 |
| Formal complaint (cap) | 75 |
| Proposed action (cap) | 60 |
| Unsupported/routine | 10 |

Importance is independent of sentiment. `quality_adjustment=0`. The existing
`evaluate_alert()` supplies decisions. Unresolved identity downgrades an ALERT
decision to DISPLAY_ONLY without misrepresenting its importance score.

## Identity and state

Instrument anchors include FR document numbers, executive-order numbers, OFAC
notice IDs, FTC case references and MOEA disruption release IDs. Current-action
links must be explicit; ambiguous links are not treated as equivalent. RIN/docket
numbers alone are not action identities. No headline similarity, embeddings or
AI is used for identity.

Identity includes family, substantive stage and revision marker. Aliases are
resolved atomically before scoring/AI/delivery. A document alias includes its
explicit instrument anchors, so reused URLs can identify new actions. Established
conflicting roots fail closed rather than being silently merged. `policy_id` is
the resolved action-version root, not a broad multi-stage proceeding identifier.
`event_id` is a versioned SHA-256 identity based on that root. Provenance and the
earliest known disclosure timestamp survive companion publications.

Synthetic fixture identity example:

```
BIS synthetic-chip-rule -> fr:2026-99901 <- FR 2026-99901
family=policy_action, stage=adopted, revision=original
policy_id=5b854e29c4acbfa15f60799a843fa708a9433c61a3ee4a3cae79d4fc858e8f1e
event_id=882e606082f6ca34394dc96c0c5501248c58ad45909abea38577f19b318bb796
score=90, decision=ALERT, related_symbols=[NVDA]
```

The test sends both documents through mocked processing/delivery and asserts
one analysis, one delivery, one duplicate, and two retained provenance records.
A separate test joins a White House EO PDF link to FR EO metadata. An amended
stage is distinct; cosmetic changes are not. Explicit new FR instruments on a
reused URL remain distinct.

Redis keys are exclusively under `mias:geopolitical:`:

- `event:{event_id}`: owner-token processing lease (900 seconds).
- `processed:{event_id}`: scored/enriched cache.
- `delivery:{event_id}`: separate owner-token delivery lease.
- `delivered:{event_id}`: confirmed Telegram message ID.
- `alias:{hash}`: instrument/document aliases.
- `policy:{policy_id}`: earliest known disclosure and provenance.

Default freshness is 48 hours (`GEOPOLITICAL_MAX_AGE_HOURS`). Date-only values
use conservative source-local start of day and record precision. Missing,
future or stale disclosures block processing; resolved older disclosure blocks
a fresh-looking companion. Effective dates, channel generation timestamps and
fetch times never substitute for disclosure. Delivery rechecks freshness.

Cache/delivered retention exceeds the freshness window independently of a
shorter general dedup TTL. Alias/policy retention defaults to 365 days
(`GEOPOLITICAL_ALIAS_TTL_DAYS`) and must exceed freshness. Redis failure prevents
AI/delivery. Dry runs do not consume delivery eligibility; failed deliveries can
retry from the cache without reanalysis. Owner-checked Lua prevents old workers
from overwriting cache or releasing a successor's lease.

## Invocation and regression coverage

`collect_geopolitical_events(enable_ai=True, send_alerts=False)` is the independent
entry point. `collect_all_sources(include_geopolitical=False, ...)` remains
opt-in and retains all existing defaults. AI import/client access is deferred
until a deterministic ALERT has a resolved identity. Only validated AI enrichment
is copied from a deep copy; parser-owned facts, identities, legal status, dates,
entities, stage and scores cannot be overwritten. Formatting includes official
evidence separately from AI commentary.

Tests mock HTTP, Redis, OpenAI, Telegram and dotenv:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest \
  tests.test_fed_pipeline \
  tests.test_macro_pipeline \
  tests.test_treasury_pipeline \
  tests.test_geopolitical_pipeline -v
```

190 tests passed (130 existing + 60 geopolitical). Synthetic fixtures are clearly
labeled and are not real government actions. No production Redis keys or live
AI/Telegram calls are involved. Lua behavior is covered with mocked Redis; an
actual Redis-server integration test was not run in this environment.

## Remaining reliability limits

- Conservative clause and current-instrument matching intentionally misses some
  relevant language. Multi-action notices need later explicit decomposition.
- Untagged or PDF-only actions do not receive inferred identities/content.
  There is no general PDF extraction or OCR fallback in this collector.
- Discovery caps, rolling RSS windows, unsupported HTML pagination, USTR node
  redirects, and layout/access changes can limit coverage. HTTP errors are not
  interpreted as empty successful releases. FR retrieval failure currently
  withholds that source batch; other sources continue.
- Identity cannot universally join all paraphrases, unlinked releases, or MOEA
  translations. Unresolved documents are withheld, and conflicting registered
  identities fail closed. Known anchors are tested, not a universal exactly-once
  guarantee. Cold starts lack previously observed disclosure history.
- Separate publications may disagree about action stage; deterministic parsing
  does not substitute for legal review. Unmarked substantive edits are not
  automatically interpreted as material corrections.
- AI enrichment can still contain inaccurate prose even though it cannot change
  parser fields. Official evidence remains visible and authoritative.
- A successful Telegram send followed by an uncertain acknowledgement or failed
  delivered-state write cannot be made exactly-once by Redis leases. Retry can
  duplicate in that crash window. The collector retains existing delivery API
  behavior and does not claim to eliminate it.
- Deduplication is internal to this collector. Existing news/other collector
  alert behavior is unchanged; cross-collector duplicate suppression would need
  a separately approved shared registry integration.
