# Phase 7G — Deterministic Evidence Synthesis

**Status:** implemented on `claude/phase7g-evidence-synthesis`; not merged.
**Format:** `phase7g-v1` · **Rules:** `phase7g-rules-v1` · **Input:** EvidencePacket `phase7c-v1`

> **EvidenceSynthesis is descriptive evidence organization, NOT a forecast or trading signal.**
> It restates what one EvidencePacket already contains: how timeframe directions relate to each other and to
> market context, and what the news and SEC items are. It never predicts, scores, ranks or recommends.

## 1. Responsibility

`evidence_synthesis.builder.synthesize(packet)` takes one **valid** `phase7c-v1` EvidencePacket and returns one
immutable, content-addressed `EvidenceSynthesis`. The input can be:

- a typed `evidence_packet.models.EvidencePacket`; or
- its canonical JSON dict.

Both forms give byte-identical output.

| Module | Role |
|---|---|
| `rules.py` | Frozen version constants, vocabularies, the `STATE_DIRECTION` table and pure rule functions |
| `model.py` | Frozen dataclasses with `to_dict()` |
| `validation.py` | Fail-closed packet validation, including re-verification of `packet_id` |
| `builder.py` | `synthesize()`: validate, then organize |
| `canonical.py` | Re-exports `canonical_json` and `content_id` from `evidence_packet.serialization`, plus `to_plain` |
| `runner.py` | Read-only CLI: packet file in, synthesis JSON out |

Dependency direction:

- `evidence_synthesis` imports only `evidence_packet` and the standard library. The `EXCHANGE_TZ` constant comes
  from `market_data.models`.
- `evidence_packet` never imports `evidence_synthesis`, and a test enforces this.

## 2. Non-goals

The synthesis contains none of the following:

- a direction, confidence, score, strength, recommendation, readiness, setup, signal or prediction field;
- any options, trade, target, stop, probability or expected-return concept;
- a higher-timeframe "winner", or any weighting.

The package also does none of the following:

- no AI, no sentiment, no news direction;
- no persistence, no migration, no scheduler, no ledger access;
- no network, database, environment, clock or file I/O inside `synthesize()`;
- no reads of `evidence/`, `evaluation/`, the technical evidence ledger or forward returns;
- no tuning on data collected on or after 2026-09-28 (the start of the Phase 6 forward-test window).

## 3. Schema (`phase7g-v1`)

There are exactly 12 top-level keys:

