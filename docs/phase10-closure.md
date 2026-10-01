# Phase 10 — Regular-session live validation and closure

**Closure date:** 2026-10-01. **Status: PHASE 10 COMPLETE.**

| Step | Status |
|---|---|
| 10A architecture freeze | COMPLETE |
| 10B global gates | COMPLETE |
| 10C contract screening | COMPLETE |
| 10D invalidation | COMPLETE |
| 10E runner and replay | COMPLETE |
| Live regular-session validation | **COMPLETE** (2026-10-01) |
| **Phase 10 overall** | **COMPLETE** |

The live validation was operator-run during the regular U.S. session on **2026-10-01**, following
`docs/phase10e-runner-live-validation.md` §6.
- Only metadata and ids are recorded here. No raw snapshot, payload, price, contract list or credential is stored
  in git.
- Nothing in Phase 10's logic, policy, thresholds, contracts or schemas changed, and no Phase 9 adapter change was
  needed.
- The policy was the checked-in `live_validation/policies/phase10e_live_validation.policy.json` (`policy_id`
  `sha256:fd9566c40b3935867cc39e99049e0e5350f0e76964425c0f21f447981996c34c`, `max_input_gap_seconds` 1800),
  unchanged.

## 1. META

**OptionsSnapshot**
- `snapshot_id`: `sha256:4b7128c0711c7fc3104f15024cb261404bc54d7f01b80bbbfd6bb52745f23a9b`
- Format `phase9-snapshot-v1`; `as_of` 2026-10-01T14:33:03.254990Z, regular session.

| Fact | Value |
|---|---|
| contracts | 7,474 |
| pages / requests | 30 / 30 |
| truncated | false |
| quote status | `present` 4,135; `excluded_after_as_of` 3,339 |
| timestamped / two-sided quotes | 4,135 / 4,135 |
| `shares_per_contract` present | 7,474 (all) |
| quote age (s) | min 0, p50 593, p90 1,294, max 3,782 |
| quote age buckets | ≤60 s: 326; ≤120 s: 204; ≤900 s: 2,677; ≤3,600 s: 756; >3,600 s: 172 (sum 4,135) |
| source capabilities | day, greeks, implied_volatility, open_interest, quote, trade: all available |
| underlying price | unavailable / `not_used_in_v1` |

**phase9-v2 OptionsIntelligence**
- `sha256:0e934a0b9e8e8ce6482de927bee798140817975cecb2a47de3368c05764d13e3`
- Contracts 7,474; complete quotes 4,135; `shares_per_contract` 7,474; open interest 7,474; current-session volume
  1,585; Greeks 6,808; IV 6,808; not truncated.

**Market context**
- `as_of` 2026-10-01T14:33:16.907872Z, with the configured stock delay of 900 s.
- META, SPY and QQQ were all `freshness.status = current` with `lag_bars = 0`; the latest bar ended at 10:30 ET.
- Calendar state: regular.

**Technical persistence** (shadow persistence on, Phase 6 evidence ledger off):
- `TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED=true`, `TECHNICAL_EVIDENCE_LEDGER_ENABLED=false`.
- Queued 3, persisted 3, duplicate 0, conflict 0, failed 0; drained.
- Fresh bars selected: 1d bar end 2026-09-30 16:00 ET; 1h 2026-10-01 10:30 ET; 5m 2026-10-01 10:40 ET.

**EvidencePacket and MarketIntelligence**
- Packet: `sha256:d073668104b52e0843ea38c7badac32f516fc9841c682ef1a6f733e56f7b32dc`, `as_of` 2026-10-01T15:00:14Z.
  Market context available, technical available, news `available_empty`.
- MI: `sha256:a34169b21c9fc82adcb62abd59313e0cd96d7a3fd0a11489d766870bf574f4b2`, `as_of` 2026-10-01T15:00:14Z.
  Technical status `available`; pattern `partially_directional`.

