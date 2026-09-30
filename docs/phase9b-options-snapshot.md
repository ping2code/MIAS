# Phase 9B — OptionsSnapshot (pure, provider-neutral)

**Status:** implemented on `claude/phase9b-options-snapshot`; not merged.
**Format:** `phase9-snapshot-v1`

> An OptionsSnapshot records **source facts only**: what a provider reported at or before `as_of`, normalized and
> validated. It contains no interpretation, and no trading, ranking or selection semantics.

## 1. Purpose and scope

`options_data.normalization.assemble(...)` turns explicit, provider-neutral source records into one immutable,
content-addressed, cutoff-aware OptionsSnapshot. `options_data.validation.validated_snapshot(...)` re-checks a
sealed snapshot and fails closed.

Phase 9B is pure: no network, files, environment, clock, database or AI.

Not in 9B:
- the live Massive adapter and runner (Phase 9C);
- OptionsIntelligence (Phase 9D);
- persistence and migrations;
- Phase 10.

## 2. Live Phase 9A evidence that shaped the schema

The META and NVDA contract checks (pre-market, bounded samples of 100 contracts in 2 pages) proved the following.

**Entitled:**
- the chain snapshot;
- the contract reference list, including as-of;
- the single-contract snapshot.

**Not entitled (HTTP 403):**
- dedicated quotes;
- historical quotes;
- dedicated trades.

**The chain snapshot carries:**
- contract details: type, exercise style, expiration, shares per contract, strike;
- day OHLC, change, volume and VWAP;
- open interest;
- IV and provider Greeks, when available.

**Not observed:**
- any timestamp on IV or Greeks;
- a date on open interest;
- rho;
- adjusted or non-standard contracts;
- an underlying price as a proven chain field.

The real-time vs delayed status is unverified.

**What follows from this:**
- `quote` and `trade` are optional groups that a snapshot can declare `unavailable`;
- `day` is first-class;
- IV, Greeks and open interest default to `provider_snapshot_unverified`;
- `rho` is optional;
- the underlying price may be unavailable;
- adjusted contracts are supported by identity, even though none were observed.

## 3. Snapshot schema (10 top-level fields, pinned)

| Field | Content |
|---|---|
| `snapshot_format_version` | `phase9-snapshot-v1` |
| `snapshot_id` | `sha256:` + SHA-256 of the canonical body without `snapshot_id` |
| `underlying` | Ticker (`market_data.models.SYMBOL`); any optionable symbol |
| `as_of` | Explicit caller input, stored as canonical UTC ISO 8601 |
| `session` | `session_date` (the `as_of` America/New_York date) and `calendar_state` (`regular`, `pre`, `post`, `closed` or null) |
| `underlying_price` | `status` (`present`/`unavailable`), `value`, `observed_at`, `source`, `reason` |
| `scope` | `contract_types`, `expiration_from`, `expiration_through`, `provider_page_limit`, `provider_result_limit`: what was requested. Provenance, never a trading filter |
| `contracts` | Sorted by (expiration, option type, strike as a number, `contract_id`) |
| `exclusions` | `[{reason, count}]`, sorted |
| `provenance` | `provider`, `adapter_version`, `endpoint_families`, `configured_delay_seconds`, `pages_fetched`, `requests_made`, `truncated`, `records_received`, `source_capabilities` (per fact group: available or unavailable). No keys, headers or URLs |

There is no `generated_at`. Every number is a canonical Decimal string (`market_data.models.format_decimal`):
never a float, never NaN.

## 4. Contract schema

