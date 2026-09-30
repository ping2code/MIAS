# Phase 9D — OptionsIntelligence (pure)

**Status:** implemented on `claude/phase9d-options-intelligence`; not merged.
**Format:** `phase9-v1` · **Rules:** `phase9-rules-v1` · **Input:** OptionsSnapshot `phase9-snapshot-v1`, plus an
optional MarketIntelligence reference

> OptionsIntelligence is a deterministic **description** of one options chain. It never chooses calls or puts, a
> contract, a strike or an expiration. It never ranks or recommends, and it computes no entry, stop, target, size,
> expected return, probability of profit or signal. All of that is Phase 10.

## 1. Purpose and API

`options_intelligence.builder.build(snapshot, market_intelligence=None, *, calendar=None)`:
- **`snapshot`:** a typed or dict OptionsSnapshot, fully validated with the Phase 9B sealed validation.
- **`market_intelligence`:** optional; validated, and used only as a reference (§3).
- **`calendar`:** an object with `previous_trading_day(date)`, e.g. `market_data.calendar.ExchangeCalendar`. It is
  used only to classify day records by session. When omitted, the default XNYS calendar is created lazily.

There is no runner in 9D.

Validation:
- `validated_options_intelligence(obj)` checks a sealed object structurally;
- `verify_against_snapshot(obj, snapshot, mi, calendar=)` re-derives everything from the snapshot and requires an
  exact match.

## 2. Schema (13 top-level fields, pinned)

| Field | Content |
|---|---|
| `options_intelligence_format_version` | `phase9-v1` |
| `options_intelligence_id` | `sha256:` + SHA-256 of the canonical body without the id |
| `rules_version` | `phase9-rules-v1` |
| `snapshot_ref` | `snapshot_id`, `snapshot_format_version`, `underlying`, `as_of` (the snapshot itself is not copied) |
| `market_intelligence_ref` | null, or `intelligence_id`, `synthesis_id`, `symbol`, `as_of`, `as_of_gap_seconds` |
| `underlying` | `symbol`, `price_status`, `price`, `price_observed_at`, `price_age_seconds`, `price_source`, `price_reason`, copied; no second lookup |
| `chain_completeness` | Counts only (§11) |
| `expirations` | Per-expiration summaries, ascending (§9) |
| `contracts` | Per-contract facts, in snapshot canonical order (§4–8) |
| `activity` | Chain activity (§10) |
| `volatility` | IV summaries (§7) |
| `attention` | Closed flags (§12) |
| `provenance` | `snapshot_id`, `market_intelligence_id`, `rules_version`, `pointer_version` (`phase9-pointer-v1`) |

There is no `generated_at`.
- **Numbers:** canonical Decimal strings. Differences and `/2` are exact; divisions are quantized to 10 places,
  round-half-even.
- **Collections:** deterministic order. Keyed counts are lists of `{key, count}`.

## 3. Optional MarketIntelligence reference

**Accepted only if all of these hold:**
- format `phase8-v1` and rules `phase8-rules-v1` (frozen local copies, test-pinned to `market_intelligence.rules`);
- exactly the 13 Phase 8 keys;
- `intelligence_id` recomputes;
- its symbol equals the snapshot underlying;
- its `as_of` is not after the snapshot `as_of`.

**How it's used:** it is referenced only. The builder output is identical with and without it, apart from the
reference, provenance and id, and a test proves this. There is no call/put preference, filtering, direction or
ranking.

**No evidence-layer import:** this package never imports `market_intelligence` or the evidence layers.

## 4. Days to expiration and strike relation

**Days to expiration:**
- `dte_calendar_days` = expiration date − the `as_of` America/New_York date (≥ 0);
- `expires_on_as_of_date` is true when it is 0;
- there are no preferred ranges.

**Strike relation:**
- `strike_relation` is `below`, `equal` or `above`, by exact comparison with the snapshot underlying price.
- `strike_distance` = strike − price (signed), and `strike_distance_relative` = distance / price.
- Without a price, the relation is `unavailable` and both distances are null.
- There are **no ITM/ATM/OTM labels**.