**TradeSetupAssessment**
- `sha256:016428d8880849fbfbef5aba932e77073a54aa6ef5af0cfc76eee717a3070114`
- Input gap **1,630 s ≤ 1,800**.
- Market bias `partially_directional`, so the outcome is **`no_setup`** with `market_evidence_partially_directional`.
- Step 8 is `not_evaluated` / `global_gate_failed`. There are 0 candidates, 4,135 usable two-sided quotes, and the
  chain is not truncated.

## 2. NVDA

**Initial OptionsSnapshot**
- `sha256:5de8975e02691404fb64300c712903f2d01d1209bdb07351367b0662257c3c95`, `as_of` 2026-10-01T15:08:11.474211Z.
- Contracts 3,836; 16 pages / 16 requests; not truncated.
- Quotes: `present` 2,352 (all timestamped and two-sided); `excluded_after_as_of` 1,484.
- `shares_per_contract` 3,836 (all).
- Quote age: min 0, p50 645 s, p90 1,863 s, max 5,891 s.
- Buckets as reported: ≤60 s: 645; ≤120 s: 46; ≤900 s: 782. The over-900 s buckets weren't recorded; by difference
  they hold 879 quotes. The reported ≤60 s count equals the reported p50 value, which may be a transcription
  coincidence. It's recorded as given.
- Initial phase9-v2 OI: `sha256:c2fb894b42786d2445cbfb9fdabdc3be0f9f2d1fe8aa28ba3b7c54a1dc248ce5`.

**Market context**
- `as_of` 2026-10-01T15:19:10.523178Z, with a 900 s delay.
- NVDA, SPY and QQQ were all `current` with `lag_bars = 0`; the latest bar ended at 11:15 ET.
- Calendar state: regular.

**Technical persistence** (shadow on, Phase 6 ledger off):
- Queued 3, persisted 3, duplicate 0, conflict 0, failed 0; drained.
- Fresh bars: 1d 2026-09-30; 1h 2026-10-01 10:30 ET; 5m 2026-10-01 10:55 ET.

**EvidencePacket and MarketIntelligence**
- Packet: `sha256:4d55bd337204c305ef093cecdf14e2f1c50cacc9afe57989f56deb99b5b3fdfe`, `as_of` 2026-10-01T15:38:30Z.
- MI: `sha256:2ec166625c698e2c62142dcc4961d0778a95d7648aa368bbe98f9babcc13b6b3`, `as_of` 2026-10-01T15:38:30Z.
  Technical status `available`; pattern `opposed`.

**Refreshed options** (the initial MI/OI gap was 15:38:30 − 15:08:11.47 = **1,818.5 s > 1,800**):
- Snapshot: `sha256:d5ed5dc540594555e3de8a23f43485d23972ed1e878f4699cc4d507ee4d39935`, `as_of`
  2026-10-01T15:43:11.280465Z.
  - Contracts 3,836; not truncated.
  - Quotes `present` 2,473, `excluded_after_as_of` 1,363.
  - `shares_per_contract` 3,836.
- Phase9-v2 OI: `sha256:0deef15bb8fd101ba34aee523ce95a8af7402959624563d5c6c3828382caa3aa`.
  - Complete quotes 2,473; current-session volume 1,549; open interest 3,836; `shares_per_contract` 3,836.

**TradeSetupAssessment**
- `sha256:ed76285b10a7ab00a2d8f2c4ebe08dc69dab15eb9b23fa5b399976b3cab9e2bc`
- Input gap **281 s ≤ 1,800**.
- Market bias `conflicting` (pattern `opposed`), so the outcome is **`no_setup`** with `market_evidence_conflicting`.
- Step 8 is `not_evaluated` / `global_gate_failed`. There are 0 candidates, 2,473 usable two-sided quotes, and the
  chain is not truncated.

## 3. Conclusions

