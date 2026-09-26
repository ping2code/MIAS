# Phase 7A — Massive Stocks production adapter

Phase 7A adds an explicitly named provider, **`massive_stocks`**, backed by the Massive Stocks aggregates RANGE
endpoint on `https://api.massive.com`. The change surface is as small as possible, and nothing that Phase 6 depends on
changes.

## Provider identities

| `MARKET_DATA_PROVIDER` | Provider class | Default host | Status |
|---|---|---|---|
| `none` (default) | none; callers that need data refuse | none | unchanged |
| `polygon` | `PolygonProvider` | `https://api.polygon.io` | unchanged; **frozen Phase 6 identity** (`PROTOCOL.provider`) |
| `massive` | alias of `polygon` → `PolygonProvider` | `https://api.polygon.io` | unchanged; never repurposed |
| `massive_stocks` | `MassiveStocksProvider` | `https://api.massive.com` | **new** (Phase 7A) |

- No existing environment is migrated.
- `MARKET_DATA_BASE_URL` still overrides the host for either provider. It must be a plain `https://` origin, with no
  query string and no credentials.

## Architecture

`market_data/providers/massive.py`: `MassiveStocksProvider(PolygonProvider)`.

**Inherited unchanged from the frozen `PolygonProvider`:**

- the secure HTTP client (`market_data/http.py`);
- `get_bars` and `get_latest_bars`, with completed bars only;
- interval mapping and derivation;
- `_results` envelope and status checks: `OK`/`DELAYED`, and `results` must be a list;
- `_bar` field, type, Decimal and OHLC validation;
- the supported-session filter, `exclude_unsupported_sessions`;
- the calendar contract, `validate_calendar_series`.

**Re-implemented here:** only the pagination loop, `_fetch`, which calls the same validators in the same order.
- Its log line and diagnostics name `provider=massive_stocks`, instead of the parent's hard-coded `provider=polygon`.
- It records the `next_url` host for each page.
- It counts, but does not keep, the optional `vw` and `n` fields.
- Its vendor `limit` is a class attribute, `page_limit`, set to 50,000. Only the live check lowers it, to observe
  pagination.

**Not modified:**

- `market_data/providers/polygon.py`, `http.py`, `aggregation.py`, `completion.py`, `validation.py`, `calendar.py`;
- `models.py` (including `MarketBar`);
- `technical/*`, `evidence/*`, `evaluation/*`, `persistence/*`, `migrations/*`, `orchestrator/*`, `docs/phase6-*`;
- `evidence/prospective_start.json`.

### Intervals

| MIAS | Fetched as | Semantics |
|---|---|---|
| 5m, 30m (and 1m, 15m) | native minute ranges | clock grid; regular session only unless `MARKET_DATA_INCLUDE_EXTENDED_HOURS=true` |
| 1h | 30m, derived | session-anchored hours: 09:30, 10:30, …, with the last bucket truncated at the close |
| 1d | 30m, derived | regular session only, stamped at ET midnight. **Vendor daily bars are never used** |

- Bars starting before 04:00 ET or from 20:00 ET are excluded and counted.
- Early closes (13:00) follow the XNYS calendar.
- Timestamps are the vendor `t`, the bar start in integer milliseconds UTC, converted to America/New_York.

### VWAP (`vw`) and trade count (`n`)

These are **not exposed in Phase 7A**:

- `MarketBar` is frozen, because Phase 6 hashes depend on its OHLCV semantics;
- adding an extras structure would create a second, parallel data model.

The adapter only counts their presence, which appears in `diagnostics` and the live check. Derived 1h and 1d VWAP and
trade-count values are never computed. Exposing them is deferred to a later, separately approved change.

## Authentication and key hygiene

- The key comes only from `MARKET_DATA_API_KEY` in the process environment, never `.env`.
- It is sent only as the `Authorization: Bearer <key>` header.
- It never appears in URLs or query strings, logs, exceptions, `diagnostics`, `safe_view()`, `repr(settings)` or bar
  data.
- Pagination follows `next_url` only to the configured host over https.
- Redirects are errors.
- Error and log messages name the host and path only.

## Failure semantics (shared HTTP client)

| Condition | Result |
|---|---|
| 401 / 403 (not authorized or not entitled) | `ProviderError("auth")`, no retry |
| 429 | bounded retry; honours `Retry-After` (capped); fallback wait if the header is absent |
| 500/502/503/504, timeout, connection error | bounded retry, exponential backoff (`http` / `transport`) |
| Redirect, other 4xx, malformed JSON, unknown status, non-list `results` | no retry (`redirect` / `http` / `payload`) |
| Empty or missing `results` | `[]` (no bars) |
| Missing field, wrong type, bad OHLC, duplicate, out-of-order, off-grid, holiday or weekend bar | `MarketDataError`; never repaired |

## Phase 6 isolation and parity

- Phase 6 collects with `MARKET_DATA_PROVIDER=polygon` (or the `massive` alias). The `provider` value `"polygon"` is
  part of `PROTOCOL` (`REGISTRY_HASH`), each ledger `evidence_hash`, and each snapshot `content_hash`. Using
  `massive_stocks` for Phase 6 collection would therefore produce ledger and snapshot conflicts. A separate commit
  adds a fail-closed guard, described below.
- `tests/test_market_data_massive.py::PolygonParityTests` shows that for identical vendor payloads:
  - `PolygonProvider` and `MassiveStocksProvider` return **identical** `MarketBar`s for 5m, 30m, 1h and 1d;
  - every session has an identical Phase 6 `bar_content_hash`, including Thanksgiving and the 2026-11-27 half day;
  - malformed data raises identical errors;
  - the Polygon log line is unchanged.
- The frozen hashes are unchanged: registry `b1080c2e…`, protocol `15587aed…`, H1′ `d89efb30…`, H2′ `78e82413…`.
  `evidence/prospective_start.json` is unchanged and the migration head is still `0007`.

### Phase 6 provider guard (separate commit)

- `technical.runner` refuses evidence collection (`TECHNICAL_EVIDENCE_LEDGER_ENABLED=true`) when
  `MARKET_DATA_PROVIDER` is not the frozen `PROTOCOL.provider` (`polygon`, including the `massive` alias).
- It exits 2 with `reason=evidence_config` before any network request, like the other Phase 6 preflight checks.
- Behaviour with `polygon` is unchanged.

## User-run live contract check

This check is read-only and bounded:
- at most 20 requests, default 8, retries included;
- at most 5 sessions and one symbol;
- no database, ledger or Redis access.

It exercises:
- the **range** endpoint for 5m and 30m;
- a small-`limit` 5m pagination probe.

It reports metadata only:
- Bearer acceptance and status values;
- result counts and `next_url` hosts;
- `vw`/`n` presence counts;
- first and last bar timestamps;
- observed freshness, measured with the data delay forced to 0;
- HTTP status counts and rate-limit headers.

It prints no prices, no payloads and no key.

```
MARKET_DATA_PROVIDER=massive_stocks python -m market_data.massive_check --symbol META --days 2
```

The key must already be in the process environment as `MARKET_DATA_API_KEY`; the command never reads `.env`.

Exit codes:
- 0: PASSED;
- 1: FAILED (provider or data error, or a failed check);
- 2: NOT EXECUTED (configuration or usage error).
