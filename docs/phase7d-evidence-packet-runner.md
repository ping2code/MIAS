# Phase 7D — Evidence Packet Loader and Runner

## 1. Purpose

Phase 7D builds one Phase 7C `EvidencePacket` (`phase7c-v1`, unchanged) from **durable MIAS data**, for an explicit
symbol and information cutoff `as_of`, and emits deterministic JSON.

- It is **read-only** and **zero-network**.
- It never interprets the packet: no scores, confidence, direction, sentiment, recommendations or options.
- It never persists packets, adds tables or migrations, fetches market or news data, calls AI, or delivers alerts.

## 2. Architecture

| Module | Role | I/O |
|---|---|---|
| `evidence_packet/models.py`, `adapters.py`, `assembler.py`, `serialization.py` | Phase 7C, **unchanged and pure** | none |
| `evidence_packet/loader.py` | MarketContext file → typed object; read-only DB queries → Phase 7C inputs | the MarketContext file and PostgreSQL reads |
| `evidence_packet/runner.py` | CLI, strict/partial policy, output, diagnostics, exit codes | stdout/stderr and the requested output file |

**Data flow:**

1. `--market-context` file → `load_market_context` → typed `MarketContext`.
2. In **one** read-only transaction:
   - `technical_snapshots` → `load_technical` → `{interval: snapshot_row}`;
   - `events`, `event_versions`, `event_history`, `event_provenance` → `load_events` → `NewsInput` tuple.
3. The Phase 7C `assemble(...)` builds the packet; `packet_id` and canonical JSON come from Phase 7C.
4. The runner writes the packet to stdout or `--output`, and diagnostics to stderr.

## 3. The MarketContext-file boundary

MarketContext is not persisted, and MIAS stores **no raw bars**, so market context comes from an explicit file produced
by the existing Phase 7B runner. That's the only place market data is fetched.

```
python -m market_context.runner --symbols META,NVDA --out ctx.json
python -m evidence_packet.runner --symbol META --as-of 2026-09-29T13:00:00Z --market-context ctx.json
```

- **Accepted input:** the runner output (a `contexts` list; exactly one entry must match `--symbol`) or a single
  `MarketContext.to_dict()` object.
- **Rebuild:** the typed `market_context.models.MarketContext` is rebuilt field by field with exact types (Decimal,
  aware datetimes, dates, floats, tuples).
- **Round-trip check:** the rebuilt `to_dict()` must **equal the file content exactly**; otherwise it's an integrity
  failure. Unknown or missing fields and a wrong `context_format_version` are rejected.
- **Consistency checks:** the context `symbol` must equal `--symbol`, and both its `now` and `as_of` must be at or
  before the packet `as_of`. Nothing later than the cutoff may enter the packet.
- `market_context/` is not modified, and nothing is persisted.

## 4. Database read path

- **Configuration:** the existing MIAS configuration: `DatabaseSettings.from_env` (`DATABASE_URL`, never `.env`) and
  `make_engine`.
- **Transaction:** a single transaction through the existing `persistence.news_audit.read_only`. On PostgreSQL that
  runs `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`, a consistent snapshot in which the database
  itself rejects writes.
- **Reads:**
  - technical: the existing `TechnicalSnapshotRepository.snapshots`;
  - events: read-only SELECTs in `loader.py` over the existing table metadata (`persistence.models`), because
    `EventRepository` has no time-window listing.
- No `persistence/` code or schema changed.

## 5. `as_of` semantics

`--as-of` is mandatory:
- ISO 8601 with a timezone (`Z` accepted);
- normalized to UTC;
- naive values rejected;
- no clock reads and no implicit "latest".

The cutoff is **inclusive**: data known at or before `as_of` is eligible.

| Source | Eligible when |
|---|---|
| MarketContext | `context.now ≤ as_of` and `context.as_of ≤ as_of` |
| Technical snapshot | `ExchangeCalendar.bar_end ≤ as_of` **and** `created_at ≤ as_of` (recorded by then) |
| Event version | `observed_at ≤ as_of`; the **latest such version** is used, never `current_version_id` |
| Score/decision history | `recorded_at ≤ as_of` (latest per kind); AI history is never read |
| Provenance | `retrieved_at ≤ as_of` (latest) |
| Publication | Phase 7C rules: `published_at ≤ as_of`; a date-only filing strictly before the `as_of` New York date; unknown time excluded |

## 6. Technical selection

For `1d`, `1h` and `5m` (in that order), with `engine_version = phase4c-v2`:

- **Query:** the repository's range query on `snapshot_timestamp < as_of`. That bound is exclusive on the bar
  *start*, and every bar ending at or before `as_of` starts before it, so the public semantics stay inclusive.
- **Eligibility:** keep completed rows with `created_at ≤ as_of` and `bar_end ≤ as_of`.
- **Choice:** the latest eligible bar.
- **Conversion:** the existing `row_from_db` gives the hashed JSON form. The Phase 7C adapter then verifies the
  content hash, vocabularies, symbol and interval.
- A stored row that fails verification is an **integrity failure (exit 4)**, never skipped.
- Nothing is recomputed.

## 7. News and SEC reconstruction

**Candidates:** versions of `news`/`sec` events with `observed_at ∈ (as_of − lookback, as_of]`. An item is only ever
observed after it's published, so this bounded superset contains every in-window item. Per event, the latest candidate
version is used.

**The collector event** that Phase 7C adapts is rebuilt only from rows available by the cutoff:

| Field | Source |
|---|---|
| headline, summary, publisher | the version |
| feed, original `published_at` string, collector fingerprint, canonical URL | provenance |
| symbols, direct/related, relevant; SEC form and accession | version attributes |
| score fields, `alert_decision`, `collector_outcome` | the latest score/decision history |

