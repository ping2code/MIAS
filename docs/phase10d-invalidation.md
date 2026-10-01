# Phase 10D — Risk facts and market-state invalidation

**Status:** implemented on `claude/phase10d-invalidation-check`; not merged. **Phase 10 is not complete:** the runner
and live validation (10E) do not exist yet.

> Phase 10D adds one separate, content-addressed object, `InvalidationCheck`. It answers one question: does a newer
> MarketIntelligence still show the market state that established a setup?
>
> Phase 10D adds no candidate risk fields and never mutates a `TradeSetupAssessment`. It leaves `phase10-v1`,
> `phase10-rules-v1`, `phase10-policy-v1`, contract screening and the candidate schema unchanged.

## 1. Risk facts

**Retained (frozen in 10C).** These are the only risk facts in Phase 10 v1:
- the candidate derived facts `entry_reference_ask`, `max_loss_per_contract` and `premium_risk_status`
  (`computed` | `multiplier_unavailable`, with the A/B/C semantics in `docs/phase10c-contract-screening.md`);
- the `multiplier_unavailable` rejection when the premium cap is on;
- the policy premium cap.

`entry_reference_ask` is an ask-side entry reference, not a fill. `max_loss_per_contract` is the premium of one long
contract (its maximum loss held to expiry) and excludes fees and commissions.

**Assessed and not added** (redundant, misleading or unsupported):

| Field | Why not added |
|---|---|
| premium per share | It is `entry_reference_ask` |
| multiplier | It is `source.shares_per_contract` |
| total premium | It is `max_loss_per_contract` |
| percent of cap, distance to cap | Invite comparing candidates, a ranking in disguise |
| execution score | The quote facts are already source facts; any composite would be a score |
| quote staleness | OptionsIntelligence does not carry quote age |
| breakeven | An underlying price level; no price-level framework is approved |

**Deferred:**
- fees and commissions;
- quote staleness (needs the quote entitlement and an OptionsIntelligence change);
- breakeven and any price levels;
- position sizing: Phase 10 v1 never infers a number of contracts, and sizing would need explicit account capital
  and risk budget inputs;
- targets, stops, reward/risk, expected return and probability of profit.

## 2. InvalidationCheck

```
check_invalidation(assessment, market_intelligence)   -> InvalidationCheck (sealed)
validated_invalidation(check)                         -> structural validation
verify_invalidation(check, assessment, mi)            -> rebuild, byte-identical
```

Versions:
- `invalidation_format_version`: `phase10-invalidation-v1`;
- `rules_version`: `phase10-invalidation-rules-v1`;
- pointer version: `phase10-invalidation-pointer-v1`, with `setup:` and `mi:` prefixes. The assessment's
  `phase10-pointer-v1` is unchanged.

**Schema** (exactly 12 top-level fields, no `generated_at`):

| Field | Contents |
|---|---|
| `invalidation_format_version`, `invalidation_id`, `rules_version` | `invalidation_id` = `sha256:` over the canonical body without it |
| `setup_ref` | `assessment_id`, `assessment_format_version`, `assessment_rules_version`, `policy_id`, `side`, `assessment_as_of`, `established_by`, `established_as_of` (no contract ids) |
| `market_intelligence_ref` | `intelligence_id`, `intelligence_format_version`, `rules_version`, `symbol`, `as_of` |
| `symbol` | the setup symbol, equal to the MI symbol |
| `required_market_state` | `rule: pattern_must_remain`, `required_pattern` (`all_bullish` / `all_bearish`), `required_technical_status: available` |
| `observed_market_state` | the new MI's `pattern` and `technical_status`, copied (frozen Phase 8 vocabularies) |
| `result`, `reason` | see §3 |
| `decision_trace` | see §5 |
| `provenance` | `assessment_id`, `market_intelligence_id`, `established_by`, `rules_version`, `pointer_version` |

Invalidation applies to the whole setup's market-state requirement, not to individual contracts. The output is
2,310 bytes whether the setup has 1 candidate or 1,125.

## 3. Results and reasons

| Observed (new MI) | result | reason |
|---|---|---|
| `technical_status` ≠ `available` | `not_evaluable` | `technical_evidence_not_available` |
| `technical_status` = `available`, pattern `incomplete` | `not_evaluable` | `timeframe_evidence_incomplete` |
| pattern = `required_pattern` | `holds` | `required_pattern_present` |
| any other pattern | `invalidated` | `required_pattern_absent` |

For a **bullish/call** setup (`all_bullish` required):
- `all_bullish` gives `holds`;
- `all_bearish`, `opposed`, `all_non_directional` and `partially_directional` give `invalidated`;
- `incomplete`, or technical evidence `partial` or `unavailable`, gives `not_evaluable`.

**Bearish/put** setups are the mirror image, with `all_bearish` required and `all_bullish` giving `invalidated`.

