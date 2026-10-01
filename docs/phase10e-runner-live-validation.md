# Phase 10E — Runner, replay and live validation

**Status:** implementation is complete on `claude/phase10e-runner-live-validation`; not merged. **Live validation
is pending:** the live META/NVDA runs and the regular-session quote-freshness check need operator credentials and
market hours (§6). **Phase 10 is not closed until they are done** (§9).

> Phase 10E adds orchestration and file replay only. It connects the frozen pipeline:
>
> MarketIntelligence + OptionsIntelligence + policy → `TradeSetupAssessment`, then, optionally, a later
> MarketIntelligence → `InvalidationCheck`.
>
> It changes no schema, screening rule, vocabulary, premium or delta semantics, policy, version, or any Phase 9
> behaviour.

## 1. `trade_setup.runner`

```bash
python -m trade_setup.runner --market-intelligence MI.json --options-intelligence OI.json --policy POLICY.json --assessment-output ASSESSMENT.json
```

```bash
python -m trade_setup.runner --market-intelligence MI.json --options-intelligence OI.json --policy POLICY.json --assessment-output ASSESSMENT.json --later-market-intelligence LATER_MI.json --invalidation-output INVALIDATION.json
```

- **Inputs:** every path is explicit, with no defaults or inferred policy.
  - MarketIntelligence: `phase8-v1`.
  - OptionsIntelligence: `phase9-v1` or `phase9-v2`. A `phase9-v1` input with numeric rules enabled is rejected with
    `policy_requires_phase9_v2`.
  - A sealed `phase10-policy-v1` policy file.
  - Optionally, a later MarketIntelligence, which must come with `--invalidation-output`.
- **Rules:** duplicate JSON keys are rejected, outputs must be distinct from each other and from every input, and
  nothing is repaired.
- **Processing:** each input is validated structurally, the assessment is built (`assess`) and the optional
  InvalidationCheck (`check_invalidation`). **Every output is validated and re-derived in memory before anything is
  written.**