| Group | Fields |
|---|---|
| `identity` | `contract_id`, `provider_symbol`, `root`, `root_matches_underlying`, `option_type` (`call`/`put`), `expiration`, `strike` |
| `terms` | `exercise_style` (`american`/`european`/`bermudan` or null), `shares_per_contract`, `deliverables` (`[{kind, symbol, amount}]`, only if supplied) |
| `quote` | `status`, `bid`, `ask`, `bid_size`, `ask_size`, `observed_at`, `time_basis` |
| `trade` | `status`, `price`, `size`, `observed_at`, `time_basis` |
| `day` | `status`, `open`, `high`, `low`, `close`, `previous_close`, `change`, `change_percent`, `volume`, `vwap`, `observed_at`, `time_basis` |
| `open_interest` | `status`, `value`, `as_of_date`, `observed_at`, `time_basis` |
| `implied_volatility` | `status`, `value`, `observed_at`, `time_basis`, `source` |
| `greeks` | `status`, `delta`, `gamma`, `theta`, `vega`, `rho`, `observed_at`, `time_basis`, `source` |

Compared with your outline:
- `root` and `root_matches_underlying` are added to `identity`, to keep adjusted contracts traceable;
- `observed_at` is added to `day`, `open_interest`, `implied_volatility` and `greeks`, so the `observed_at` time
  basis always has its instant.

## 5. Contract identity

- **`contract_id`** is `ROOT + YYMMDD + C|P + strike × 1000` as 8 digits, e.g. `META261016C00700000`. It is derived
  from the provider's OCC-style symbol (an `O:` prefix is accepted), and `provider_symbol` is kept verbatim.
- **Cross-checks:** the stated `option_type`, `expiration`, `strike` and `underlying` must agree with the symbol.
  A disagreement is an `identity_mismatch`.
- **The OCC root is part of the identity.** (underlying, expiration, type, strike) is not unique once adjusted
  contracts exist: a `META1` root with a 50-share deliverable is a separate contract, marked
  `root_matches_underlying: false`.
- **Duplicates:**
  - identical copies keep one, counted as `duplicate_identical`;
  - copies with conflicting facts are all excluded, counted as `identity_conflict`.
- No list position is ever used.

## 6. Zero, missing and unavailable

Every fact group has a closed `status`:

| Status | Meaning |
|---|---|
| `present` | At least one value was supplied. Zero (e.g. volume `"0"`) is present |
| `missing` | The source omitted the group, or supplied it with no values |
| `unavailable` | The source or entitlement cannot provide it (`unavailable_groups`, recorded in `provenance.source_capabilities`) |
| `excluded_after_as_of` | Supplied, but observed after the cutoff |

A group that isn't `present` carries no values. Crossed (ask < bid), locked (ask = bid), partial and zero-bid
quotes are kept exactly as reported. The snapshot never computes mid, spread or a quote state, and never
substitutes the last trade for a quote.

## 7. Time basis

| `time_basis` | When |
|---|---|
| `observed_at` | The source supplied an instant, at or before `as_of` (stored in `observed_at`) |
| `provider_as_of_date` | Open interest with a source date, on or before the `as_of` exchange date (stored in `as_of_date`) |
| `provider_snapshot_unverified` | Part of the provider snapshot, with no trustworthy time of its own. **Not proven cutoff-safe** |
| `unavailable` | The group is not present |

No time or date is ever invented. Under the Phase 9A evidence, IV, Greeks, open interest and untimed day facts
are `provider_snapshot_unverified`.

## 8. Cutoff policy

- `as_of` is explicit caller input; the pure layer never reads the clock.
- A group observed after `as_of`, or open interest dated after the `as_of` exchange date, becomes
  `excluded_after_as_of`. Only that group is dropped: the contract's identity and terms stay. Each such group
  counts once as `fact_after_as_of`. A fact observed exactly at `as_of` is included.
- Contracts expiring before the `as_of` exchange date are excluded as `expired_before_as_of`. Expiring *on* that
  date is kept.
- An underlying price observed after `as_of` becomes unavailable with the reason `observed_after_as_of`.
- Days to expiration are not computed here; that belongs to OptionsIntelligence.

## 9. Open interest, IV, Greeks, day, quote and trade

