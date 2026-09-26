# Phase 7C v1 — Unified Evidence Packet

`evidence_packet/` assembles **independent, already-produced evidence** into one typed, immutable, deterministic packet
per symbol, at a caller-supplied information cutoff.

- It **transports** evidence. It never synthesizes, scores, recommends or interprets it.
- The packet can hold a −1.5-point relative return vs QQQ, a 5m `bullish_setup` and an ALERT news item side by side,
  and it concludes nothing.

`format_version = "phase7c-v1"`. It is a separate package from the frozen Phase 6 `evidence/` package.

## Components

| File | Role |
|---|---|
| `evidence_packet/models.py` | frozen dataclasses: `Availability`, `MarketContextSection`, `TechnicalTimeframeEvidence`, `TechnicalSection`, `NewsFacts`, `NewsRelevance`, `NewsUpstreamScore`, `NewsDelivery`, `NewsEvidenceItem`, `Exclusion`, `NewsSection`, `PacketProvenance`, `EvidencePacket`; inputs `NewsInput`, `NewsCollection` |
| `evidence_packet/adapters.py` | allow-list adapters: durable technical rows, RSS/SEC events, summary normalization |
| `evidence_packet/assembler.py` | **pure** `assemble(symbol, as_of, *, calendar, market_context=None, technical_rows=None, news=None)` |
| `evidence_packet/serialization.py` | deep freeze/thaw, canonical JSON, content-addressed ID |

No existing file was modified. There is no runner, no persistence and no migration.

## Zero I/O

Assembly performs **no** vendor, news, database, Redis or AI calls, does no file I/O and reads no clock. Tests enforce
this with sockets disabled, `open` and `make_engine` patched to fail, and the technical engine patched to fail.

The package never imports `collector`, `analyzer`, `alert_engine`, `shared.config` (which loads `.env`), `evidence`,
`evaluation`, `openai`, `redis`, `telegram`, `dotenv`, `feedparser` or `requests`; a subprocess test checks this.

It imports only these pure helpers:
- `market_context.models`;
- `persistence.technical_snapshot_repository` (`ROW_FIELDS`, `validate_row`, `content_hash`);
- `persistence.adapters.news` (`article_identity`, `article_url`, `publication`);
- `persistence.adapters.sec` (`filing_document`, `filing_date`, `EVENT_TYPE`);
- `market_data.calendar` and `market_data.models`.

## `as_of`: the information cutoff

`as_of` is supplied by the caller and must be timezone-aware. The packet serializes it in UTC.

| Domain | Rule | Violation |
|---|---|---|
| Market context | `context.as_of ≤ as_of` | `unavailable/after_as_of` (not carried) |
| Technical | `bar_end ≤ as_of`. `bar_end` is `ExchangeCalendar.bar_end` of the row's bar start and interval: the 1d close, the 1h bar truncated at the close, the 5m bar end | that timeframe is missing with `after_as_of` |
| News, second/minute precision | `published_at ≤ as_of` **and** `observed_at ≤ as_of` | `after_as_of` / `observed_after_as_of` |
| News, date-only (SEC filing date) | `publication_date` **strictly before** `as_of`'s America/New_York date, and `observed_at ≤ as_of`. A date is never converted to a timestamp or an assumed midnight | `after_as_of` / `observed_after_as_of` |
| News, unknown/unparseable/missing time | never included | `unknown_publication_time` |

## Market context

The typed `market_context.models.MarketContext` is carried as-is. It is never recomputed or mutated, and is serialized
only through its own canonical `to_dict()`. That preserves `context_format_version`, reason codes, benchmarks,
comparisons, freshness and provenance.

If no context is supplied, or it belongs to another symbol, the section is `unavailable` with `not_supplied` or
`symbol_mismatch`.

## Technical evidence

**Input:** `{interval: durable snapshot_row}`, in the existing `technical_snapshots` row schema. It can come from the
runner's shadow rows or from persisted rows in that JSON form.

For each of **`1d`, `1h`, `5m`**, emitted in exactly that order, the adapter:
1. copies only `ROW_FIELDS` (the existing `MATERIAL_FIELDS` plus `provider_delay_seconds` and `content_hash`) through
   a JSON round trip into a **private deep copy, frozen recursively** (read-only mappings and tuples), so the caller's
   dict can't change the packet later;
2. verifies the existing `content_hash` (`content_hash_mismatch`);
3. runs the existing `validate_row` (`invalid_row`);
4. checks the symbol and interval, and computes `bar_end`.

**Nothing is recomputed or reinterpreted:** RSI, EMA, ATR, VWAP, structure, levels, breakouts, state, confidence and
momentum are the row's own values.

**Availability:**
- all three present: `available`;
- some: `partial`, with reasons like `1h:not_supplied`;
- none: `unavailable`.

There is no combined technical verdict.

## News evidence (RSS and SEC only in v1)

**Input:** `NewsCollection(succeeded, inputs)`, where each input is `NewsInput(family, event, observed_at,
collector_outcome)`. `event` is the **post-decision** collector event (the same stage `news_shadow` persists).
`observed_at` is when MIAS observed it; the caller supplies it, because the event dict has no collection time.

### Identity

The existing durable identities are reused:

| Family | Identity | Publication |
|---|---|---|
| `news` (RSS) | `news-url-v1` = sha256 of the normalized URL (`article_identity`), or `news-fingerprint-v1` from the caller-supplied collector `news_fingerprint` when there is no http(s) URL | `publication()`: second precision (`rss_published`), or `unknown` (`no_feed_date`/`unparsed_feed_date`) |
| `sec` | `sec-v1` keyed by the caller-supplied collector `sec_fingerprint`, exactly as `persistence/adapters/sec.py` does; accession and URL validated with `filing_document` | `filing_date()`: **date precision** (`sec_submissions_filing_date`), or `unknown` |

### Structural separation

| Group | Fields | Meaning |
|---|---|---|
| `facts` | family, headline, normalized summary, source (feed), publisher, canonical URL, published_at or publication_date, timestamp_precision, publication_basis, SEC form and accession | source facts |
| `relevance` | symbols, direct_symbols, related_symbols (sorted), relevant | upstream keyword relevance |
| `upstream_score` | impact_score, impact_level, score_reasons (upstream order), original_impact_score, quality_adjustment | **recorded upstream heuristics, transported verbatim** |
| `delivery` | alert_decision, collector_outcome | **delivery policy, not a market fact**; nothing branches on it |

**Limitations of `upstream_score`:**
- The RSS scorer adds a recency bonus using the wall clock at scoring time.
- For ALERT items, the score may have been adjusted by the AI quality step (`original_impact_score` and
  `quality_adjustment` record that).

The packet carries the recorded values without reversing or reproducing either effect.

**AI fields (`ai_*`) are never serialized.** They are not used for identity, availability, ordering, filtering or
selection, and no LLM is called. Every key that isn't on the allow-list (API keys, headers, raw payloads, fingerprints,
database URLs, …) is ignored.

### Summary normalization

The source summary is kept as a bounded fact:
- HTML markup removed (`script`/`style` content dropped);
- entities decoded;
- whitespace collapsed and trimmed;
- **at most 1,000 Unicode characters**;
- missing or empty gives `None`.

It is never summarized, paraphrased, fetched or invented.

### Inclusion and exclusions

An item is included only if the packet symbol is **explicitly** in `event.symbols`. A multi-symbol event appears in
each such packet with the same identity. Benchmarks exist only inside MarketContext.

Each input gets exactly one counted reason: the first failing check in this order.
1. `unsupported_family` (fed, macro, treasury and geopolitical in v1)
2. `near_duplicate_suppressed`
3. `invalid_event`
4. `symbol_mismatch` (also covers market-wide events with no symbols)
5. `unknown_publication_time`
6. `after_as_of`
7. `observed_after_as_of`

Exclusions are serialized as `(reason, count)` sorted by reason. Nothing is dropped silently, and no exception text is
serialized.

**Market-wide evidence** is excluded in v1. Fed, macro, treasury and geopolitical events are never mapped to a stock.

### Dedupe and conflicts

Upstream dedupe (collector fingerprint and near-duplicate suppression) is trusted. There's no fuzzy matching and no
Redis. Within one assembly:
- **same identity, identical content** (ignoring `observed_at`): one record is kept, with the earliest `observed_at`;
  the rest count as `duplicate_identical`;
- **same identity, different content**: *all* records of that identity are excluded as `identity_conflict`. The
  assembler never picks one.

### Order

1. known publication before unknown;
2. newest America/New_York date first;
3. within a date, timestamped items (latest first), then date-only items;
4. then `family`, `identity_version`, `event_key` ascending.

### Availability

| Case | Status |
|---|---|
| no `NewsCollection` supplied | `unavailable` / `not_supplied` |
| `succeeded=False` | `unavailable` / `source_error` |
| succeeded with zero kept items | `available_empty` / `no_matching_items` (exclusions still counted) |
| at least one item | `available` |

An empty list never implies a successful collection. A packet in which every domain is unavailable is still valid.

## Identity and canonical serialization

- **Canonical JSON:** `sort_keys=True`, `separators=(",", ":")`, `allow_nan=False`.
  - Decimal: `format_decimal` (inside MarketContext).
  - Packet-level datetimes: UTC ISO.
  - Dates: ISO.
  - Floats as-is (technical rows, VWAP); `None` as null.
  - Tuples in fixed semantic order; reason sets sorted.
- **Packet ID:** `packet_id = "sha256:" + sha256(canonical JSON of the packet body without packet_id)`.
  - There is no wall-clock `generated_at`.
  - `format_version`, `context_format_version`, technical `engine_version` and the 7C news adapter label
    (`phase7c-news-adapter-v1`, this phase's own adapter label, not an upstream version) are all inside the hashed
    body.
  - Same evidence + same `as_of` + same versions = same `packet_id`.

## Provenance

- **Market:** inside MarketContext (provider per input, delay, bars supplied, adjusted, extended hours).
- **Technical:** inside each row (`source_provider`, `provider_delay_seconds`, `snapshot_timestamp`, `warmup_start`,
  `warmup_bars`, `engine_version`, `content_hash`), plus the computed `bar_end`.
- **News:** `source`, `publisher`, `canonical_url`, `publication_basis`, `timestamp_precision`, the identity scheme
  (`identity_version`) and `observed_at`.
- **Packet:** `format_version` and `provenance` (assembler, news adapter label, technical intervals, supported news
  families).
- No scorer, decision or collector versions are invented; RSS doesn't record any.

## Not in v1

- A runner or loader.
- Persistence or a migration.
- Market-wide evidence.
- Fed, macro, treasury and geopolitical families.
- AI fields.
- Options: no empty section. Phase 7D may add an `OptionsSection` under a new format version, and v1 semantics stay
  stable.
- Any synthesis (Phase 7E).
