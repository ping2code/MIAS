# Phase 10B — Trade Setup core

**Status:** implemented on `claude/phase10b-trade-setup-core`; not merged. **Phase 10 is not complete.** Contract
screening (10C), premium-risk facts (10D) and the runner (10E) do not exist yet.

> Phase 10B adds the pure `trade_setup` package: the explicit policy, structural validation of both inputs, the
> market-bias derivation, the global no-setup gates and the `TradeSetupAssessment` shell. It also adds the bounded
> `phase9-v2` OptionsIntelligence compatibility amendment (D2). No contract is screened, selected or ranked, and there
> is no score, confidence, sizing, target, stop or reward/risk.

## 1. Scope (locked decisions from the Phase 10A freeze)

| Decision | Phase 10B behaviour |
|---|---|
| D1 | Long single-leg calls and puts only. The side follows from the bias (bullish→call, bearish→put). |
| D2 | Additive `phase9-v2` / `phase9-rules-v2` OptionsIntelligence (section 2). This is a Phase 10 compatibility amendment, not a new Phase 9 sub-phase. Phase 9 stays complete. |
| D3 | Strict bias mapping (section 5). |
| D4 | The policy is always required. The engine has no defaults. |
| D5 | `allow_unverified_time_basis` is explicit (used by 10C screening). |
| D6 | `max_input_gap_seconds` is required. |
| D7 | No underlying-price logic in 10B. |
| D9 | No ranking. Canonical contract order is reserved for 10C. |
| D10 / R5 | Only the market-state invalidation descriptor. The `InvalidationCheck` design is deferred to 10D. |
| D11 | Structural MarketIntelligence validation only. There are no upstream imports. |
| D12 | SEC filing presence never blocks. |
| R1 | `block_on_market_context_opposition` and `block_on_market_context_not_current` are kept. |
| R2 | `underlying_price_unavailable` is not a reason. |
| R3 | `abs_delta_min` / `abs_delta_max`. |
| R4 | A `phase9-v1` input with `min_volume`, `min_open_interest` or `max_premium_per_contract` enabled raises `policy_requires_phase9_v2`. This is an error, not no_setup. |

## 2. The `phase9-v2` OptionsIntelligence amendment

`options_intelligence.builder.build(..., format="phase9-v2")` emits the same 13 top-level fields as `phase9-v1`. Each
contract adds exactly four facts:

| Field | Source |
|---|---|
| `current_session_volume` | `day.volume`, only when `day.session_relation == "current_session"`, otherwise `null` (the locked volume semantics) |
| `open_interest_value` | the snapshot `open_interest.value` |
| `open_interest_time_basis` | the snapshot `open_interest.time_basis` |
| `shares_per_contract` | the snapshot `terms.shares_per_contract` |

- `phase9-v1` output is byte-identical to before. The v1 goldens are unchanged and all Phase 9 tests pass.
- Validation requires the rules version to match the format, and contract fields to match the format exactly (v1
  contracts carry none of the four fields).
- `verify_against_snapshot` rebuilds at the embedded format.
- The v2 goldens are in `tests/fixtures/options_intelligence_v2/`.
- The one change to an existing test: the Phase 9D replay tamper label changed from `phase9-v2` to `phase9-v9`,
  because `phase9-v2` is now a supported format.

## 3. Policy (`phase10-policy-v1`)

`trade_setup.policy.make_policy(**rules)` requires exactly these 18 rule fields, validates them and seals them with
`policy_format_version` and `policy_id` (`sha256:` over the canonical body):

| Field | Type |
|---|---|
| `allowed_sides` | sorted, unique, non-empty subset of `call`, `put` |
| `min_dte`, `max_dte` | ints, `0 <= min_dte <= max_dte` |
| `allow_same_day_expiry`, `allow_locked_quote`, `require_iv`, `allow_unverified_time_basis`, `require_current_session_day`, `require_complete_chain`, `block_on_market_context_opposition`, `block_on_market_context_not_current` | exact bools |
| `max_spread_relative` | non-negative canonical decimal string or `null` |
| `abs_delta_min`, `abs_delta_max` | both `null`, or canonical decimal strings with `0 <= min <= max <= 1` |
| `min_volume`, `min_open_interest` | non-negative int or `null` (null disables the rule) |
| `max_premium_per_contract` | positive canonical decimal string or `null` |
| `max_input_gap_seconds` | non-negative int |

Unknown or missing fields, floats, non-canonical decimals and a tampered `policy_id` are all errors. In 10B, only
the gate fields are evaluated. The screening fields are validated and carried for 10C.

## 4. Input validation (local and structural)

`trade_setup.validation` imports only the standard library and `trade_setup`. It checks the following against
frozen local copies of the upstream constants. The tests prove the copies equal the real `market_intelligence`,
`evidence_synthesis`, `options_intelligence`, `options_data` and `market_data` definitions.

**MarketIntelligence (`phase8-v1` / `phase8-rules-v1`):**
- exactly 13 keys and `synthesis_ref`;
- a valid symbol and tz-aware `as_of`;
- the closed pattern, technical_status, conflict and attention vocabularies;
- a recomputed `intelligence_id`.

**OptionsIntelligence (`phase9-v1` or `phase9-v2`):**
- exactly 13 keys and `snapshot_ref`;
- the format and rules version pairing;
- contract fields exact per format;
- the quote, activity, session and time-basis vocabularies;
- a recomputed `options_intelligence_id`.

**Compatibility:**
- The MI `synthesis_ref.symbol` must equal the OI `snapshot_ref.underlying`, otherwise it is an error.
- `assessment_as_of = max(MI as_of, OI as_of)` in UTC.
- `input_gap_seconds` is the whole-second absolute difference.