## 5. Quote state, mid and spread

| `quote_state` | Rule |
|---|---|
| `complete` | bid and ask present, 0 ≤ bid < ask |
| `locked` | bid = ask |
| `crossed` | ask < bid |
| `bid_missing` / `ask_missing` / `both_missing` | One side or both absent |
| `unavailable` / `excluded_after_as_of` | Copied from the snapshot group status |

**Mid and spread** are computed only for `complete` and `locked` quotes:
- `mid = (bid + ask)/2` and `spread_absolute = ask − bid`;
- `spread_relative = spread/mid`. A zero mid gives null with `spread_relative_reason = zero_mid`;
- any other state gives nulls with the reason `quote_not_two_sided`.

There is no "wide spread" threshold.

## 6. Activity states

- **`volume_state`** (from `day.volume`) and **`open_interest_state`** each take one of `positive`, `zero`,
  `missing`, `unavailable` or `excluded_after_as_of`. There are no high, low, liquid or illiquid labels.
- **`volume_exceeds_open_interest`:** true or false when both values are present, otherwise null. It is **never**
  called unusual activity.

## 7. Day session relation, IV and Greeks

**Day.** Phase 9C proved day records are each contract's *most recent* day, and often not today's:
- `age_seconds` = `as_of` − `day.observed_at`;
- `session_relation` is `current_session` (the `as_of` exchange date), `previous_session` (on or after the previous
  XNYS trading day), `older_session`, or `unavailable` (no trustworthy observation time);
- a present day group never implies current-session activity.

**IV.**
- Per contract, status, value and time basis are copied from the snapshot; the provider value is never modified.
- `volatility.overall` and `volatility.by_expiration_and_type` give available and missing counts, min, max and
  median. The median uses sorted values: the middle one, or the exact mean of the two middle ones.
- There is no rank, percentile, cheap or expensive label, skew signal or forecast.

**Greeks.**
- Provider values are copied, and there is no in-house model.
- `available_fields` and `missing_fields` are listed; `rho` is optional.
- `out_of_bounds_fields` uses mathematical sanity predicates only: call delta in [0, 1], put delta in [−1, 0],
  gamma ≥ 0 and vega ≥ 0. It is descriptive, never a rejection.

## 8. Provenance

Every contract carries sorted, key-based `source_pointers` into the snapshot:
- `contracts[<contract_id>].<group>.<field>`;
- `underlying_price.value`;
- `as_of`.

List positions are never used. Tests resolve every pointer against the snapshot.

## 9. Expiration summaries

Per expiration, ascending:
- `dte_calendar_days`;
- contract, call and put counts;
- distinct strike count, strike min and max;
- `paired_strike_count` (strikes with both a call and a put);
- `quote_state_counts`;
- IV-available, Greeks-available, volume-present and open-interest-present counts;
- an `activity` block (§10).

There is no best or preferred expiration and no expiry score.

## 10. Chain activity: structural measures only

**Volume totals count current-session day records only** (`volume_basis: current_session_day_records`), because
day records from older sessions are not comparable with today's. The block also gives:
- `call_volume_records` / `put_volume_records`: the records counted;
- `volume_records_not_current_session`: the records set aside.

**Open-interest totals** count every present value (open interest is untimed on this plan). The block also gives
`call_open_interest_records` / `put_open_interest_records`.

**Put/call ratios** (`put_call_volume_ratio` and `put_call_open_interest_ratio`) are put total / call total, with
explicit reasons for a null: `zero_denominator`, or `total_unavailable` when either side has no records. They are
structural activity measures, with no sentiment interpretation.

## 11. Completeness

Counts only; there is no composite score:
- `contract_count`, `expiration_count`, `records_received`;
- `truncated`, copied from the snapshot provenance;
- snapshot `exclusions`;
- per-group `status_counts`;
- `contracts_with` (a present group);
- `day_session_relation_counts`.

## 12. Attention

Closed codes, each with a fixed category:

| Category | Codes |
|---|---|
| `gap` | `quote_incomplete`, `iv_unavailable`, `greeks_unavailable`, `day_not_current_session`, `underlying_price_unavailable`, `time_basis_unverified` |
| `data_quality` | `crossed_quote`, `locked_quote`, `greeks_out_of_bounds`, `facts_excluded_after_as_of`, `records_excluded` |
| `presence` | `no_volume`, `no_open_interest` |

Each entry has exactly `category`, `code`, `scope` (`contracts` or `chain`), `count` and a sorted `contract_ids`.
Chain-scope flags have no ids:
- `underlying_price_unavailable` has count 1;
- `records_excluded` has the count of record-level exclusions.

Entries are sorted by (category, code), and a flag is emitted only when its condition holds.

There is **no severity, priority, score or recommendation**, and no `wide_spread`, liquidity, tradeable or avoid
code.

## 13. Validation (fail closed)

**Structural checks:**
- format and rules versions, exact keys, and that the id recomputes;
- the `snapshot_ref` and `market_intelligence_ref` shapes, and provenance agreement;
- the closed vocabularies;
- canonical contract, expiration and attention order;
- the attention entry shape and counts;
- that each pointer names its own contract.

**Re-derivation (`verify_against_snapshot`):** the object must equal a fresh build from its snapshot (and
reference). This catches any inconsistent DTE, strike relation, quote state, mid or spread, activity state or
total, ratio, IV or expiration summary, completeness count or attention flag. The message names the first
differing body path. Nothing is ever repaired.

## 14. Canonicalization and no-I/O boundary

**Canonical form:** the MIAS form (via `options_data.canonical`). Snapshot record order and dict key order never
change the id, and the output is identical across processes, hash seeds, working directories, `TZ`, `LANG` and
`HOSTNAME`.

**Imports:** `builder`, `rules`, `model`, `validation` and `canonical` import only:
- the standard library;
- the pure `options_data` modules (`model`, `validation`, `canonical`);
- `market_data.models`;
- `market_data.calendar`, imported lazily and only when no calendar is passed.

They never import `market_intelligence`, the evidence layers, `options_data.massive`, `runner`, `provider` or
`config`, or `requests`. A sealed test blocks the network, files, subprocesses, the clock, the environment,
process identity and database creation.

## 15. Current Starter-plan limitations (live Phase 9C snapshots, 2026-09-30)

| | META | NVDA |
|---|---|---|
| `options_intelligence_id` | `sha256:cd3e7f33…` | `sha256:30fb2023…` |
| Contracts / expirations | 7,780 / 23 | 3,886 / 24 |
| Quote states | `unavailable` (all) | `unavailable` (all) |
| Strike relations | `unavailable` (no underlying price) | `unavailable` |
| Day sessions (current / previous / older / unavailable) | 1,754 / 1,727 / 2,023 / 2,276 | 1,365 / 820 / 1,128 / 573 |
| IV and Greeks available | 6,540 | 3,604 |
| Open interest available | 7,780 | 3,886 |
| Volume records counted (current session, call/put) | 988 / 766; 3,750 set aside | 759 / 606; 1,948 set aside |
| `greeks_out_of_bounds` | 6 | 93 |
| Build (with snapshot validation) / full verify / size | 3.2 s / 4.3 s / 12.2 MB | 1.5 s / 2.2 s / 6.3 MB |

The same schema already supports quotes, trades and an underlying price for richer plans.

**Synthetic 10,000-contract chain:** 4.3 s build and 14.2 MB. The output is larger than the snapshot because of the
per-contract pointers and the attention id lists.

## 16. Phase 10 boundary and Phase 6 safeguards

**Phase 10 owns:**
- direction, the call/put choice, and contract, strike and expiration selection;
- ranking and liquidity acceptability;
- entry, invalidation, stop and target;
- sizing and risk;
- any use of MarketIntelligence content to steer options.

**Phase 6:**
- no read of `evidence/`, `evaluation/`, the ledger, forward returns or outcomes;
- `MARKET_DATA_*`, the frozen hashes and the migration head (`0007_technical_evidence_ledger`) are unchanged;
- no persistence, migration or AI.
