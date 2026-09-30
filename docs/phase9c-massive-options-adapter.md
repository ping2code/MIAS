# Phase 9C — Massive options adapter and snapshot runner

**Status:** implemented on `claude/phase9c-massive-options-adapter`; not merged.
**Output:** OptionsSnapshot `phase9-snapshot-v1` (unchanged from Phase 9B).

> Phase 9C maps the live Massive options chain into the locked Phase 9B contract. It adds no interpretation,
> ranking, selection or trading semantics. OptionsIntelligence is Phase 9D.

## 1. Provider boundary

```
Massive API → options_data.massive → provider-neutral records → options_data.normalization (pure, Phase 9B)
            → OptionsSnapshot → canonical snapshot file (options_data.runner)
```

- **`options_data.provider`:** the `OptionsDataProvider` interface.
  - `get_chain(underlying, scope)` returns a `ChainResult`: the provider-neutral records plus provenance facts.
  - There is no `as_of`: retrieval is "what the source returns now", and the pure assembler applies the cutoff to
    every timed fact.
  - `ChainScope` holds collection bounds only. `FixtureOptionsProvider` serves records from memory for tests and
    replay.
- **`options_data.massive`:** the Massive translation. No Massive JSON shape crosses this boundary.
- **`options_data.runner`:** the only clock read, assembly, the atomic file write, and the summary.

Canonicalization, identity, cutoff exclusion, hashing and validation all stay in the pure Phase 9B layer.

## 2. Pre-implementation regular-session check (Phase 9A tool, META, 2026-09-30 14:30 UTC)

- **Newest `day.last_updated`:** 27 s old, so day data is current-session and fresh. The feed as a whole is
  **not** classified as real-time from this alone.
- **Entitled:** the chain snapshot, contract snapshot, reference contracts and as-of reference contracts.
- **Not entitled (403):** quotes, historical quotes and trades.
- **IV and Greeks:** present in 95 of 100 sampled contracts, still with no timestamp.
- **Open interest:** present in 100 of 100, with no date.
- **Pagination:** unchanged: HTTPS on `api.massive.com`, a cursor `next_url`, header authorization, and no key
  embedded in the link.

## 3. Massive mapping

**Endpoint:** `GET /v3/snapshot/options/{underlying}?limit=N[&contract_type=][&expiration_date.gte=][&expiration_date.lte=]`.

| Massive field | Phase 9B |
|---|---|
| `details.ticker`, `contract_type`, `expiration_date`, `strike_price` | Identity cross-check fields (the symbol is parsed and cross-checked by the pure layer) |
| `underlying_asset.ticker` | `underlying` cross-check |
| `details.exercise_style`, `shares_per_contract`, `additional_underlyings` (if present) | `terms` |
| `day.{open, high, low, close, previous_close, change, change_percent, volume, vwap}` | `day`; `observed_at` from `day.last_updated` (epoch ns) when valid, so `time_basis = observed_at` |
| `open_interest` | `open_interest.value`, `provider_snapshot_unverified`; no date is invented |
| `implied_volatility` | `implied_volatility.value`, `provider_snapshot_unverified` |
| `greeks.{delta, gamma, theta, vega, rho}` | `greeks` (rho only if present; never observed), `provider_snapshot_unverified` |
| `last_quote` / `last_trade` | `quote` / `trade` **only when returned** |

- **Parsing:** JSON numbers are parsed as `Decimal` by the secure client, so there are no floats.
- **Absent fields:** a field the source doesn't return is absent; an empty `day` block is `missing`.
- **Malformed items:** an item that can't be mapped becomes a record the pure layer excludes as
  `malformed_record`.

## 4. Quote and trade: entitlement limitation

On the current plan, dedicated quotes and trades return 403, and the chain carries no `last_quote` or
`last_trade`. So `quote` and `trade` are declared **unavailable** (`provenance.source_capabilities`) whenever no
returned contract carries them.

- If a later plan returns them for some contracts, the group becomes available, and contracts without them are
  `missing`.
- No bid or ask is derived from day fields, and no last trade from `day.close`.

## 5. IV, Greeks, open interest and day timing

- **IV, Greeks and open interest:** no timestamp or date was ever observed, so they are
  `provider_snapshot_unverified`, which means **not proven cutoff-safe**.
- **Day:** `day.observed_at` is each contract's own `last_updated`. The `day` block is the contract's **most recent
  day record**, not necessarily today's (§12). Consumers must use `observed_at`, not assume the current session.

## 6. Underlying price: none invented

The chain's `underlying_asset` returned only a ticker, so the price is `unavailable` with the reason
`not_supplied`.

- If a price field ever appears, it is still not used in v1 (`not_used_in_v1`) until verified.
- No second stock-price call is made. `MARKET_DATA_*` and the Phase 6 provider code are untouched.
- Phase 9D must treat strike relationships as unavailable when the price is unavailable.

## 7. Configuration and delay

Only `OPTIONS_DATA_*` is read; a test records every environment read. The key is required, and is sent only as
the Bearer header.

| Setting | Runner use |
|---|---|
| `OPTIONS_DATA_DELAY_SECONDS` | `as_of = checked_at − delay`. **Unset means no delay is assumed:** `as_of = checked_at` and `configured_delay_seconds = null`. The stock 900 s delay is never inherited |
| `OPTIONS_DATA_MIN_REQUEST_INTERVAL_SECONDS` | Pacing (default 1.0 s) |
| `OPTIONS_DATA_MAX_PAGES` | Default page cap (1–5); `--max-pages` (1–200) overrides it for a run |

