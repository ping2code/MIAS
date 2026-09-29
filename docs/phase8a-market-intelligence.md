# Phase 8A — Market Intelligence (current state)

**Status:** implemented on `claude/phase8a-market-intelligence`; not merged.
**Format:** `phase8-v1` · **Rules:** `phase8-rules-v1` · **Input:** EvidenceSynthesis `phase7g-v1` /
`phase7g-rules-v1`

> **MarketIntelligence is a deterministic description of current evidence. It is NOT a forecast,
> recommendation, signal, or trading decision.**

## 1. Purpose and scope

`market_intelligence.builder.build(synthesis)` takes exactly one verified EvidenceSynthesis and returns one
immutable, content-addressed `MarketIntelligence`. The input can be the typed object or its canonical dict. The
result describes:

- evidence coverage;
- timeframe structure;
- market-context alignment;
- event presence;
- unresolved conflicts;
- operational attention flags.

Phase 8A covers the **current state only**. The previous-synthesis comparison and transitions belong to Phase 8B.
Their two top-level fields already exist: `comparison` is always `null` and `transitions` is always `[]`.

## 2. Non-goals

Phase 8A has none of the following:

- no previous-synthesis comparison or transitions;
- no prediction, forecast or recommendation, no BUY/SELL/HOLD and no trade signal;
- no ranking, score, confidence, severity, conviction, priority, probability or expected return;
- no options, strikes, expirations, Greeks or IV, no stops, targets or sizing;
- no AI or LLM, no sentiment, no alert delivery;
- no persistence, database access, migration or scheduler;
- no runner;
- no Phase 9 or Phase 10 concepts.

## 3. Schema (13 top-level fields, pinned)

| # | Field | Content |
|---|---|---|
| 1 | `intelligence_format_version` | `"phase8-v1"` |
| 2 | `intelligence_id` | `"sha256:" + SHA-256(canonical body without intelligence_id)` |
| 3 | `rules_version` | `"phase8-rules-v1"` |
| 4 | `synthesis_ref` | `synthesis_id`, `synthesis_format_version`, `synthesis_rules_version`, `packet_id`, `symbol`, `as_of` (copied) |
| 5 | `comparison` | `null` (reserved for Phase 8B) |
| 6 | `evidence_coverage` | §4 |
| 7 | `timeframe_structure` | §5 |
| 8 | `market_context_alignment` | §6 |
| 9 | `event_presence` | §7 |
| 10 | `conflicts` | §8 |
| 11 | `transitions` | `[]` (reserved for Phase 8B) |
| 12 | `attention` | §9 |
| 13 | `provenance` | `synthesis_id`, `synthesis_format_version`, `synthesis_rules_version`, `packet_id`, `domains_used` |

There is no `generated_at`, and no host, process or environment field. Every value is JSON-native (str, int,
bool, null, lists and objects), with no floats. Adding or removing a top-level field needs approval.

## 4. Evidence coverage

These are lists and enums only; there are no ratios or percentages.

