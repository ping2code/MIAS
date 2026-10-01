# Phase 11 — Prospective setup evaluation

**Status:** implemented on `claude/phase11-setup-evaluation` using synthetic and replay inputs only; not merged.
**Prospective collection has NOT started, and the production protocol is NOT activated** (§9). Phase 10 live
regular-session validation is still pending, so Phase 10 is open.

> Phase 11 is a descriptive, deterministic, replayable record of what each frozen Phase 10 candidate's quote showed
> at fixed later session horizons.
>
> It never creates setups, ranks, selects, labels (no win, loss, success or failure), averages, tunes, sizes, sets
> targets or stops, or recommends. It makes "how did setups perform under the frozen rules?" answerable later. It
> never answers "which rules should change?"; any such research would need separate authorization after the
> relevant prospective gate.

## 1. Contract

There is one `SetupEvaluation` (`phase11-v1` / `phase11-rules-v1` / `phase11-pointer-v1`) per **(assessment,
horizon)**. It contains **every** original candidate in the Phase 10 canonical order (expiration, numeric strike,
`contract_id`).

It has exactly 13 fields, with no `generated_at`:

| Field | Contents |
|---|---|
| `evaluation_format_version`, `evaluation_id`, `rules_version` | `evaluation_id` = `sha256:` over the canonical body without it |
| `protocol_ref` | `protocol_format_version`, `protocol_id`, `purpose` (`test` until activation) |
| `setup_ref` | `assessment_id`, `assessment_format_version`, `assessment_rules_version`, `policy_id`, `symbol`, `side`, `assessment_as_of`, `candidate_count` |
| `observation_ref` | `snapshot_id`, `snapshot_format_version`, `underlying`, `as_of`, `session_date`, `truncated` |
| `schedule_ref` | `schedule_id`, `schedule_format_version` (`phase11-sessions-v1`) |
| `horizon` | `name`, `target_session_date`, `target_open`, `target_close`, `window_start` |
| `candidate_outcomes` | per candidate: `contract_id`, `entry_reference_ask`, `shares_per_contract`, `observed_quote`, `outcome_status`, `liquidation_reference_bid`, `premium_change`, `premium_return`, `return_status`, `dollar_change_per_contract`, `dollar_status` |
| `summary` | `candidate_count`, `outcome_status_counts`, `premium_change_sign_counts` {positive, negative, zero} (observed candidates only) |
| `invalidation_relation` | `status`, `check_ids`, `invalidating_check_ids` |
| `decision_trace`, `provenance` | fixed pass steps with pointers; the ids of every input and the versions |

## 2. Inputs (all sealed, all explicit)

- A Phase 10 **TradeSetupAssessment** with `setup_candidates`. A `no_setup` assessment is an input error.
- A later **OptionsSnapshot** (`phase9-snapshot-v1`), used directly. It carries per-quote `observed_at` and
  `time_basis`, the cutoff, the session and `truncated`. OptionsIntelligence lacks per-quote times, so it isn't
  used. There is no new observation object.
- A **SessionSchedule** (`phase11-sessions-v1`): a content-addressed list of `{session_date, regular_open,
  regular_close}`, built from `market_data.calendar` outside the core. For a final evaluation, build it after the
  target date. The sealed schedule is authoritative. It must start at or before `assessment_as_of`.
- An **evaluation protocol** (`phase11-evaluation-protocol-v1`). See §9.
- Optionally, Phase 10D **InvalidationChecks** for the same assessment.

Each input is validated **locally and structurally**, with exact keys, versions and a recomputed content id. The
pure core imports only the standard library and `setup_evaluation`; tests pin the local constants to the upstream
packages.

## 3. Horizons and window

- `session_1` is the **first** regular XNYS session whose `regular_open` is strictly later than `assessment_as_of`.
  `session_5` is the **fifth** such session. Only full forward sessions count.
  - Examples: Wednesday 14:00 ET → Thursday; Wednesday after close → Thursday; Thursday 08:00 ET → Thursday;
    Friday after close → Monday.
  - An assessment exactly at an open skips that session.
  - Early closes come from the schedule (for example, 13:00 ET on 2026-11-27).
- The **window** is `[target_close − 30 min, target_close]`, inclusive and backward-looking only. The snapshot's
  `as_of` must lie in it, be later than `assessment_as_of`, and fall in a **regular** session on the target date.
  Anything else is an input error, and nothing is sealed.
