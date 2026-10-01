# Phase 10C — Contract screening

**Status:** implemented on `claude/phase10c-contract-screening`; not merged. **Phase 10 is not complete:** premium-risk
and invalidation evaluation (10D) and the runner and live validation (10E) do not exist yet.

> Phase 10C screens every contract of a globally eligible input against the explicit policy. It produces the final
> sealed `TradeSetupAssessment` (`phase10-v1` / `phase10-rules-v1`), with `outcome.status` set to either
> `setup_candidates` or `no_setup`.
>
> There is no ranking, "best" contract, score, confidence, sizing, target, stop or reward/risk. The order of the
> candidates is a canonical order only.

## 1. Entry point and flow

```
assess(market_intelligence, options_intelligence, policy)
  └─ prescreen(...)                       Phase 10B global gates
       ├─ any gate failed  → sealed no_setup; step 8 not_evaluated / global_gate_failed;
       │                     no candidates, no rejections (screening never runs)
       └─ all gates passed → PreScreeningEligibility (internal, unsealed)
                               └─ screen(eligibility, options_intelligence) → final sealed assessment
```

`screen` fails closed with `TradeSetupInputError` in these cases:
- it is not given a `PreScreeningEligibility`;
- the OptionsIntelligence id differs from the eligibility's;
- the eligibility's side is inconsistent;
- a `phase9-v1` input is combined with `min_volume`, `min_open_interest` or `max_premium_per_contract`
  (`policy_requires_phase9_v2`).

`assess` is pure: no I/O, clock, environment, database, network or AI. It imports only the standard library and
`trade_setup`.

## 2. Screening order (frozen: `rules.SCREENING_RULES`)

Every contract is evaluated against every enabled rule, in this fixed order. Evaluation never stops at the first
failure, and the order never depends on the data.

| # | Rule | Policy field | Enabled when | Rejection reason |
|---|---|---|---|---|
| 1 | `side` | `allowed_sides` | always | `side_mismatch` (option_type ≠ eligible side) |
| 2 | `dte_minimum` | `min_dte` | always | `expiration_outside_policy` |
| 3 | `dte_maximum` | `max_dte` | always | `expiration_outside_policy` |
| 4 | `same_day_expiry` | `allow_same_day_expiry` | always | `same_day_expiry_excluded` (DTE 0, not allowed) |
| 5 | `quote_state` | `allow_locked_quote` | always | see §3 |
| 6 | `spread_availability` | `max_spread_relative` | set (usable quote) | `spread_relative_unavailable` |
| 7 | `spread_threshold` | `max_spread_relative` | set (relative spread present) | `spread_above_policy` |
| 8 | `delta_minimum` | `abs_delta_min` | delta pair set | `delta_unavailable` / `delta_outside_policy` |
| 9 | `delta_maximum` | `abs_delta_max` | delta pair set (delta usable) | `delta_outside_policy` |
| 10 | `greeks_time_basis` | `allow_unverified_time_basis` | delta pair set (delta usable) | `time_basis_unverified` |
| 11 | `iv_availability` | `require_iv` | `require_iv` | `iv_unavailable` |
| 12 | `iv_time_basis` | `allow_unverified_time_basis` | `require_iv` (IV present) | `time_basis_unverified` |
| 13 | `day_session` | `require_current_session_day` | true | `day_not_current_session` |
| 14 | `volume_threshold` | `min_volume` | set | `volume_below_policy` (null or below) |
| 15 | `open_interest_threshold` | `min_open_interest` | set | `open_interest_below_policy` (null or below) |
| 16 | `open_interest_time_basis` | `allow_unverified_time_basis` | `min_open_interest` set (value present) | `time_basis_unverified` |
| 17 | `multiplier_availability` | `max_premium_per_contract` | set (usable quote) | `multiplier_unavailable` |
| 18 | `premium_cap` | `max_premium_per_contract` | set (usable quote, multiplier present) | `premium_above_policy` |

A rule that needs a fact an earlier rule has shown to be absent is not evaluated for that contract. For example,
there are no spread rules without a usable quote, and no time-basis check without the value. All bounds are
inclusive, and all comparisons are exact `Decimal`.