- **Open interest:** a non-negative whole number. Its time basis follows exactly what the source supplied, and
  nothing is invented:

  | Source supplied | `time_basis` | `as_of_date` | `observed_at` |
  |---|---|---|---|
  | No date or time (the Phase 9A live case) | `provider_snapshot_unverified` | null (required) | null |
  | A date | `provider_as_of_date` | the date (required) | null |
  | A timestamp | `observed_at` | null | the timestamp |
  | A timestamp and a date | `observed_at` | the date (kept, not invented) | the timestamp |

  Validation rejects `provider_as_of_date` without a date, and `provider_snapshot_unverified` with one. A date after
  the `as_of` exchange date excludes the group (`excluded_after_as_of`).
- **IV:** copied from the provider (`source: "provider"`), finite and non-negative, never recomputed. There is no
  percentile, rank, or cheap or expensive label.
- **Greeks:** copied from the provider. `rho` is optional. Mathematically surprising values (e.g. delta 1.7) are
  kept as source facts; bound checks belong to OptionsIntelligence. There is no in-house model.
- **Day:** OHLC, previous close and VWAP are non-negative; change and change percent may be negative; volume is a
  non-negative whole number. Nothing is interpreted.
- **Quote and trade:** optional. Prices, sizes and volumes are non-negative, and sizes are whole numbers.

## 10. Exclusions

Record-level reasons are checked in a fixed order:
1. identity problems: `malformed_record`, `unsupported_option_type`, `invalid_strike`, `identity_mismatch`;
2. terms and values: `malformed_record`;
3. `expired_before_as_of`;
4. then `duplicate_identical` and `identity_conflict`.

The group-level reason is `fact_after_as_of`.

Exclusions are sorted and have positive counts. `provenance.records_received` always equals the contracts plus
the record-level exclusions. No provider payload content is recorded.

Caller mistakes raise `SnapshotAssemblyError`. These include a bad `as_of`, underlying, scope, provenance or
underlying price, and data supplied for a group declared unavailable.

## 11. Canonicalization and validation

- **Canonical form:** the MIAS form (sorted keys, compact separators, `allow_nan=False`, ASCII), defined locally
  in `options_data.canonical`. A test proves it is byte-identical to the Phase 7C serialization.
- **Order independence:** neither the provider's record order nor dict key order changes `snapshot_id`. All 120
  permutations of a 5-contract chain give identical bytes.
- **Validation:** `validated_snapshot` fails closed with a stable message. It checks:
  - the version and exact keys, and that the id recomputes;
  - the underlying, and a canonical UTC `as_of`;
  - the session date;
  - the identity re-derivation, uniqueness, canonical order and expiry;
  - every group's status, value and time basis, with the non-negative and whole-number rules;
  - times and dates against the cutoff;
  - the capability agreement;
  - the exclusion and provenance arithmetic.

  Every assembled snapshot is validated before it is returned.

## 12. No-I/O boundary

- `canonical`, `model`, `identity`, `normalization` and `validation` import only the standard library,
  `options_data` and `market_data.models` (for `SYMBOL`, `format_decimal` and `EXCHANGE_TZ`).
- At runtime they load nothing else from the project: none of persistence, sqlalchemy, evidence, evaluation,
  evidence_packet, evidence_synthesis, market_intelligence, requests or AI.
- A sealed test blocks sockets, files, subprocesses, the clock, environment reads and database creation, and
  assembly still produces the same id.
- The Phase 9A I/O files (`config`, `massive_check`) are unchanged.

## 13. Performance

A 10,000-contract chain (25 expirations × 200 strikes × 2 types) is assembled and self-validated in about 5 s. It
serializes to about 12 MB, roughly 1.2 KB per contract. Every contract is kept; there is no preference-based
filtering.

## 14. No trading semantics

The snapshot has:
- no score, confidence, ranking, recommendation, signal or prediction;
- no buy or sell, and no selection of contracts, strikes or expirations;
- no mid, spread, quote state, days to expiration, moneyness, liquidity label or IV rank.

Tests scan the keys, vocabularies and code identifiers for these concepts.

## 15. Phase 6 safeguards

- No read or write of `evidence/`, `evaluation/`, the prospective ledger, forward returns or Phase 6 outcomes.
- `MARKET_DATA_*`, the Phase 6 hashes and the migration head (`0007_technical_evidence_ledger`) are unchanged.
- There is no database and no persistence.