**Only `holds` confirms** that the required market state still exists. `not_evaluable` means the evidence needed to
observe the state is missing. That is not proof of invalidation, and it is not a confirmation either. Results are
named `holds` rather than `valid` so they are never confused with input validity.

## 4. Time ordering and input errors

The new MI's symbol must equal the setup symbol. Its `as_of` must be **strictly later** than `established_as_of`,
the `as_of` of the MarketIntelligence that established the setup. The anchor is that MI, not `assessment_as_of`.
There is no maximum gap.

These inputs are errors (`TradeSetupInputError`). Nothing is sealed for them, and they are never `holds`,
`invalidated` or `not_evaluable`:
- an invalid or tampered assessment, including an inconsistent invalidation descriptor (§7);
- an outcome other than `setup_candidates`, or a `setup_candidates` assessment with zero candidates;
- an unsupported or tampered MarketIntelligence;
- a symbol mismatch;
- an MI `as_of` equal to `established_as_of` (including the establishing MI itself) or older.

## 5. Decision trace

There are five fixed steps:
1. `symbol_match` (pass);
2. `as_of_order` (pass);
3. `technical_evidence`: pass, or fail with `technical_evidence_not_available`;
4. `timeframe_completeness`: pass, fail with `timeframe_evidence_incomplete`, or `not_evaluated`;
5. `pattern_match`: pass with `required_pattern_present`, fail with `required_pattern_absent`, or `not_evaluated`.

Steps 1–2 always pass in a sealed object, because a failure there is an input error. A `not_evaluated` step has
the reason `earlier_step_failed`. Pointers look like `mi:timeframe_structure.pattern` or
`setup:market_bias.invalidation.required_pattern`.

## 6. No reactivation; assessment immutability

`InvalidationCheck` is stateless and point-in-time. Example: at T1 the check says `invalidated`, and at a later T2
it says `holds`. **T2 does not revive the setup.** The rule for downstream workflows is that the **earliest
`invalidated` check for a setup is terminal**. A new eligible market state needs a new `TradeSetupAssessment`. A
check also cannot see departures between two MarketIntelligence snapshots.

v1 has no chaining to earlier checks and no lifecycle persistence. The `TradeSetupAssessment` is never patched:
there is no `status = invalidated` field. The check is a separate object that references the setup by id.

## 7. Assessment-validator hardening

`validated_assessment` now also checks, structurally:
- the market bias re-derives (D3) from its copied pattern and technical status;
- a directional bias carries exactly `{rule, required_pattern, established_by}`, where:
  - `rule` is `pattern_must_remain`;
  - `required_pattern` matches the bias (bullish→`all_bullish`, bearish→`all_bearish`) and the bias pattern;
  - technical status is `available`;
  - `established_by` equals both `inputs.market_intelligence_ref.intelligence_id` and
    `provenance.market_intelligence_id`;
- a non-directional bias carries no descriptor.

This is validation hardening only. Builder output is byte-identical, and no version changes.

## 8. Validation and re-derivation

`validated_invalidation` checks:
- the 12 keys, the versions and the id;
- the ref shapes and versions, and the symbols;
- strict time ordering;
- the required state against the setup side;
- the observed vocabularies;
- result, reason and the whole trace, **re-derived from the object's own required and observed state**;
- pointer syntax and provenance.

`verify_invalidation` rebuilds from the assessment and the MarketIntelligence and requires an identical body. That
catches self-consistent, resealed forgeries, such as a false observed state or a swapped MI id. Nothing is ever
repaired.

## 9. Boundaries, determinism and performance

- **Pure:** no clock, environment, files, database, network, subprocess or AI. It imports only the standard library
  and `trade_setup`; MarketIntelligence is validated locally and structurally. It uses no prices, P&L, premium,
  targets or stops, and no new provider data.
- **Deterministic:** identical bytes over 100 repeats, under dict-order variation, and in fresh processes with
  different `PYTHONHASHSEED`, cwd, `TZ`, `LANG`/`LC_ALL`, `HOSTNAME` and unrelated environment.
- **Performance:** the check itself takes constant time, and its output size is constant. Validating the input
  assessment is linear in its size, because it verifies the sealed setup.

| Setup | Candidates | Setup size | check | verify | output |
|---|---|---|---|---|---|
| 2 contracts | 1 | 5.6 KB | 0.001 s | 0.001 s | 2,310 B |
| 8,000 contracts | 900 | 2.05 MB | 0.14 s | 0.15 s | 2,310 B |
| 10,000 contracts | 1,125 | 2.56 MB | 0.18 s | 0.18 s | 2,310 B |

## 10. What remains for 10E

- A local runner for assessment and invalidation replay (files in, canonical files out; no network for these steps).
- The quote-entitlement upgrade, then live validation of candidate generation.
- Live MarketIntelligence produced alongside options snapshots, to feed `check_invalidation`.