**Rejection vocabulary (19 codes, frozen):**
- the 18 codes from 10A/10B: `side_mismatch`, `expiration_outside_policy`, `same_day_expiry_excluded`,
  `quote_unavailable`, `quote_one_sided`, `quote_crossed`, `quote_locked_excluded`, `quote_after_as_of`,
  `spread_relative_unavailable`, `spread_above_policy`, `delta_unavailable`, `delta_outside_policy`,
  `time_basis_unverified`, `iv_unavailable`, `volume_below_policy`, `open_interest_below_policy`,
  `day_not_current_session`, `premium_above_policy`;
- `multiplier_unavailable`, the one reason added by the approved 10C decision.

## 3. Rule details

**Quotes.**
- A quote is usable when `quote_state` is `complete`, or `locked` when `allow_locked_quote` is set.
- The unusable states map to reasons as follows:
  - `unavailable` gives `quote_unavailable`;
  - `bid_missing`, `ask_missing` and `both_missing` give `quote_one_sided`;
  - `crossed` gives `quote_crossed`;
  - a disallowed `locked` gives `quote_locked_excluded`;
  - `excluded_after_as_of` gives `quote_after_as_of`.
- The quote state is taken from OptionsIntelligence and never re-derived.

**Spread.** The rule is off when `max_spread_relative` is null. Otherwise, for a usable quote:
- a missing `spread_relative` (for example, zero mid) gives `spread_relative_unavailable`;
- `spread_relative > max_spread_relative` gives `spread_above_policy`.

There are no wide, tight or liquidity labels.

**Delta.** The rule is off when both bounds are null. Otherwise `|delta|` from the provider is compared to
`[abs_delta_min, abs_delta_max]`.
- A missing delta, or one listed in `greeks.out_of_bounds_fields`, is unusable and gives `delta_unavailable`.
- Delta is never clamped or recomputed.

**Timing.** When `allow_unverified_time_basis` is false, a fact whose time basis is not `observed_at` or
`provider_as_of_date` rejects with `time_basis_unverified`. This applies only to facts an enabled rule relies on:
- Greeks, when the delta rule is on;
- IV, when `require_iv` is set;
- open interest, when `min_open_interest` is set.

Untimed facts that no enabled rule uses never reject. A contract gets at most one `time_basis_unverified`.

**IV.** When `require_iv` is false, IV is not a gate. When it is true, a missing IV gives `iv_unavailable`. IV values
are never compared or ranked.

**Day/session.** When `require_current_session_day` is set, any `day_session_relation` other than
`current_session` gives `day_not_current_session`. That covers `previous_session`, `older_session` and
`unavailable`.

**Volume and open interest.** These use the phase9-v2 numeric facts.
- `current_session_volume` is null unless the day record is from the current session, so a previous-session volume
  never counts.
- A null value, or one below the minimum, gives `volume_below_policy` or `open_interest_below_policy`.
- There is no separate "missing" reason.

## 4. Premium cap (the 10C/10D boundary)

The cap is off when `max_premium_per_contract` is null. Otherwise, for a contract with a usable quote:

```
entry_reference_ask   = mid + spread_absolute / 2        (complete, or allowed locked, quotes)
max_loss_per_contract = entry_reference_ask * shares_per_contract
```

- A null `shares_per_contract` gives `multiplier_unavailable` for that contract only. The multiplier is never
  assumed (no 100), and the case is never mapped to `premium_above_policy` or failed for the whole assessment.
- `max_loss_per_contract > max_premium_per_contract` gives `premium_above_policy`.
- Phase 10D owns the rest of premium-risk evaluation and the invalidation check design (R5).

## 5. Candidates

Each candidate (long single-leg only) has three parts:

- **`source`:** facts copied from OptionsIntelligence, never recomputed.
  - Identity: `contract_id`, `provider_symbol`, `option_type`, `expiration`, `strike`, `dte_calendar_days`.
  - Quote: `quote_state`, `mid`, `spread_absolute`, `spread_relative`.
  - Greeks and IV: `delta`, `greeks_time_basis`, `implied_volatility`, `iv_time_basis`.
  - Activity: `volume_state`, `open_interest_state`, `day_session_relation`.
  - phase9-v2: `current_session_volume`, `open_interest_value`, `open_interest_time_basis`, `shares_per_contract`.
    These are null for phase9-v1 input.