- **Outputs:** canonical JSON (the builders' exact bytes plus a trailing newline), written atomically:
  - each goes to a temporary file in the target directory and is fsynced;
  - then it is hard-linked into place (no-clobber) or, with `--overwrite`, replaces the target;
  - every target is checked for collisions before anything is written;
  - without `--overwrite`, a commit failure removes any output already linked: all requested outputs or none;
  - with `--overwrite`, an OS error after the first replace can leave the new assessment without the invalidation
    file. Both files are complete and valid, never partial.
- **Summary:** stdout carries a metadata-only JSON summary with ids, as-of times and input gap, outcome and
  reasons, bias and side, quote-state counts, candidate count, `premium_risk_status` counts, rejection counts, the
  step-8 result, and the invalidation result if requested. It never contains prices, strikes or contract lists.
- **Errors:** stderr carries `{"result": "FAILED", "exit_code", "error"}`.

| Exit | Meaning |
|---|---|
| 0 | written |
| 2 | usage error, or an unreadable, invalid or incompatible input (tampered id, symbol mismatch, `policy_requires_phase9_v2`, invalidation of a `no_setup`, a same-age or older later MI, …) |
| 3 | a built output failed its own validation or re-derivation (internal integrity) |
| 4 | an output exists without `--overwrite`, or cannot be written |

These codes follow the requested scheme. The Phase 9E `options_intelligence.runner` uses a different one (1 invalid,
2 usage, 3 output). Runner conventions already differ across MIAS, so neither was changed.

**Replay** is byte-deterministic. The same sealed files give the same `assessment_id`, `invalidation_id` and bytes,
regardless of cwd, `PYTHONHASHSEED`, `TZ`, `LANG`, `HOSTNAME` or unrelated environment. Timings come from a
monotonic counter and are never part of a content-addressed object.

## 2. Boundaries

- `trade_setup` apart from the runner is the pure core. It does no I/O; the tests check every module.
- The runner reads and writes local files only. It makes no network, provider, database, AI, environment or clock
  calls; its imports and its runtime-loaded modules are test-pinned.
- Provider collection stays outside `trade_setup`.
- Nothing adds ranking, a "best" contract, sizing, targets, stops, reward/risk, a scheduler, a service or execution.

## 3. Live-validation tooling (`live_validation.phase10`)

The tooling covers two integration gaps without changing a frozen package:
- the Phase 9E runner emits only `phase9-v1`;
- there is no MarketIntelligence CLI.

It is local and metadata-only. It never reads the environment, and never touches the network or a database.

```bash
python -m live_validation.phase10 snapshot-summary --snapshot SNAP.json
```

```bash
python -m live_validation.phase10 options-intelligence --snapshot SNAP.json --output OI.json
```

```bash
python -m live_validation.phase10 market-intelligence --synthesis SYN.json --output MI.json
```

- `snapshot-summary` reports:
  - quote status and time-basis counts, and the two-sided quote count;
  - **quote age** (snapshot `as_of` minus quote `observed_at`) as min, p50, p90 and max, plus buckets (≤60 s,
    ≤120 s, ≤900 s, ≤3600 s, more);
  - multiplier presence, underlying-price status, source capabilities and paging.

  This is characterization only. No freshness threshold exists or is created.
- `options-intelligence` builds `phase9-v2` with the unchanged Phase 9D builder, verifies it by re-derivation, and
  reports the phase9-v2 fact counts.
- `market-intelligence` builds Phase 8 MarketIntelligence from an EvidenceSynthesis file.

**Live-validation policy:** `live_validation/policies/phase10e_live_validation.policy.json`, with
`policy_id` `sha256:fd9566c4…`. Its values are illustrative, for validation, **not tuned and not optimal**:

| Rule | Value |
|---|---|
| sides | call, put |
| DTE | 7–45, no same-day expiry |
| locked quotes | excluded |
| relative spread | ≤ 0.1 |
| \|delta\| | 0.25–0.6 |
| current-session volume | ≥ 10 |
| open interest | ≥ 100 |
| premium cap | none (no account assumption) |
| IV | required |
| unverified time basis | allowed (Massive IV, Greeks and OI are untimed) |
| complete chain | required |
| input gap | ≤ 1800 s |
| context gates | none |

## 4. Provider entitlement

- **Options: Advanced.** The operator's out-of-hours check returned HTTP 200 on the chain snapshot, contract
  snapshot, contract reference and dedicated quotes endpoints. Bid/ask fields, quote timestamps,
  `shares_per_contract` and `underlying_asset.price` were present in the sample. Real-time freshness has **not**
  yet been checked in a regular session.
- **Stocks: Starter.** Not upgraded. See §7.
- **Phase 9C adapter:** it already maps the chain snapshot's `last_quote` (`bid`, `ask`, `bid_size`, `ask_size`,
  plus `last_updated` in epoch ns as `observed_at`). It declares the quote group unavailable only when *no* returned
  record carries `last_quote`. So with quotes in the chain snapshot, quotes reach the OptionsSnapshot (time basis
  `observed_at`) and then OptionsIntelligence `quote_state` with no Phase 9 change and no second quote call.
  - This is confirmed in code and on synthetic data. **A live Advanced snapshot must confirm it with real data**
    (§6, step 2).
  - If live quotes still show as unavailable, that is a Phase 9 gap. Report it before any Phase 9 change.
- **Underlying price:** the adapter still treats any price as `not_used_in_v1`. Nothing in Phase 10 uses it.

## 5. What `no_setup` means

`no_setup` is a correct, complete outcome, not a failure. Live validation succeeds when the system behaves
correctly, whether or not a candidate exists. The reasons come from the frozen vocabulary. For example:
- `inputs_not_contemporaneous`: the MI and OI are further apart than the policy allows;
- `execution_data_unavailable`: no usable quote;
- `no_candidate_satisfies_policy`, with per-contract rejection counts.

The live result is never forced, and policy is never bypassed.

## 6. Live validation (operator-run; pending)

These steps need credentials, so the operator runs them. Keys are supplied only in the process environment and are
never written to files or printed.

**Configuration check.** The options runner sets `as_of = clock - OPTIONS_DATA_DELAY_SECONDS`. With real-time
Advanced quotes, **leave `OPTIONS_DATA_DELAY_SECONDS` unset**. Otherwise fresh quotes are correctly excluded as
`quote_after_as_of`.

The full META chain was about 7,780 contracts (about 32 pages at 250), and the live policy requires a complete
chain. The steps below use META; repeat them with `NVDA`.

1. Collect an options snapshot (network, options key):

   ```bash
   OPTIONS_DATA_PROVIDER=massive OPTIONS_DATA_API_KEY=... python -m options_data.runner --symbol META --output /tmp/p10e/META.snapshot.json --page-limit 250 --max-pages 60 --max-requests 80
   ```

2. Check quote readiness and freshness (local):

   ```bash
   python -m live_validation.phase10 snapshot-summary --snapshot /tmp/p10e/META.snapshot.json
   ```