1. **Options Advanced** is entitled during regular market hours.
2. Timestamped live bid/ask quotes flow end to end: Massive → Phase 9C OptionsSnapshot (`observed_at` time basis) →
   phase9-v2 OptionsIntelligence (`complete` quote states) → Phase 10 TradeSetupAssessment
   (`usable_two_sided_quote_count`).
3. The **Phase 9C adapter already maps the chain snapshot's `last_quote` correctly. No Phase 9 change is needed.**
4. Both chains were **complete** (not truncated): META with 7,474 contracts in 30 pages, NVDA with 3,836 in 16.
5. **Two-sided, timestamped quotes** were available for both symbols: 4,135 for META, and 2,352 and then 2,473 for
   NVDA.
6. **`shares_per_contract`** was present for every contract in both chains.
7. **Open interest** was present across both chains.
8. **Current-session volume** was partially present (META 1,585 / 7,474; NVDA 1,549 / 3,836), as expected: only
   contracts with a current-session day record carry it.
9. **Market context** was current, with `lag_bars = 0` under the frozen 900 s delay, for META, NVDA, SPY and QQQ.
10. **Stocks Starter is sufficient for the current Phase 10 design.** Both assessments ran within the 1,800 s
    contemporaneity limit (META 1,630 s; refreshed NVDA 281 s). These observations give no reason to upgrade to
    Stocks Advanced.
11. **Input-gap enforcement works, and nothing bypasses it.** NVDA's first pairing was 1,818.5 s apart, over 1,800.
    The options were refreshed, giving a 281 s gap, and the gate was not changed or skipped.
12. The **TradeSetupAssessment completed end to end** on real regular-session inputs for both symbols.
13. **`no_setup` is a valid, successful outcome.** It is the deterministic answer of the frozen D3 market-bias rule:
    META's `partially_directional` gives `market_evidence_partially_directional`, and NVDA's `opposed` gives
    `conflicting`. Validation needs correct behaviour, not a trade.
14. **No live candidate existed, so no live InvalidationCheck applied** (one is built only from a
    `setup_candidates` assessment). Invalidation is covered by the Phase 10D and 10E replay fixtures (`holds`,
    `invalidated`, `not_evaluable`, and the error cases).

**Quote freshness:**
- This is characterization only; no threshold was created.
- Quote ages run from 0 s up to roughly 1–1.6 hours: p50 593 s for META and 645 s for NVDA, p90 1,294 s and
  1,863 s. Only part of each chain was quoted in the last minute (META 326 quotes ≤ 60 s).
- Many quotes (META 3,339; NVDA 1,484, then 1,363) were `excluded_after_as_of`. The snapshot `as_of` is read once
  before the paged fetch, so quotes updated during the fetch are correctly excluded by the cutoff. This is the frozen
  Phase 9 behaviour, unchanged.
- These figures are worth reviewing before the Phase 11 activation review defines its collection timing.

## 4. Isolation and invariants

- **Phase 6:** the registry, protocol, H1′ and H2′ hashes are unchanged, as are the pin, start (2026-09-28), earliest
  evaluation (2027-03-29), 120 required sessions and the locked gate.
  - The live runs had `TECHNICAL_EVIDENCE_LEDGER_ENABLED=false`, and the operator reports no ledger write.
  - This closure didn't query the persistent database. That rests on the operator's run configuration and report.
  - Phase 6 prospective outcomes were not read.
- **Phase 10 logic, policy, contracts and versions:** unchanged (`phase10-v1`, `phase10-rules-v1`,
  `phase10-policy-v1`, and the invalidation versions).
- **Phase 11:** the implementation and replay framework is complete. The production protocol is **not activated**:
  `PRODUCTION_PROTOCOL_ID` and `PRODUCTION_PROSPECTIVE_START` are `None`, and no production protocol JSON exists.
  **Prospective Phase 11 collection has not started.** Activation is a separate, later step
  (`docs/phase11-setup-evaluation.md` §9).