- **`derived`:**
  - `entry_reference_ask` and `max_loss_per_contract`, as in §4;
  - `premium_risk_status`: `computed`. A candidate with no multiplier gets `multiplier_unavailable` and a null
    `max_loss_per_contract`. That happens with phase9-v1 input, or phase9-v2 with a null multiplier when the
    premium cap is off.
- **`policy_checks`:** the enabled rules, in the frozen order, each `{rule, result: "pass", policy_field,
  source_pointers}`. A candidate never carries a failed check.

**Canonical order:** expiration ascending, then numeric strike ascending, then `contract_id`. This is an ordering
for determinism only. It implies no preference.

## 6. Rejections

Rejections are aggregated by reason: `[{reason_code, count, contract_ids}]`.
- They are sorted by `reason_code`.
- Each `contract_ids` list is sorted and unique, and `count = len(contract_ids)`.
- A rejected contract appears under every reason it fails, and never among the candidates.
- Candidates and rejected contracts together partition the whole chain, including opposite-side contracts
  (`side_mismatch`).

## 7. Outcome and decision trace

| Situation | outcome | step 8 `contract_screening` |
|---|---|---|
| A global gate failed | `no_setup`, the failed-gate reasons | `not_evaluated` / `global_gate_failed` |
| Candidates found | `setup_candidates`, `no_setup_reasons = []` | `pass` / `candidates_available` |
| No candidate | `no_setup`, including `no_candidate_satisfies_policy` | `fail` / `no_candidate_satisfies_policy` |

A screening `no_setup` adds the following reasons, derived only from the actual results:
- `execution_data_unavailable` when no eligible-side contract has a usable two-sided quote;
- `source_timing_unverified` when at least one eligible-side contract's only rejection is `time_basis_unverified`.

Steps 1–7 are the Phase 10B global trace, unchanged. Step 8's pointers are `oi:contracts[*]` plus `policy:<field>`
for every enabled screening rule. The trace never lists contract ids.

`execution_readiness` is the unchanged 10B block. Screening results live only in `candidates` and `rejections`.

## 8. Validation and re-derivation

`validated_assessment` checks the following:
- the two-value status;
- a global-gate `no_setup` (no candidates or rejections, `global_gate_failed`);
- for a screened assessment:
  - step 8 against the outcome, and its pointers against the policy;
  - `setup_candidates` has candidates and no reasons, while a screening `no_setup` has no candidates and
    `no_candidate_satisfies_policy`;
  - the candidate structure, with the side equal to the bias side;
  - **every candidate re-screened from its own source facts** against the policy, with checks equal to the
    enabled rules and the derived facts recomputed;
  - canonical candidate order;
  - rejection structure, order and counts;
  - no candidate among the rejections, and the partition of the contract count;
  - provenance.

`verify_assessment` rebuilds from the MarketIntelligence, the OptionsIntelligence and the embedded policy, and
requires a byte-identical body. It never repairs.

## 9. Scale and current entitlement

Synthetic quoted chains, built through the real Phase 9 pipeline:

| Contracts | assess | verify | candidates | rejected ids (sum over reasons) | output |
|---|---|---|---|---|---|
| 3,880 | 0.58 s | 0.74 s | 441 | 5,228 | 1.22 MB |
| 7,780 | 1.18 s | 1.49 s | 882 | 10,488 | 2.43 MB |
| 10,000 | 1.52 s | 1.90 s | 1,125 | 13,500 | 3.11 MB |

Screening is linear in the number of contracts.

**Live current plan.** The live chains have no quotes (the Starter plan). The local Phase 9E META (7,780 contracts)
and NVDA (3,886) intelligence files give `no_setup` / `execution_data_unavailable` at global gate 7, so screening
never runs. Live data therefore cannot show that screening produces candidates. The synthetic quoted fixtures are the
authoritative 10C tests.

## 10. What remains

- **10D:** premium-risk evaluation beyond the cap, and the `InvalidationCheck` design (R5).
- **10E:** the runner and live validation. A quote entitlement is needed before live candidates are possible.