3. Build the phase9-v2 OptionsIntelligence (local):

   ```bash
   python -m live_validation.phase10 options-intelligence --snapshot /tmp/p10e/META.snapshot.json --output /tmp/p10e/META.oi.json
   ```

4. Build MarketIntelligence through the existing stocks pipeline (stocks key, then database):

   ```bash
   python -m market_context.runner --symbols META --out /tmp/p10e/ctx.json
   ```

   ```bash
   python -m evidence_packet.runner --symbol META --as-of <snapshot as_of> --market-context /tmp/p10e/ctx.json --output /tmp/p10e/META.packet.json
   ```

   ```bash
   python -m evidence_synthesis.runner --packet /tmp/p10e/META.packet.json --output /tmp/p10e/META.synthesis.json
   ```

   ```bash
   python -m live_validation.phase10 market-intelligence --synthesis /tmp/p10e/META.synthesis.json --output /tmp/p10e/META.mi.json
   ```

5. Run the Trade Setup assessment (local):

   ```bash
   python -m trade_setup.runner --market-intelligence /tmp/p10e/META.mi.json --options-intelligence /tmp/p10e/META.oi.json --policy live_validation/policies/phase10e_live_validation.policy.json --assessment-output /tmp/p10e/META.assessment.json
   ```

6. Invalidation: only if step 5 produced `setup_candidates`, rebuild MI later with step 4 and a later `--as-of`, then
   rerun step 5 with `--later-market-intelligence` and `--invalidation-output`. No setup is ever fabricated from live
   data. Without a live setup, invalidation is validated by the replay fixtures.

7. Regular-session freshness: during regular hours, repeat steps 1–2, and optionally run
   `options_data.massive_check`, which reports quote timestamps and its existing appears-real-time or delayed
   classification.

**Report metadata only:** the summaries, never prices or contract lists, and never anything described as a "best"
contract or a recommendation. Raw live snapshots in `/tmp` are never committed.

### Live results

*Pending the operator runs above.* This section records, for META and NVDA:
- quote-state counts and quote age;
- multiplier and phase9-v2 fact counts;
- MI and OI as-of times and the input gap;
- the assessment outcome and reasons, candidate count and rejection counts;
- the ids;
- invalidation status;
- whether the regular-session quotes appear real-time.

## 7. Stocks Starter

Stock-side MarketIntelligence can lag real-time options quotes. The packet's `--as-of` sets the MI time:
- The gap rule compares MI `as_of` with OI `as_of`. Building the packet at (or near) the snapshot `as_of` keeps the
  gap within policy, even with delayed stock bars inside the evidence.
- If the gap exceeds `max_input_gap_seconds`, the correct frozen outcome is `no_setup` with
  `inputs_not_contemporaneous`. It is never bypassed.

**Stocks Advanced is not required by any Phase 10 contract.** It would only matter if live runs show that
stock-side evidence cannot be produced within the policy gap. Live validation should measure that before any
upgrade decision.

## 8. Invalidation integration

`--later-market-intelligence` adds a point-in-time `InvalidationCheck` (`holds` / `invalidated` /
`not_evaluable`):
- it is built only from a `setup_candidates` assessment;
- the later MI must be strictly newer than the establishing MI;
- it is written atomically with the assessment.

The earliest `invalidated` check is terminal downstream. `trade_setup` keeps no lifecycle state.

## 9. Phase 10 closure

| # | Criterion | Status |
|---|---|---|
| 1–4 | 10A frozen; 10B gates, 10C screening and 10D invalidation pass | done |
| 5–7 | runner from sealed files; byte-deterministic replay; phase9-v2 integration | done |
| 8 | Options Advanced quote entitlement verified | operator check done (out of hours) |
| 9 | a live META/NVDA snapshot shows usable quote facts | **pending** (§6, steps 1–2) |
| 10 | a live TradeSetupAssessment runs end-to-end | **pending** (§6, step 5) |
| 11 | regular-session quote freshness characterized | **pending** (§6, step 7) |
| 12–14 | Phase 6 untouched; full regression passes; protected packages stable | done |

**Status: Phase 10E implementation is complete; Phase 10 live-session validation is pending.** Phase 10 closes
when items 9–11 are recorded in §6.

## 10. What remains for Phase 11

These are out of Phase 10 scope, and none is started:
- scheduling or persistence of assessments and checks;
- lifecycle tracking (the downstream earliest-invalidation rule);
- any position-sizing framework with explicit account inputs;
- a price-level framework (prerequisite for targets, stops or breakeven);
- quote-staleness facts in OptionsIntelligence.