Invalid input always raises `TradeSetupInputError`. It is never converted to no_setup.

## 5. Market bias (D3)

Market bias is derived from MI `timeframe_structure.pattern`. The result is `insufficient` whenever
`evidence_coverage.technical_status != "available"`.

| Pattern | State | Side | Reason if not directional |
|---|---|---|---|
| `all_bullish` | `bullish` | `call` | — |
| `all_bearish` | `bearish` | `put` | — |
| `opposed` | `conflicting` | — | `market_evidence_conflicting` |
| `incomplete` | `insufficient` | — | `market_evidence_insufficient` |
| `all_non_directional` | `non_directional` | — | `market_evidence_non_directional` |
| `partially_directional` | `partially_directional` | — | `market_evidence_partially_directional` |

A directional bias carries `invalidation = {rule: "pattern_must_remain", required_pattern, established_by:
intelligence_id}`. This descriptor is the only market-state invalidation in 10B.

## 6. Global gates and `decision_trace`

The gates are always evaluated in this order. Each is recorded as `{step, rule, result, reason, pointers}`, where
`result` is one of `pass`, `fail` or `not_evaluated`. Pointers use `phase10-pointer-v1` (`mi:` / `oi:` / `policy:`
paths).

| # | Rule | Fails with |
|---|---|---|
| 1 | `input_contemporaneity` | `inputs_not_contemporaneous` when the gap exceeds `max_input_gap_seconds` |
| 2 | `market_bias` | the bias reason (section 5) |
| 3 | `side_allowed` | `side_not_allowed_by_policy` (`not_evaluated`/`market_bias_not_directional` if no side) |
| 4 | `context_opposition_gate` | `context_gate_blocked` when enabled and MI attention has `market_context_opposition_present` |
| 5 | `context_current_gate` | `context_gate_blocked` when enabled and MI attention has `market_context_not_current` |
| 6 | `chain_completeness` | `options_chain_truncated` when `require_complete_chain` and the chain is truncated |
| 7 | `execution_data_readiness` | `execution_data_unavailable` when no contract in the chain has a usable two-sided quote (`complete`, or `locked` if allowed) |
| 8 | `contract_screening` | always `not_evaluated` / `deferred_to_phase10c` |

Disabled gates are `not_evaluated` / `policy_disabled`. SEC filing presence is never consulted.

- Gate 7 is a chain-level fact, not per-contract screening. On the live Starter plan (no quotes), every run is
  `no_setup` with `execution_data_unavailable` (D8).
- `source_timing_unverified` and `no_candidate_satisfies_policy` are in the locked reason set but are only reachable
  once 10C screening exists.

## 7. Outcome and the assessment shell (`phase10-v1` / `phase10-rules-v1`)

`TradeSetupAssessment` has exactly 12 top-level fields:
- `assessment_format_version`, `assessment_id`, `rules_version`;
- `policy`, the embedded sealed policy;
- `inputs`, the MI and OI refs, symbol, `assessment_as_of` and `input_gap_seconds`;
- `outcome`;
- `market_bias`;
- `execution_readiness`: the OI format, contract count, truncation, quote-state / Greeks / IV time-basis /
  day-session counts, `usable_two_sided_quote_count`, `numeric_activity_facts_available` and
  `shares_per_contract_present_count` (`null` for v1);
- `candidates`, `rejections` and `decision_trace`;
- `provenance`.

`outcome.status` is one of the following:
- `no_setup`, with the sorted, unique failed-gate reasons;
- `contract_screening_pending`, a **Phase 10B interim status** that means every global gate passed and screening has
  not run;
- `setup_candidates`, which is reserved for 10C and rejected by the 10B validator.

`candidates` and `rejections` are always `[]` in 10B. The `phase10-rules-v1` label covers this interim behaviour.
10C will introduce screening under its own rules label.

## 8. Canonical form, ids and verification

The canonical form and ids follow the earlier phases:
- canonical JSON uses sorted keys, compact separators, ASCII and no NaN;
- there are no floats, and decimals are canonical strings;
- there is no `generated_at`;
- `assessment_id` is `sha256:` over the body without the id.

`validated_assessment(data)` checks structure, id, policy, trace order and consistency, outcome/trace agreement and
provenance. `verify_assessment(data, mi, oi)` rebuilds from the inputs with the embedded policy. It names the first
differing top-level fields.

## 9. Boundaries

`trade_setup` performs no I/O. It has no clock, environment, network, files, database, subprocess or AI access, and
it imports only the standard library and itself. This is verified by:
- a sealed run;
- an AST scan;
- a fresh-process check that none of `market_intelligence`, `options_intelligence`, `options_data`, `market_data`,
  `evidence_*`, `persistence`, `sqlalchemy` or `requests` is loaded.

Output is identical across the following, and the tests cover each:
- 100 repeats;
- dict-order variation;
- fresh processes with different `PYTHONHASHSEED`, cwd, `TZ`, `LANG`, `HOSTNAME` and unrelated environment.

## 10. Tests

| Module | Tests |
|---|---|
| `tests/test_trade_setup_core.py` | policy, inputs, bias, gates, v1/v2 compatibility, shell |
| `tests/test_trade_setup_boundary.py` | tamper matrices, determinism, cross-process, no-I/O, import boundary, frozen upstream constants, forbidden semantics |
| `tests/test_options_intelligence_v2.py` | the `phase9-v2` amendment |
| `tests/trade_setup_cases.py` | test-only sealed inputs built from real Phase 8 / Phase 9 cases |
