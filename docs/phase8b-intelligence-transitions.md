# Phase 8B — Market Intelligence transitions

**Status:** implemented on `claude/phase8b-intelligence-transitions`; not merged.
**Format:** `phase8-v1` (unchanged) · **Rules:** `phase8-rules-v1` (unchanged)

> **Transitions describe differences between two cutoff-safe EvidenceSynthesis objects. They do NOT predict
> what happens next.** MarketIntelligence remains a description of evidence, not a forecast, recommendation,
> signal or trading decision.

## 1. Purpose

`build(current, previous=None)` accepts an optional, explicit previous EvidenceSynthesis. When it is supplied,
the builder populates the two fields Phase 8A reserved:

- `comparison`;
- `transitions`.

The schema is unchanged: the same 13 top-level fields. There is no new format and no new rules version.

- **Why the same rules version:** the rules version is part of the hashed body. A new version would change every
  current-only `intelligence_id`, and current-only output must stay byte-identical to Phase 8A. The fields were
  reserved in `phase8-v1` from the start.
- **Previous input:** it is supplied by the caller. There is no database lookup, no implicit "latest", no file
  lookup in the builder, no clock, no network and no environment dependence.

## 2. Comparison validation (fail closed)

`validated_pair` first fully validates each synthesis on its own, with every Phase 8A check including
re-derivation. It then requires:

| Check | Message |
|---|---|
| Two different syntheses | `previous and current synthesis are the same synthesis` |
| Same symbol | `previous synthesis symbol does not match the current synthesis` |
| `previous.as_of < current.as_of` (equal or later is invalid) | `previous synthesis as_of must be earlier than the current synthesis as_of` |

A failure raises `MarketIntelligenceInputError`.

## 3. Comparability

| Situation | `status` | `reasons` | Transitions |
|---|---|---|---|
| Same synthesis format and rules version | `comparable` | `[]` | Computed |
| Format differs | `not_comparable` | `synthesis_format_version_mismatch` | None |
| Rules differ | `not_comparable` | `synthesis_rules_version_mismatch` | None |

A version difference between two individually valid syntheses is not an error. Today the supported set is a
single version (`phase7g-v1` / `phase7g-rules-v1`), so `not_comparable` cannot occur yet; an unsupported version
is still invalid. Tests exercise it with a patched supported set.

## 4. Comparison schema

```
comparison:
  status            comparable | not_comparable
  reasons           closed reason codes (sorted; empty when comparable)
  previous_ref      synthesis_id, synthesis_format_version, synthesis_rules_version, packet_id, symbol, as_of
  elapsed_seconds   current.as_of - previous.as_of   (a fact; no threshold, no interpretation)
```

For a current-only build, `comparison` is `null` and `transitions` is `[]`, exactly as in Phase 8A.

## 5. Transition codes (closed)

Each transition has a `code`, `subjects`, `previous_pointers` and `current_pointers`. There is no severity,
score or confidence, and nothing implies a change is good or bad. Transitions are sorted by (code, subjects).

| Code | Deterministic definition | Subjects |
|---|---|---|
| `timeframe_state_changed` | Both rows present and `state` differs | interval, previous state, current state |
| `timeframe_direction_changed` | `state_direction` differs (including `unavailable`) | interval, previous, current |
| `pattern_changed` | `timeframe_alignment.pattern` differs | previous, current |
| `entered_alignment` | Current pattern is `all_bullish` or `all_bearish`, and the previous pattern was neither | previous, current |
| `exited_alignment` | Previous pattern was `all_bullish` or `all_bearish`, and the current pattern is neither | previous, current |
| `opposition_appeared` | An opposition contradiction identity is in current but not previous | code, contradiction subjects |
| `opposition_resolved` | That identity is in previous but not current | code, contradiction subjects |
| `gap_appeared` | A gap contradiction identity is in current but not previous | code, contradiction subjects |
| `gap_resolved` | That identity is in previous but not current | code, contradiction subjects |
| `domain_became_available` | Domain status goes from `unavailable` to anything else | domain, previous, current |
| `domain_became_unavailable` | Domain status goes from anything else to `unavailable` | domain, previous, current |
| `reference_sign_changed` | The same (reference, basis) is in both inputs and `value_sign` differs | reference, basis, previous, current |
| `news_identities_added` | News identities present in current and absent in previous | count, sorted identities |
| `news_identities_removed` | News identities present in previous and absent in current | count, sorted identities |

**Clarifications:**
- `all_bullish` to `all_bearish` is a `pattern_changed`, not entered or exited: both are aligned patterns.
- **Opposition codes** are `timeframe_opposition` and `market_context_opposes_timeframe`.
- **Gap codes** are `timeframe_unavailable`, `market_context_unavailable`, `comparison_misaligned`,
  `context_session_mismatch` and `news_unavailable`.

**Never used:** improving, worsening, strengthening, weakening, confirmation, acceleration, deterioration,
reversal or momentum wording.

## 6. Mirror and inverse semantics

Swapping the two inputs mirrors every transition. Tests check this for 10 pairs.