| Key | Content |
|---|---|
| `synthesis_format_version` | `"phase7g-v1"` |
| `synthesis_id` | `"sha256:" + SHA-256(canonical body without synthesis_id)` |
| `rules_version` | `"phase7g-rules-v1"` |
| `packet_ref` | `packet_id`, `packet_format_version`, `symbol`, `as_of` |
| `completeness` | Status and sorted reasons for each domain (market_context, technical, news), plus `technical_missing[{interval, reason}]` |
| `timeframes` | One entry per interval in 1d/1h/5m order. The source facts are copied verbatim: `state`, `trend`, `breakout_state`, `momentum`, `ema_alignment`, `vwap_position`, `technical_confidence` (the engine's own field, renamed so that no synthesis-level confidence is implied), `bar_end`. Derived fields: `available`, `state_direction`, `age_seconds = as_of − bar_end`. Also `missing_reason` and `sources` pointers |
| `timeframe_relations` | Pairs (1d,1h), (1h,5m), (1d,5m), in that order, each with `relation` and `sources` |
| `timeframe_alignment` | `pattern` plus counts of bullish, bearish, non_directional and unavailable |
| `market_context` | `available`, `session_date`, `calendar_state`, `freshness_status` (kept as-is, never re-judged), `context_as_of`, `context_age_seconds`, `references[]`, `relations[]` |
| `news` | Non-directional counts and times (see §8) |
| `contradictions` | `[{code, subjects, pointers}]`, sorted by (code, subjects) |
| `provenance` | `packet_id`, `packet_format_version`, `synthesis_format_version`, `rules_version`, `source_domains` |

There is no `generated_at` or any other wall-clock field. Every age is measured against the packet's own `as_of`.

## 4. Rule version

`phase7g-rules-v1` is part of the hashed body, so any change to the rules changes every `synthesis_id`. A test
patches `RULES_VERSION` and checks that the identity changes. Changing a rule means:

1. bump the rules version;
2. regenerate the golden fixtures with `python -m tests.evidence_synthesis_cases --regenerate`;
3. review the diff.

## 5. State-direction table

`rules.STATE_DIRECTION` is a frozen local copy of the technical engine's direction contract.
`test_state_direction_matches_engine` checks that it equals `technical.signals.DIRECTION`. That import is
test-only; the package never imports `technical` at runtime.

| State | `state_direction` |
|---|---|
| bullish_setup, bullish_momentum, breakout_watch | `bullish` |
| bearish_setup, bearish_momentum, breakdown_watch | `bearish` |
| range, mixed | `non_directional` |
| insufficient_data, or a missing row | `unavailable` |

## 6. Pairwise rules and pattern precedence

**Pairwise relation.** The rule is symmetric, and the first matching row wins:

| Condition | Relation |
|---|---|
| Either side is unavailable | `unavailable` |
| Otherwise, either side is non-directional | `non_directional` |
| Otherwise, both sides have the same direction | `agree` |
| Otherwise | `oppose` |

**Pattern.** The pattern is taken over the three directions, by fixed precedence, with no weighting:

1. `incomplete`: any timeframe is unavailable;
2. `opposed`: at least one bullish and at least one bearish;
3. `all_bullish`;
4. `all_bearish`;
5. `all_non_directional`;
6. `partially_directional`: everything else (directional and non-directional states mixed, with no opposition).

The pattern and the pairwise relations are both kept. The pattern is a compact summary; the relations say
*which* timeframes disagree. A test checks all 729 possible state combinations against expectations written
independently of the implementation.

## 7. Market-context integration

**References.** Each reference is a signed return:

- `self` with basis `prev_close` (`symbol_context.return_since_prev_close`);
- `self` with basis `open` (`symbol_context.return_since_open`);
- each benchmark comparison `(benchmark, basis)`, using `relative_return`.

Both bases and **all** benchmarks are kept. None of them is preferred.

- **Order:** `self` first, then by benchmark name, then by basis (prev_close before open).
- **`value_sign`:** positive, negative, zero or unavailable. The sign is strict: no deadband and no threshold.
- **Comparison fields:** `aligned` and `unavailable_reasons` are copied from the source comparison.

**Relations.** There is one relation per timeframe × reference, and the first matching row wins:

| Condition | Relation |
|---|---|
| The timeframe or the reference is unavailable | `unavailable` |
| Otherwise, the timeframe is non-directional or the sign is zero | `non_directional` |
| Otherwise, the direction matches the sign | `agree` |
| Otherwise | `oppose` |

Market context never changes technical facts. It is only placed next to them.

## 8. News and SEC are non-directional

News and SEC items are **counted**, never interpreted. The news section reports:

- item count;
- counts by family (news, sec);
- direct-relevance count, and related-only relevance count;
- upstream `alert_decision` counts (`none` when absent);
- upstream `impact_level` counts;
- newest and oldest publication timestamp, and the newest publication age in seconds;
- newest and oldest publication date, and the newest publication date's age in days, measured against the
  New York date of `as_of`;
- exclusion counts;
- `sources`, sorted pointers in the form `news.items[<identity_version>:<event_key>]`.

The following are always distinct:

- `available_empty`: news was checked, and there were no items;
- `unavailable`: there was no news data. This produces a `news_unavailable` contradiction.

Nothing in the news section is sentiment or direction. The upstream score and delivery values are counted
exactly as recorded upstream.

## 9. Contradiction semantics

A contradiction is either an **explicit opposition** or a **gap** in the evidence. It is not a judgment.

| Code | When | Subjects |
|---|---|---|
| `timeframe_opposition` | A pair's relation is `oppose` | (first, second) |
| `market_context_opposes_timeframe` | A timeframe × reference relation is `oppose` | (interval, reference, basis) |
| `timeframe_unavailable` | A timeframe's direction is unavailable | (interval,) |
| `market_context_unavailable` | Market context is unavailable | ("market_context",) |
| `news_unavailable` | News status is `unavailable` (**not** `available_empty`) | ("news",) |
| `comparison_misaligned` | A benchmark comparison reports `misaligned` | (benchmark, basis) |
| `context_session_mismatch` | Context `session_date` ≠ the New York date of the latest technical `bar_end` | ("market_context", interval) |

**A `mixed` technical state is NOT a contradiction by itself.** It is a non-directional source fact. The same
holds for `range`, and for non-directional relations generally. There is no `technical_state_mixed` code
(test: `test_mixed_alone_is_not_a_contradiction`).

Every contradiction carries `pointers` into the packet, such as `technical.1d.row.technical_state` or
`market_context.context.comparisons[QQQ:open].relative_return`.

## 10. Canonical form and hash

- The canonical JSON is the same as Phase 7C: `sort_keys`, compact separators, UTF-8 and `allow_nan=False`
  (`evidence_packet.serialization.canonical_json`).
- `synthesis_id = content_id(body)`, where the body is the whole synthesis except `synthesis_id`.
- The packet itself is identified only by `packet_ref.packet_id`. The id is re-verified on input, so a packet
  whose body was changed while its old `packet_id` was kept is rejected.
- Tuples serialize as lists, and dataclasses as dicts.
- The runner writes canonical JSON followed by a newline. `--pretty` is for display only; its content
  re-canonicalizes to identical bytes.

## 11. Validation (fail closed, never repair)

**Invalid input** raises `PacketValidationError`:

- the input is neither a typed packet nor a dict;
- the top-level keys are not exactly the 8 packet keys;
- an unsupported packet format or market-context format;
- a malformed or **mismatched** `packet_id`;
- a body that is not canonical JSON (such as NaN);
- a bad symbol, or a naive timestamp;
- an unknown vocabulary value, or a symbol or interval mismatch in a technical row;
- timeframes other than 1d, 1h and 5m in that order, or a row that doesn't have exactly one of row and
  `missing_reason`;
- an availability status that doesn't match the content;
- a market-context `as_of` later than the packet `as_of`;
- a return value that isn't a canonical decimal string (floats included);
- a duplicate comparison or duplicate news identity;
- an unsupported news family;
- a malformed exclusion entry;
- unexpected keys.

**Incomplete input is still valid:**

- missing timeframes that the packet itself marks;
- unavailable market context;
- unavailable or empty news.

The synthesis describes these through `completeness` and contradictions. Validation never mutates or repairs
its input.

## 12. Determinism and the no-I/O boundary

- The same packet always produces byte-identical output. Dict key order, the input form (typed or JSON), and
  the order of news items (after resealing) do not affect content.
- `synthesize()` performs no I/O. Tests run it with the following blocked, and it still produces the golden
  output:
  - `socket.socket` and `create_connection`;
  - `builtins.open`;
  - `time.time`, `time.time_ns` and `time.monotonic`.
- A subprocess test checks that importing the package loads none of these modules: `technical`, `evidence`,
  `evaluation`, `collector`, `analyzer`, `alert_engine`, `orchestrator`, `persistence`, `openai`, `redis`,
  `telegram`, `dotenv`, `requests`, `feedparser`, `sqlalchemy` or `shared.config`.
- A source scan rejects clock, environment, AI, network, database and ledger tokens.
- The runner's output does not depend on environment variables.

The runner reads only the given packet file, and writes only to stdout or `--output`. Errors are written to
stderr as JSON, with exit code 2 (`invalid_packet`, `unreadable_packet`, or a usage error):

```bash
python -m evidence_synthesis.runner --packet packet.json [--output synthesis.json] [--pretty]
```

## 13. No persistence, no migration

Phase 7G adds no table, repository, migration or scheduler hook. The migration head stays at
`0007_technical_evidence_ledger`. A synthesis exists only in memory, or in a file the operator names.

## 14. Phase 6 contamination safeguards

- There is no import of or read from `evidence/`, `evaluation/`, the technical evidence ledger or any
  forward-return data.
- The rules are fixed before any evaluation outcome exists. Nothing is fitted on data collected on or after
  2026-09-28.
- The frozen Phase 6 registry, protocol, H1′/H2′ hashes and pin are unchanged. No existing file changes: every
  file on the branch is new.
- The synthesis produces no signal that could feed back into the Phase 6 forward test.

## 15. Example

Excerpts from `tests/fixtures/evidence_synthesis/meta_real_shaped.synthesis.json`:

```json
{"synthesis_format_version": "phase7g-v1", "rules_version": "phase7g-rules-v1",
 "synthesis_id": "sha256:5cec92e66a0adf896b589c6e1a92d24a714f1f6ec81d3c95fc6fa541ef7f51c1",
 "packet_ref": {"packet_id": "sha256:e217192e4682dcddc67ac537902fc707e5b15903729dd46f7be12c01c462e9d8",
                "packet_format_version": "phase7c-v1", "symbol": "META", "as_of": "2026-09-23T20:05:00+00:00"},
 "timeframe_alignment": {"pattern": "partially_directional", "bullish": 2, "bearish": 0,
                         "non_directional": 1, "unavailable": 0}}
```

A timeframe entry. The facts are copied from the packet row; only `state_direction` and `age_seconds` are derived:

```json
{"interval": "1d", "available": true, "state": "bullish_setup", "state_direction": "bullish",
 "trend": "bullish", "breakout_state": "failed_breakout", "momentum": "neutral",
 "ema_alignment": "bullish_alignment", "vwap_position": null, "technical_confidence": "MEDIUM",
 "bar_end": "2026-09-23T20:00:00+00:00", "age_seconds": 300, "missing_reason": null,
 "sources": ["technical.1d.bar_end", "technical.1d.row.technical_state", "..."]}
```

A market-context opposition. This is a described fact, not a verdict:

```json
{"code": "market_context_opposes_timeframe", "subjects": ["1d", "QQQ", "open"],
 "pointers": ["technical.1d.row.technical_state",
              "market_context.context.comparisons[QQQ:open].relative_return"]}
```

The golden cases are:

- all_bullish, all_bearish;
- non_directional (range/mixed/range, which has **no** contradictions);
- higher_aligned_5m_opposed, lower_aligned_1d_opposed;
- unavailable_timeframe, market_context_unavailable;
- empty_news;
- meta_real_shaped.