Facts timed after `as_of` are still excluded by the pure layer. With a configured delay, fresh day records become
`excluded_after_as_of`, which is visible and counted. The options delay itself remains unverified: day timestamps
alone don't prove real-time quotes.

## 8. Pagination and truncation

- **Stopping:** pages are followed until the source ends, `--max-pages` is reached, or the request budget runs
  out.
- **Link checks:** `next_url` must be HTTPS on the configured host, otherwise the run fails (`pagination`). It is
  followed exactly as given, with header authorization only.
- **Truncation:** stopping on the page or request limit while more remains sets `provenance.truncated = true`,
  which is part of the snapshot identity. A partial chain never looks complete.
- **Failures:** any other provider error fails the run: authentication or entitlement, HTTP, transport, malformed
  payloads, or a budget exhausted before any page. Nothing is written.
- **Request budget:** defaults to `--max-pages × 3` (retries included), at most 250.

## 9. Scope

`--contract-type call|put|all`, `--expiration-from`, `--expiration-through`, `--page-limit` (results per page,
1–250) and `--max-pages` are **collection bounds only**.

- They are sent as provider filters and recorded in `scope`, which is part of the snapshot identity.
- If any returned contract falls outside the requested type or expiration range, the run fails (`scope`). A
  snapshot never claims a scope it doesn't match.
- There are no preference filters of any kind: expiry, strike, days to expiration, delta, moneyness, liquidity or
  IV.

## 10. Runner CLI and output file

```bash
OPTIONS_DATA_PROVIDER=massive OPTIONS_DATA_API_KEY=... python -m options_data.runner --symbol META --output /tmp/META-options-snapshot.json [--contract-type all] [--expiration-from YYYY-MM-DD] [--expiration-through YYYY-MM-DD] [--page-limit 250] [--max-pages N] [--max-requests N] [--overwrite]
```

**Output file:**
- the canonical snapshot JSON plus a newline;
- written to a temporary file in the target directory, then fsync'd;
- then placed with a **no-clobber** hard link (an existing file fails the run with exit 3, and nothing is fetched),
  or replaced atomically with `--overwrite`;
- a failed write leaves no partial file.

**Stdout:** a compact metadata summary. It has no chain data, keys, URLs or cursors.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Written |
| 1 | Provider, data or assembly failure |
| 2 | Configuration or usage error (nothing fetched) |
| 3 | The output exists or cannot be written |

## 11. Security

- The key is only ever the `Authorization: Bearer` header. It never appears in URLs, the file, the summary, errors
  or logs, and tests check all of these with a planted key.
- The HTTP client is `market_data.http.JsonHttpClient`, reused unchanged: timeouts, retries, pacing, no redirects,
  a request budget, and host-only errors.
- No raw payloads, full URLs, cursors or headers are stored.

## 12. Live results (2026-09-30, regular session, no configured delay)

| | META | NVDA |
|---|---|---|
| `snapshot_id` | `sha256:6d632856…c6f9b579` | `sha256:f82658c3…d5204a9a` |
| `as_of` | 14:38:18Z | 14:39:10Z |
| Contracts (expirations) | 7,780 (23, 2026-09-30 → 2029-01-19) | 3,886 (24) |
| Exclusions | none | none |
| Pages, requests | 32, 32 | 16, 16 |
| Truncated | no (complete chain) | no |
| Day present / missing | 5,504 / 2,276 | 3,313 / 573 |
| IV and Greeks present / missing | 6,540 / 1,240 | 3,604 / 282 |
| Open interest present | 7,780 (all) | 3,886 (all) |
| Quote, trade | unavailable (all) | unavailable (all) |
| rho, adjusted roots, non-100 deliverables | none observed | none observed |
| Underlying price | unavailable (`not_supplied`) | unavailable (`not_supplied`) |
| File size | 9.9 MB | 5.0 MB |
| Fetch / assemble+validate / write | 31.2 s / 4.5 s / 0.02 s | 15.2 s / 2.3 s / 0.01 s |

Both files re-validate against `phase9-snapshot-v1`.

**Regular-session timing finding:**
- **Newest day record:** 15 s old for META and 6 s for NVDA, so `current_session`.
- **But most records are older:** only 1,754 of 5,504 META day records (1,365 of 3,313 NVDA) are from the current
  session. The oldest go back to 2025-01-06 (META) and 2024-05-30 (NVDA).
- **What `day` means:** the most recent day with activity for that contract.
- **Not a real-time claim:** this does not establish real-time options data. Quote and trade delay stay
  unavailable on this plan.

**Performance:** fetching is dominated by the 1 s pacing (about 1 s per page). Assembly and validation run at
roughly 0.6 ms per contract.

## 13. Replay model and persistence

- **Replay:** the snapshot file is the replay artifact. Loading it and running `validated_snapshot` reproduces
  the same `snapshot_id`. The same normalized records always give the same bytes, whatever the page or record
  order.
- **Fixture replay:** `FixtureOptionsProvider` replays records without the network.
- **Persistence:** none: no database, migration, scheduler or AI.

## 14. Phase 6 safeguards

- No read of `evidence/`, `evaluation/`, the ledger, forward returns or outcomes.
- `MARKET_DATA_*`, the Phase 6 provider settings, the frozen hashes and the migration head
  (`0007_technical_evidence_ledger`) are unchanged.

## 15. Phase 9D boundary

Phase 9D (OptionsIntelligence) will consume these snapshots. It must:
- treat `provider_snapshot_unverified` facts as not proven cutoff-safe;
- use each `day.observed_at` rather than assuming the current session;
- treat strike relationships as unavailable while the underlying price is unavailable.

Phase 9C computes nothing of Phase 9D's: no mid, spread, days to expiration, moneyness, summaries, flags or
selection.