| Transition | Mirror |
|---|---|
| `*_appeared` | `*_resolved` |
| `entered_alignment` | `exited_alignment` |
| `domain_became_available` | `domain_became_unavailable` |
| `news_identities_added` | `news_identities_removed` |
| `*_changed` | The same code with its two values swapped |

The same evidence re-cut at a later `as_of` yields **no** transitions, for all 24 corpus cases.

## 7. Domain availability and `insufficient_data`

- **Domains:** `market_context`, `technical`, `news`, using the synthesis completeness statuses:
  - technical: `available`, `partial` or `unavailable`;
  - market context: `available` or `unavailable`;
  - news: `available`, `available_empty`, `partial` or `unavailable`.
- **What counts as a crossing:** only `unavailable` ↔ anything else. `partial` → `available` is not a domain
  transition; the missing timeframe shows up as `gap_resolved`, for `timeframe_unavailable`.
- **`available_empty` news** is not unavailable: news was collected and is empty.
- **`insufficient_data` is a timeframe state, never a domain status.** A row with `insufficient_data` counts as
  present in technical completeness (the Phase 7C rule). It appears as:
  - `timeframe_state_changed` (e.g. `mixed` → `insufficient_data`);
  - `timeframe_direction_changed` (→ `unavailable`);
  - `gap_appeared` for `timeframe_unavailable`.

  It never appears as `domain_became_unavailable`. Nothing is silently remapped.

## 8. News identity semantics

Identities are the synthesis `news.sources` entries, `news.items[<identity_version>:<event_key>]`. They are
compared as sets, never by list position.

- The subjects are the count followed by the sorted identities.
- The wording is only "present in one input and absent in the other". An identity can drop out because the
  publication lookback window moved, so it is never called "expired", "invalid" or "no longer relevant".
- There is no direction, catalyst or "news flow" wording.

## 9. Contradiction identity semantics

A contradiction's identity is `(code, subjects)`, which is the Phase 7G canonical key. The same identity is used
for opposition and for gap transitions, compared as sets. List order is irrelevant: a synthesis must keep
canonical order anyway, and tests permute validated lists to show the comparison uses identities.

## 10. Current-state invariance

`evidence_coverage`, `timeframe_structure`, `market_context_alignment`, `event_presence`, `conflicts`,
`attention`, `synthesis_ref` and `provenance` are derived from the **current** synthesis only. Tests check that
`build(current)` and `build(current, previous)` agree on all of them, for all 18 pairs.

- **Attention is unchanged:** there is no `change` category. Transitions stay separate from attention, which keeps
  the Phase 8A attention semantics exactly.
- **Provenance is byte-identical to Phase 8A.** The previous synthesis's references (`synthesis_id`, `packet_id`,
  versions, `as_of`) are recorded in `comparison.previous_ref`, which is part of the hashed body. That keeps the
  `provenance` shape unchanged for current-only builds.

## 11. Canonical identity

`intelligence_id = "sha256:" + SHA-256(canonical body without intelligence_id)`, where the body includes
`comparison` and `transitions`.

| Input | Result |
|---|---|
| Same current, no previous | The Phase 8A id (all 25 goldens unchanged) |
| Same current + previous A | id A, deterministic |
| Same current + previous B | id B, deterministic, different from A |

## 12. Provenance and pointers

Transition pointers are key-based synthesis pointers prefixed with `previous:` or `current:`. Examples:

- `previous:timeframes[1h].state`
- `current:contradictions[timeframe_opposition:1d,5m]`
- `previous:completeness.news`
- `current:market_context.references[QQQ:open].value_sign`
- `current:news.sources[<identity>]`

The side where the item is absent has no pointer. Tests resolve every pointer against the matching input.

## 13. No-I/O boundary, persistence and migration

- **Isolation:** the builder remains pure. A sealed test blocks sockets, files, subprocesses, the clock,
  environment reads and database creation, and all 18 transition goldens still reproduce.
- **Imports:** production code imports only the standard library, `evidence_synthesis` and
  `market_intelligence`.
- **Persistence and migration:** none. The head stays `0007_technical_evidence_ledger`.

## 14. Non-goals

- no prediction, forecast or probability;
- no score, confidence, severity or conviction;
- no BUY/SELL/HOLD or signals;
- no options, strikes, expirations, IV or Greeks;
- no entry, stop, target or sizing;
- no AI or LLM, no sentiment;
- no alert delivery, persistence, migration or scheduler;
- no Phase 9 or Phase 10 concepts.

## 15. Phase 6 safeguards

- Both inputs are cutoff-safe syntheses, and each is re-validated.
- The previous synthesis must be strictly earlier, so the comparison can never look ahead.
- No read of `evidence/` or `evaluation/`, the ledger, forward returns or prospective statistics.
- No thresholds are fitted; `elapsed_seconds` is only recorded.
- Frozen Phase 6 hashes are unchanged.

## 16. Known limits

- A reference present in only one input (e.g. a benchmark added or dropped) produces no
  `reference_sign_changed`. Its absence is visible in coverage and in contradiction transitions only.
- `not_comparable` cannot occur until a second synthesis version is supported.
- The Phase 8A identifier scan explicitly allows `timeframe_direction_changed`. The word "direction" in it names
  the upstream `state_direction` fact, and that code is required by the Phase 8B code list.
