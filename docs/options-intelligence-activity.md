# Options Intelligence activity (read model and dashboard)

A compact, descriptive summary of each Options Intelligence report, and its change since the previous report of the
same symbol and trading session. It is derived only from fields the phase9-v2 artifact already records. The full
artifact stays immutable and is the only source of truth. The Phase 9 schema, rules and semantics are unchanged.

There is no direction, prediction, recommendation, contract pick, entry, exit, size, target or stop.

## Architecture

```
full immutable OptionsIntelligence artifact (7–14 MB)
   │ parsed and validated once per index refresh (already the case)
   ├─► Summary (artifact_store/options_activity.py), kept on the in-memory index entry (~0.7 KB)
   │      ├─► GET /api/v1/options-intelligence[/{id}|/latest]   options-intelligence-summary-v1 (unchanged output)
   │      └─► GET /api/v1/options-intelligence/activity         options-intelligence-activity-v1 (new)
   └─► GET /api/v1/options-intelligence/{id}/canonical         exact stored bytes (unchanged)
```

- **Derivation:** the summary is computed during the existing index build from the object already parsed for
  validation, so no extra parse is needed. It is never persisted and is rebuilt with every refresh.
- **Fallback:** if a summary cannot be derived, the build does not fail. Readers fall back to reading the file.
- **Prior lookup:** the index records each report's immediately previous report for the same symbol (strictly earlier
  `as_of`). Two reports at that same instant make the prior ambiguous.
- **No file reads on the list path:** list, latest and by-id views no longer read or parse files. Only `/canonical`
  reads the stored bytes (and re-checks their digest).

## Definitions

| Field | Definition |
|---|---|
| `contract_count` | `len(contracts)` (as in the summary view) |
| `expiration_count` | `chain_completeness.expiration_count` |
| `call_volume`, `put_volume` | `activity.call_volume_total`, `activity.put_volume_total`: Phase 9 current-session day records only; `null` when there are none |
| `put_call_volume_ratio` (+ reason) | copied from `activity` (Phase 9 formula) |
| `iv_median` | `volatility.overall.median`, a decimal fraction (the UI shows a percentage) |
| `current_session_volume_gt_oi_count` | contracts with Phase 9 `volume_exceeds_open_interest == true` **and** a `current_session` day record |
| `call_breadth`, `put_breadth` | distinct strikes of that type with `current_session_volume > 0` |
| `*_concentration` | the Phase 9 `strike_relation` (below / equal / above the underlying price) holding the largest share of that type's current-session volume: `below_spot`, `at_spot` or `above_spot`; a tie is `mixed`. `unavailable` without an underlying price (phase9-v2 records none: `price_reason = not_used_in_v1`) or without current-session volume |
| `comparison.status` | `comparable`, `no_prior_snapshot`, `prior_from_other_session` or `prior_ambiguous` |
| session | the America/New_York calendar date of `as_of` (Phase 9's session date) |
| `*_volume_change` | current − prior cumulative current-session volume |
| `*_volume_change_pct` | change ÷ prior × 100, two decimals; `null` with a reason: `not_comparable`, `value_unavailable` or `prior_zero` |
| `current_session_volume_gt_oi_change`, `*_breadth_change` | current − prior; `null` when not comparable |
| `activity_bias` | `CALL` if call > put volume, `PUT` if put > call, `BALANCED` if equal, `UNAVAILABLE` if either is missing |
| `momentum_15m` | the side with the larger volume increment since the prior report (`CALL`, `PUT` or `BALANCED`). Otherwise `INSUFFICIENT_PRIOR` (no same-session prior), `UNAVAILABLE` (missing volume) or `VOLUME_CORRECTION` (a cumulative volume went down). |
| `trend_summary` | one sentence from a closed rule set (`trend_summary()`; every sentence is enumerated in the tests), plus an optional "… broadening across more strikes." clause when breadth increased |

**No thresholds.** "Near ATM" or "far" concentration would need a distance threshold, so it is not implemented. Any
such rule needs explicit approval first.

**Not compared:** the previous trading day is never used for intraday changes, and another symbol is never compared.
Missing values are never turned into zero changes.

**Current-session Vol > OI** (`current_session_volume_gt_oi_count`; decision 2026-10-06) is restricted to
current-session day records. It is not the raw Phase 9 relation. Phase 9 computes `volume_exceeds_open_interest` from
whatever day record a contract has. Before the open that record is the previous session's: 463 to 806 contracts were
flagged in the 07:35 ET reports of 2026-10-06, which had no current-session volume. Counting them would mix sessions.

## Performance (70 real phase9-v2 artifacts, 722 MB, 2026-10-05 and 2026-10-06; in-process, unconstrained CPU)

| Request | Before | After |
|---|---|---|
| `GET /options-intelligence` (50 rows) | 6.9 s | 0.004 s |
| `GET /options-intelligence?symbol=META` | 6.5 s | 0.003 s |
| `GET /options-intelligence/activity` (50 rows) | – | 0.006 s, 57 KB |
| Index build | 35.8 s | 35.4 s (summaries add about 0.4 s) |

The summary view's response is unchanged: identical for all 70 real artifacts and byte-for-byte the same size
(18,141 bytes). The API pod is limited to 0.5 CPU, so its absolute times are higher.

## Known limitations

- **Index cost still grows with the store.** The 30 s index rebuild still re-reads and re-validates every artifact. That
  is the existing design and is not changed here. An incremental index is a separate decision.
- **Concentration is always `unavailable`** while Phase 9 records no underlying price.
- **The momentum label is fixed.** `momentum_15m` compares with the previous report, whatever its spacing; the response
  includes `comparison.prior_as_of`.