- There is no nearest-observation search and no after-close use. Collection is one scheduled snapshot per window.

## 4. Outcomes

Each candidate's status is decided in this order:
1. `expired_before_target`, when the target date is after the expiration. A target date equal to the expiration is
   evaluated normally, and a closing bid is never treated as settlement.
2. A missing contract:
   - in a **complete** chain gives `contract_absent`;
   - in a **truncated** chain gives `observation_incomplete`. A truncated chain cannot prove absence.
3. The copied quote:
   - `quote_unavailable`: missing or unavailable;
   - `quote_after_cutoff`: excluded after `as_of`;
   - `quote_timing_unverified`: time basis isn't `observed_at`;
   - `quote_not_two_sided`;
   - `quote_crossed`;
   - `quote_stale`: `observed_at` before the window;
   - otherwise **`observed`**. Locked quotes and a bid of `0` are valid.

**Only `observed` carries values:**
- `liquidation_reference_bid`: the later bid. Neither it nor `entry_reference_ask` is ever called a fill.
- `premium_change = liquidation_reference_bid − entry_reference_ask`, an exact Decimal.
- `premium_return = premium_change / entry_reference_ask`, exactly **8 places, ROUND_HALF_EVEN**. It is null with
  `entry_reference_zero` when the entry reference is 0.
- `dollar_change_per_contract = premium_change × shares_per_contract`, using the Phase 10 candidate's multiplier
  (never assumed). It is null with `multiplier_unavailable`, or with `contract_terms_changed` when the later
  snapshot's multiplier differs.

No midpoint metric, fees, contract count or portfolio P&L. Censored or missing states are never converted into gains
or losses.

**Pending or missed windows** are not object states. An evaluation exists only once a valid observation does.
Pending and missed reporting needs the current time, so it belongs to operator tooling outside the core.

## 5. Invalidation relation (optional, descriptive)

- `invalidated_by_target`: a supplied check for this assessment is `invalidated` with an MI `as_of` at or before
  `target_close`.
- `no_invalidation_observed`: checks were supplied and none qualifies. This covers **supplied checks only**; it is
  not proof that continuous monitoring found nothing.
- `not_evaluated`: no checks were supplied.

The check ids are recorded. Invalidation never changes outcomes and is never converted into profit or loss, and
nothing implies causation.

## 6. Deferred in v1

Expiration settlement, MFE/MAE (endpoint-only in v1), all labels, averages and session_10.

## 7. Validation, re-derivation and determinism

- `validated_evaluation` checks:
  - schemas, versions, ids and refs;
  - the protocol purpose, with production rejected until activation;
  - symbol, time ordering, the window and target-session consistency;
  - candidate count and uniqueness;
  - every outcome's status and values, **re-derived from its own copied facts**;
  - the summary, the relation, the trace and provenance.
- `verify_evaluation` rebuilds from the assessment, snapshot, schedule, protocol and checks, and requires identical
  bytes. That catches resealed self-consistent forgeries, such as a better bid or an altered entry reference, and any
  omitted or reordered candidate.
- Deterministic across 100 repeats, dict order, and fresh processes with varied `PYTHONHASHSEED`, cwd, `TZ`, `LANG`,
  `HOSTNAME` and environment.
- **Performance:** 1,125 candidates against a 10,000-contract snapshot take 0.55 s to evaluate and 0.64 s to verify.
  The output is about 435 B per candidate.

## 8. Runner (local replay)

```bash
python -m setup_evaluation.runner evaluate --assessment A.json --snapshot SNAP.json --schedule SCHED.json --protocol PROTOCOL.json --horizon session_1 --output EVAL.json
```

```bash
python -m setup_evaluation.runner build-schedule --from 2026-09-28 --through 2026-12-31 --output SCHED.json
```

```bash
python -m setup_evaluation.runner test-protocol --output PROTOCOL.json
```

- `evaluate` optionally takes `--invalidation-check CHECK.json` (repeatable) and `--overwrite`.
- Outputs are atomic and no-clobber, and verified before being written.
- Summaries are metadata only: no quotes, marks, returns or contract lists.
- `verify --evaluation EVAL.json` plus the same inputs re-verifies a sealed evaluation (structure and a byte-identical
  rebuild) and writes nothing.