**Identity** must reproduce the durable one, or it's an integrity failure (exit 4):
- RSS: `news-url-v1`, or the `news-fingerprint-v1` fallback, via the existing `article_identity`;
- SEC: `sec-v1` = the stored fingerprint key. Accession and URL validation stay in the Phase 7C adapter.

**Excluded before assembly** and counted in diagnostics only (the packet contract is unchanged):
- items whose known publication is at or before the window start (`outside_lookback`);
- versions without provenance by the cutoff (`no_provenance`).

Everything else is passed to Phase 7C, which applies and counts its own exclusions: `symbol_mismatch`,
`after_as_of`, `observed_after_as_of`, `unknown_publication_time`, `near_duplicate_suppressed`, `invalid_event`,
`duplicate_identical`, `identity_conflict`.

## 8. Lookback

- `--news-lookback-hours`: default **72**, allowed range 1–720.
- The window is `(as_of − lookback, as_of]`, computed only from `as_of`.
- A date-only SEC filing is in the window if its date is on or after the window start's New York date.

## 9. Deterministic ordering

- Technical: `1d`, `1h`, `5m`.
- Loaded news/SEC inputs: `(family, identity_version, event_key)`; the packet then applies the Phase 7C news order.
- Version, history and provenance choices use explicit keys: `(observed_at, recorded_at, id)`,
  `(recorded_at, content_hash)` and `(retrieved_at, provenance_key)`.
- Database row order is never relied on.

## 10. CLI

```
python -m evidence_packet.runner --symbol META --as-of 2026-09-29T13:00:00Z --market-context ctx.json \
  [--news-lookback-hours 72] [--allow-partial] [--pretty] [--output FILE] [--dry-run]
```

- **Output:** canonical Phase 7C JSON to stdout, UTF-8, with one trailing newline.
- **`--pretty`:** indented presentation of the identical content. `packet_id` is unchanged, because it's computed from
  the canonical body.
- **`--output FILE`:** writes only that file.
- **`--dry-run`:** prints the plan (symbol, `as_of`, MarketContext file status, intervals, lookback, durable
  sources, packet format) with **no database or network access**.

## 11. Diagnostics (stderr, one JSON object)

- **Success:** `{"diagnostics": {...}}` containing:
  - per-interval technical `found`/`missing` status (with `snapshot_timestamp`, `bar_end` and candidate counts);
  - MarketContext `validated`/`not_supplied`/`missing_file`;
  - per-family `loaded`, `outside_lookback`, `no_provenance`, `passed` and `included`;
  - the Phase 7C exclusion counts, domain availability and `packet_id`.
- **Failure:** `{"error": ..., "exit_code": N}`. For source-incomplete failures it also includes the full
  diagnostics, since every source is loaded before the strict check.
- Diagnostics never contain credentials, database URLs, raw bars, payloads or `ai_*` fields.

## 12. Exit codes

| Code | Meaning |
|---|---|
| 0 | packet built (or dry run) |
| 2 | invalid CLI or input: bad or naive `--as-of`, invalid symbol, lookback out of range, unreadable or inconsistent MarketContext file (symbol, or later than `as_of`) |
| 3 | source incomplete (strict): MarketContext missing, or any technical interval missing |
| 4 | integrity failure: a stored snapshot fails hash or vocabulary verification, the MarketContext file doesn't round-trip, or an event doesn't reproduce its durable identity |
| 5 | database not configured, or a read failure |

## 13. Partial mode

`--allow-partial` is never the default. A missing MarketContext or missing intervals are then carried as Phase 7C
`unavailable/not_supplied`, and diagnostics list `missing_intervals`. The `packet_id` changes naturally. No placeholder
data is ever invented. Integrity failures are never downgraded.

## 14. Read-only guarantee

- The runner executes only `SELECT` (plus the PostgreSQL `SET TRANSACTION … READ ONLY`).
- On PostgreSQL the transaction itself rejects writes.
- Tests capture every SQL statement (SQLite and a disposable PostgreSQL 16) and assert there are no writes and that
  table counts are unchanged.

## 15. Zero-network guarantee

The only I/O is PostgreSQL, the MarketContext file, the requested output file and stdout/stderr.
- The runner never imports `market_data.providers`, `market_data.http`, `requests`, `redis`, `openai`, `telegram`,
  collectors, analyzers or `shared.config` (a subprocess test checks this).
- Tests disable Python sockets.
- There are no provider, news, SEC, Fed, Treasury, OpenAI or Telegram calls.

## 16. Reproducibility and replay

The same database contents, symbol, `as_of`, lookback, MarketContext file and code give **byte-identical** output and
the same `packet_id` (tested on SQLite and PostgreSQL).

**Replay:** running with an earlier `as_of` uses only what MIAS knew then:
- earlier technical rows;
- earlier event versions, even when `current_version_id` has moved on;
- no history recorded after the cutoff.

Data recorded later can't change an earlier packet (tested byte-for-byte).

## 17. Limitations

- MarketContext isn't replayable from MIAS itself: it's only as reproducible as the supplied file (Phase 7B fetches
  live data).
- Technical, news and SEC evidence exist only where shadow persistence was enabled. A newly bootstrapped database gives
  `source incomplete` in strict mode.
- RSS relevance covers the collector watchlist (currently META/NVDA). SEC covers the configured symbols.
- Upstream news scores may depend on scoring time or an AI adjustment. They're transported as recorded (see Phase 7C).
- Candidate selection relies on `observed_at ≥ published_at`. An item published inside the window but observed before
  it would not be seen.

## 18. Deferred

- Packet persistence.
- Market-wide families (Fed, macro, Treasury, geopolitical).
- Options evidence.
- Scheduler integration.
- Any interpretation or synthesis (Phase 7E).