| Field | Meaning |
|---|---|
| `technical_status` | Synthesis completeness status: `available` / `partial` / `unavailable` |
| `available_intervals` | A row is present and its state is not `insufficient_data` |
| `unavailable_intervals` | `{interval, reason}` where there is no row (the packet's missing reason) |
| `insufficient_data_intervals` | A row is present but its state is `insufficient_data` |
| `market_context_status` | `available` / `unavailable` |
| `available_references` | `{reference, basis}` with a known sign |
| `unavailable_references` | `{reference, basis, reasons}` with sign `unavailable` (e.g. `session_mismatch`, `misaligned`) |
| `news_state` | `available` / `available_empty` / `partial` / `unavailable` (empty is never unavailable) |
| `status_reasons` | The per-domain availability reasons, copied |

## 5. Timeframe structure and `opposition_shape`

No new directional summary is created: `pattern` is copied from `timeframe_alignment.pattern`. The block also
lists `directional_intervals`, `non_directional_intervals` and `unavailable_intervals`, and gives
`opposing_pairs` in the synthesis pair order.

`opposition_shape` is decided by fixed rules, with no ranking of timeframes:

| Shape | Rule |
|---|---|
| `not_applicable` | `pattern == incomplete` |
| `none` | No pair has relation `oppose` |
| `isolated_interval` | Two directional intervals agree and the third directional interval opposes both. `isolated_interval` names it |
| `single_pair` | Exactly one pair opposes, and the third interval is non-directional |

These words are never used: countertrend, dominant, primary, confirmation.

**Validation counts over the 729 state combinations** (not market statistics): `not_applicable` 217, `none` 242,
`isolated_interval` 162, `single_pair` 108.

## 6. Market-context alignment

Market context is placed next to the technical structure and never changes it.

- **Own returns vs relative returns** are kept apart:
  - `own_return_signs`: the symbol's own return, per basis;
  - `relative_return_signs`: return relative to each benchmark, per basis.
- **Sign profiles:** `own_return_profile` and `relative_return_profile` each take one of `all_positive`,
  `all_negative`, `all_zero`, `mixed` or `unavailable`. A profile considers only available signs. `mixed` is a
  description and never a conflict.
- **`by_interval`:** for each timeframe, `agree_count`, `oppose_count`, `non_directional_count`,
  `unavailable_count` and `opposing_references`. These are descriptive counts.
- **Freshness:** `freshness_status` and `context_age_seconds` are copied.
- **Context unavailable:** `available: false`, both profiles `unavailable`, and an empty `by_interval`.

## 7. Event presence (news/SEC)

Presence, counts and recency only; there is no direction or sentiment. Fields:

- `news_state` and `item_count`;
- `counts_by_family` (news/sec);
- `counts_by_relevance`: `direct` and `related_only`;
- `upstream_alert_decision_counts` and `upstream_impact_level_counts`. These are **upstream metadata** from the
  collector, never a Phase 8 judgment;
- `newest_publication` and `oldest_publication`, each `{timestamp, date}`. Timestamped items and date-only (SEC)
  items are separate extremes and are never mixed;
- `newest_age_seconds` and `oldest_age_seconds`, both measured from the synthesis `as_of`;
- `newest_date_age_days`;
- `exclusion_counts`.

There are no "important news" or catalyst semantics.

## 8. Conflicts

A conflict is evidence that disagrees. Conflicts are exactly the synthesis opposition contradictions,
`timeframe_opposition` and `market_context_opposes_timeframe`, mapped one to one. Each has a `code`, `subjects`,
`synthesis_pointers` (the contradiction and its relation) and `packet_pointers` (the contradiction's own
pointers).

- They are sorted by (code, subjects). They are never resolved and never ranked.
- **Gaps are not conflicts.** Unavailable, misaligned and out-of-session evidence belong to coverage and
  attention.
- A `mixed` technical state, `range`, or mixed return signs never creates a conflict.

## 9. Attention

Attention means "a structural condition a human reviewer should look at". It is not investment importance.

| Code | Category | Emitted |
|---|---|---|
| `timeframe_opposition_present` | conflict | One per opposing timeframe pair |
| `market_context_opposition_present` | conflict | One per timeframe with any opposing reference (not one per reference) |
| `evidence_incomplete` | gap | Per unavailable timeframe (including `insufficient_data`), for unavailable context, or per unavailable reference that is not misaligned |
| `context_session_mismatch` | gap | From the synthesis contradiction |
| `comparison_misaligned` | gap | Per misaligned benchmark and basis |
| `market_context_not_current` | gap | Freshness status is `lagging`, `no_data` or `unknown` (upstream values; `market_closed` and `current` are never flagged) |
| `news_unavailable` | gap | News status is `unavailable` (never `available_empty`) |
| `sec_filing_present` | presence | At least one SEC item |

- Categories are **unordered**. There is no severity, priority, ranking or score.
- Flags are sorted canonically by (category, code, subjects).
- There is no staleness threshold and no clock: only the upstream freshness vocabulary and the copied ages are
  used.

## 10. Provenance

Every block and statement carries two kinds of pointer:

- **`synthesis_pointers`:** key-based paths into the synthesis, never list positions. Examples:
  - `timeframes[1d].state_direction`
  - `timeframe_relations[1d:5m].relation`
  - `market_context.references[QQQ:open].value_sign`
  - `market_context.relations[1d:QQQ:open].relation`
  - `news.counts_by_family[sec]`
  - `contradictions[timeframe_opposition:1d,5m]`
- **`packet_pointers`:** the synthesis's own EvidencePacket pointers, copied through. For timeframe structure,
  each timeframe's resolving pointer (its state, or its `missing_reason`) is used; relation sources are not,
  because for a missing timeframe they name an absent row (Phase 7H §12).

Tests resolve every pointer, in all 25 golden cases, against both the synthesis and the packet.

## 11. Canonical identity

Canonicalization is the EvidenceSynthesis / Phase 7C form: `sort_keys`, compact separators, `allow_nan=False`,
ASCII escaping, and stable list orders.

`intelligence_id = "sha256:" + SHA-256(canonical body without intelligence_id)`. The hashed body includes the
format and rules versions, `synthesis_ref`, `comparison` (null), every current-state block, `transitions` (`[]`)
and `provenance`.

| Change | `intelligence_id` |
|---|---|
| `rules_version` | changes |
| `synthesis_id` / `packet_id` | changes |
| dict key order | unchanged |

## 12. Validation: invalid vs incomplete

`validated_synthesis` fails closed with `MarketIntelligenceInputError`, using a stable message, and never
repairs. It checks:

- the format `phase7g-v1` and rules `phase7g-rules-v1` (a frozen supported set);
- the exact 12 top-level keys, and exact keys at every level;
- that `synthesis_id` recomputes;
- the `packet_ref` structure;
- ISO timestamps with a timezone;
- closed vocabularies;
- integers that are never booleans.

It also **re-derives** every derived synthesis fact with the Phase 7G rule functions, so a body that was edited
and then resealed is still rejected:

- `state_direction` from `state`;
- each timeframe relation, the pattern, and the alignment counts;
- each context relation from direction and sign, and the reference order;
- ages from `as_of`;
- completeness agreement;
- news counts, sources and dates;
- provenance agreement;
- the complete contradiction key set, including the session-date rule.

Incomplete evidence is valid and is described:

- missing or `insufficient_data` timeframes;
- unavailable context or benchmarks;
- empty or unavailable news.

## 13. No-I/O boundary, persistence and migration

- **Pure modules:** `rules`, `model`, `validation`, `builder` and `canonical` have no network, database, file,
  subprocess, clock, environment or AI access. A sealed test blocks all of these, and every golden still
  reproduces.
- **Dependency:** only `evidence_synthesis`, which uses `evidence_packet` internally, and the standard library.
  Importing `market_intelligence` adds no other project package, and lower layers never import it.
- **Persistence:** none. The chain EvidencePacket → EvidenceSynthesis → MarketIntelligence is reproducible, so
  intelligence is recomputed, never stored.
- **Migration:** none. The head stays `0007_technical_evidence_ledger`.

## 14. Phase 6 safeguards

- No read of `evidence/` or `evaluation/`, the ledger, forward returns or prospective statistics.
- Only a cutoff-safe synthesis is consumed. Its packet `as_of` limits are re-validated upstream, and here too:
  bar ends, context `as_of` and publications must not be later than `as_of`.
- There are no fitted thresholds or parameters. Nothing is tuned on post-2026-09-28 data.
- The frozen Phase 6 hashes are unchanged and are re-verified at the end of the phase.

## 15. Phase 8B reserved fields

- **`comparison`:** always `null` in Phase 8A. Phase 8B may set it for an explicit, caller-supplied previous
  synthesis of the same symbol with an earlier `as_of`.
- **`transitions`:** always `[]` in Phase 8A. Phase 8B may fill it with deterministic transition codes.

Both are already part of Phase 8A identity, so Phase 8B changes the rules version when it populates them.