- **Production schedule completeness (activation hardening):**
  - Under a `production` protocol, `evaluate` and `verify` first regenerate the XNYS sessions for the supplied
    schedule's exact first-through-last dates, and require an identical sealed schedule. Otherwise:
    `schedule does not match the XNYS calendar`.
  - This catches omitted middle sessions (one would silently move `session_5`), added non-sessions, altered opens,
    closes or early closes, and reordered or duplicate rows, none of which a content-addressed schedule can detect by
    itself.
  - The pure core stays calendar-free.
  - Test protocols accept any valid sealed schedule, so synthetic fixtures remain possible.
- Exit codes are 0/2/3/4, as in `trade_setup.runner`.
- Only `build-schedule`, and the production calendar check in `evaluate`/`verify`, load the calendar.

## 9. Protocol and production activation

- v1 implements one semantics, so a protocol's semantic fields must equal the frozen rules. Changing the window,
  adding a horizon and similar edits are rejected.
- The protocol records every frozen semantic explicitly:
  - `evaluation_format_version` `phase11-v1`, `rules_version` `phase11-rules-v1`, `schedule_format_version`
    `phase11-sessions-v1`;
  - the horizons, horizon rule, window, mark, entry, quote requirements, return quantization, status vocabularies,
    all-candidate inclusion and the invalidation rule;
  - `missing_contract_rule` `contract_absent_if_complete_chain_else_observation_incomplete`;
  - `multiplier_rule` `assessment_candidate_multiplier_never_assumed`;
  - `exclusions`, sorted: `expiration_settlement`, `labels`, `mae`, `mfe`, `portfolio_pnl`, `position_sizing`,
    `ranking`, `retrospective_selection`.
- `make_test_protocol()` produces `purpose: test` with no `prospective_start`, for fixtures and replay only. Its id
  is `sha256:1eebe890…`; the pre-hardening test id `sha256:188119d4…` is retired and never reused. **A test hash is
  never a production hash.**
- **Prospective boundary (activation hardening):** under a production protocol, `evaluate` requires
  `assessment_as_of >= prospective_start`; it is accepted exactly at the start. `validated_evaluation` rejects any
  sealed production evaluation whose assessment precedes the pinned start, so re-derivation can't accept one either.
  `prospective_start` must be canonical UTC (`+00:00`, round-trip exact), leaving no timezone ambiguity. Nothing made
  before activation is ever evaluated or backfilled.
- **The production protocol is not activated.** `rules.PRODUCTION_PROTOCOL_ID` and
  `rules.PRODUCTION_PROSPECTIVE_START` are `None`, and every `purpose: production` protocol is rejected.
- **Activation is a separate step, only after Phase 10 live regular-session validation closes successfully.** It:
  1. writes the final production `phase11-evaluation-protocol-v1` JSON;
  2. pins its SHA-256 id;
  3. pins an explicit `prospective_start`;
  4. records the activation commit;
  5. begins prospective collection only after that timestamp.

  This is an operational gate, not tuning. **No return is inspected before activation.**

## 10. Collection boundary (prospective workflow, after activation)

1. For each `setup_candidates` assessment after `prospective_start`, schedule one `options_data.runner` snapshot
   inside each horizon window (about 15 minutes before the target close; `OPTIONS_DATA_DELAY_SECONDS` unset). This
   is operator-run, and scheduling comes later.
2. After the target date, build the SessionSchedule.
3. Run `setup_evaluation.runner evaluate`.
4. Missed windows stay missing. There is no backfill, no historical-quote substitution and no nearest-observation
   search.

## 11. Phase 6 isolation

| | Phase 6 | Phase 11 |
|---|---|---|
| Purpose | Technical prospective evidence validation | Trade-setup prospective outcome evaluation |
| Package | `evidence/`, `evaluation/` | `setup_evaluation/` |
| Protocol and hashes | registry, protocol, H1′, H2′, pin | `phase11-evaluation-protocol-v1` (not yet activated) |
| Start | 2026-09-28; earliest evaluation 2027-03-29; 120 sessions; gate locked | after activation only |

The two are physically and logically separate. Phase 11 never reads `evidence/`, `evaluation/`, forward returns or
prospective outcomes, and nothing in Phase 6 changes.
