# Phase 7H — Evidence Synthesis Validation and Replay

**Status:** implemented on `claude/phase7h-synthesis-validation`; not merged.
**Validates:** EvidenceSynthesis `phase7g-v1`, rules `phase7g-rules-v1`, input EvidencePacket `phase7c-v1`.

> Phase 7H validates the existing Phase 7G synthesis. It adds **no** synthesis semantics, prediction, scoring,
> weighting, AI, persistence or migration. EvidenceSynthesis remains descriptive evidence organization, not a
> forecast or trading signal. Every count below is a **validation count**, not a market statistic.

## 1. Purpose and scope

Phase 7H answers whether Phase 7G is deterministic, correctly validated and isolated enough for Phase 8 to
consume.

In scope:

- tests (`tests/test_evidence_synthesis_replay.py`, `tests/test_evidence_synthesis_matrix.py`);
- a replay corpus (`tests/evidence_synthesis_corpus.py`, `tests/fixtures/evidence_synthesis_replay/`);
- a read-only replay tool (`evidence_synthesis/replay.py`);
- this document.

The only production change is one Phase 7G validation fix (§14), made in its own commit.

Out of scope, and not done:

- Phase 6 outcomes, forward returns and the evidence ledger;
- rule tuning on any post-2026-09-28 data;
- Phase 8.

## 2. Static contract (Task A)

`StaticContractTests` pins these values directly from code:

| Item | Value |
|---|---|
| Format / rules | `phase7g-v1` / `phase7g-rules-v1` (input `phase7c-v1`, market context `phase7b-v1`) |
| Top-level fields (12) | `synthesis_format_version`, `synthesis_id`, `rules_version`, `packet_ref`, `completeness`, `timeframes`, `timeframe_relations`, `timeframe_alignment`, `market_context`, `news`, `contradictions`, `provenance` |
| `packet_ref` | `packet_id`, `packet_format_version`, `symbol`, `as_of` |
| Timeframes | `1d`, `1h`, `5m` (contractual order) |
| Pairs | (1d,1h), (1h,5m), (1d,5m) |
| Relations | `agree`, `oppose`, `non_directional`, `unavailable` |
| Patterns | `all_bullish`, `all_bearish`, `all_non_directional`, `opposed`, `partially_directional`, `incomplete` |
| Contradiction codes | `timeframe_opposition`, `market_context_opposes_timeframe`, `timeframe_unavailable`, `market_context_unavailable`, `news_unavailable`, `context_session_mismatch`, `comparison_misaligned` (no `technical_state_mixed`) |
| Hash | `synthesis_id = "sha256:" + SHA-256(UTF-8 canonical JSON of the body without synthesis_id)` |

**Canonical JSON:** `sort_keys`, `(",", ":")` separators, `allow_nan=False`, ASCII-escaped output.

**Validation:** fails closed with `PacketValidationError`, never repairs, and re-verifies `packet_id` (§9).

**Runtime imports:** the production package loads none of these: `technical`, `persistence`, `evidence`,
`evaluation`, `analyzer`, `collector`, `sqlalchemy`, `redis`, `openai`, `telegram`, `requests`, `dotenv` or
`shared.config`. The only project packages it loads are `evidence_packet`, `evidence_synthesis` and
`market_data` (for `EXCHANGE_TZ` and `SYMBOL`). The only import of `technical.signals.DIRECTION` is in the
Phase 7G contract-equality test.

## 3. Replay corpus (Task B)

There are 24 cases, each a real `phase7c-v1` packet built by the Phase 7C assembler from fixture data:

- the 9 Phase 7G goldens (`tests/fixtures/evidence_synthesis/`, unchanged);
- 15 new cases (`tests/fixtures/evidence_synthesis_replay/`).

Each case has a stored packet and its expected synthesis. The new cases vary only inputs the Phase 7G rules
already define.

| # | Required scenario | Corpus case |
|---|---|---|
| 1 | all bullish | `all_bullish` |
| 2 | all bearish | `all_bearish` |
| 3 | all non-directional | `non_directional` |
| 4 | partially directional | `meta_real_shaped` |
| 5 | 1d + 1h bullish, 5m bearish | `higher_aligned_5m_opposed` |
| 6 | 1d bearish, 1h + 5m bullish | `lower_aligned_1d_opposed` |
| 7 | missing 5m | `unavailable_timeframe` |
| 8 | unavailable market context | `market_context_unavailable` |
| 9 | empty available news | `empty_news` |
| 10 | unavailable news | `news_unavailable` |
| 11 | benchmark comparison unavailable | `benchmark_unavailable` (SPY has only the previous session → `session_mismatch`) |
| 12 | benchmark comparison misaligned | `benchmark_misaligned` |
| 13 | context opposes one timeframe | `context_opposes_one_timeframe` |
| 14 | context opposes multiple timeframes | `all_bullish` (negative context vs 3 bullish timeframes) |
| 15 | context session mismatch | `context_session_mismatch` |
| 16 | `insufficient_data` state | `insufficient_data_state` |
| 17 | `mixed` state | `mixed_all` |
| 18 | `range` state | `range_all` |
| 19 | exactly zero market return | `zero_market_return` |
| 20 | positive market return | `positive_market_return` |
| 21 | negative market return | `all_bullish` |

The extra cases are `missing_1d`, `missing_1h`, `missing_1d_5m`, `technical_unavailable` and
`market_context_and_news_unavailable`.

`test_explicit_scenario_semantics` checks each scenario's pattern, directions and contradiction codes against
hand-derived expectations, not against snapshots alone.

**Regeneration** is only for a deliberate rules or format change, followed by a diff review:
`python -m tests.evidence_synthesis_corpus --regenerate`.

## 4. Exhaustive technical replay (Task C)

All 9³ = 729 technical-state combinations go through the **complete** builder. Each uses the resealed
`all_bullish` packet, whose market context has 4 negative references.

**Checks:**

- no exception for any combination;
- every enum value is valid;
- directions match an independent restatement of the table;
- contradictions exactly equal an independently derived set, with no duplicates;
- `mixed` and `range` never appear in any contradiction's subjects;
- `insufficient_data` yields `incomplete`, with every relation that involves it `unavailable`;
- all 729 `synthesis_id` values are distinct;
- the SHA-256 of the 729 ids joined in order is pinned: `e700a817…9830ed`. The same digest is reproduced under
  `PYTHONHASHSEED` values 7 and 99991.

**Validation counts** (hand-derived: 3 bullish, 3 bearish, 2 non-directional and 1 unavailable state):

| Pattern | Count |
|---|---|
| incomplete | 217 |
| opposed | 270 |
| partially_directional | 180 |
| all_bullish | 27 |
| all_bearish | 27 |
| all_non_directional | 8 |

| Relation | Timeframe pairs (2,187) | Timeframe × reference (8,748) |
|---|---|---|
| agree | 486 | 2,916 |
| oppose | 486 | 2,916 |
| non_directional | 756 | 1,944 |
| unavailable | 459 | 972 |

**Contradiction records per synthesis** (count of syntheses): 0 (125), 1 (75), 2 (15), 3 (1), 4 (36), 5 (144),
6 (144), 8 (54), 9 (27), 10 (81), 12 (27).

**By code:** `market_context_opposes_timeframe` 2,916, `timeframe_opposition` 486, `timeframe_unavailable`
243.

## 5. Deterministic replay (Task D)

Every corpus case was synthesized 100 times from freshly parsed JSON, and every repetition was byte-identical to
the stored expected bytes.

The following input forms give identical bytes for every case:

- typed `EvidencePacket`;
- `to_dict()`;
- canonical JSON round trip;
- the stored file.

No output field depends on time, so no timestamps change between runs.

## 6. Cross-process verification (Task I)

`test_fresh_processes_and_environments` runs `python -m evidence_synthesis.replay` over both corpus directories
in three fresh interpreters. Each run changes all of the following:

- `PYTHONHASHSEED` (0, 1, 4242);
- a new temporary working directory;
- `HOSTNAME`, `TZ` (UTC, Asia/Tokyo), `LANG` and an unrelated variable;
- the process ID (naturally).

All three stdout reports are byte-identical to each other and to the in-process report.

## 7. Order-independence (Task E)

- **Dict key order**, including provenance and comparison objects, is not semantic. With keys reversed at every
  level, every case keeps the same bytes and `synthesis_id`.
- **List order** is part of the packet's own identity: `packet_id` hashes the canonical list order, and the
  Phase 7C assembler emits a canonical order. Reordering these lists and resealing gives a *different packet*,
  so `synthesis_id` must change:
  - news items;
  - exclusions;
  - benchmark comparisons;
  - relevance symbol lists;
  - availability reasons.

  The synthesized **content** (everything except `synthesis_id`, `packet_ref` and `provenance`) is identical
  in every case. The builder canonicalizes it: references, news sources, counts, exclusions, reasons and
  contradictions are sorted.
- **Intentionally semantic:** only the technical timeframe list, which must be exactly 1d, 1h, 5m. Any other
  order is invalid, not re-sorted.

## 8. Hash and canonicalization stability (Task K)

- The canonical JSON properties from §2 hold. Every stored synthesis re-canonicalizes to identical bytes.
- **Numbers:** output contains no floats, only str, int, bool, null, lists and objects. Decimal inputs are
  consumed only as signs.
- **Orderings:**
  - timeframes follow `INTERVALS`;
  - relations follow `PAIRS`;
  - contradictions are sorted by (code, subjects);
  - references: `self` first, then benchmark name, then basis;
  - news sources are sorted;
  - `provenance.source_domains` is fixed.
- **Identity:**

  | Change | `synthesis_id` |
  |---|---|
  | `rules_version` | changes |
  | `synthesis_format_version` | changes |
  | `packet_id`, with the same evidence (e.g. an added provenance note) | changes; content unchanged |
  | non-semantic dict order | unchanged |

## 9. Tamper and invalid-packet rejection (Task F)

32 invalid cases, plus non-packet inputs, all raise `PacketValidationError` with an exact, stable message, and
the input is left unmodified. There is no separate error code: the message is the stable identifier, and both
CLIs report `{"error": "invalid_packet"}` with exit 2.

**Covered:**

- **Packet identity:** wrong `packet_id`; modified body with unchanged id; malformed id; NaN anywhere in the
  body ("not canonical JSON").
- **Top level:** unsupported `format_version`; missing key; extra key; malformed or naive `as_of`; malformed
  symbol.
- **Technical:** unknown state; malformed interval; duplicate interval; wrong order; both row and
  `missing_reason` set.
- **Decimals:** float, non-numeric and NaN decimal strings.
- **Market context:** context not an object; comparisons not a list; duplicate comparison; unknown basis;
  context after `as_of`.
- **News structure:** items not a list; item without facts; malformed `observed_at`; status inconsistent with
  items.
- **The Phase 7H findings (§14):** duplicate exclusion reason; zero count; benchmark `self`; `bar_end` after
  `as_of`; `published_at` after `as_of`.

## 10. Incomplete but valid (Task G)

These all synthesize deterministically, without raising, and with explicit facts:

- 5m, 1h or 1d unavailable;
- 1d + 5m unavailable, or all technical unavailable;
- market context unavailable;
- news unavailable, or news available but empty;
- both market context and news unavailable;
- a benchmark comparison unavailable;
- an `insufficient_data` state.

The facts carried in each case are:

- `completeness` status and `technical_missing`;
- `state_direction = unavailable`, with a null state and age for a missing timeframe;
- pattern `incomplete`;
- `timeframe_unavailable`, `market_context_unavailable` or `news_unavailable` as applicable.

Empty news (`available_empty`) is never reported as `news_unavailable`.

## 11. Isolation and no I/O (Task J)

`sealed()` blocks all of the following at once:

- sockets (`socket`, `create_connection`, `gethostname`);
- `sqlalchemy.create_engine`;
- `builtins.open`, `io.open`, `os.open`;
- `subprocess.Popen` and `run`, `os.system`;
- `time.time`, `time_ns`, `monotonic`, `perf_counter`;
- `datetime.now`, `utcnow` and `today`, and `date.today`;
- every `os.environ` read, and `os.getenv`;
- `os.getpid`.

Under the seal, all 24 corpus cases still reproduce their stored bytes, and 64 extra state combinations
synthesize.

AST scans (docstrings excluded) show:

- `builder`, `rules`, `validation`, `canonical` and `model` import only the standard library (`datetime`, `re`,
  `decimal`, `collections`, `dataclasses`, `hashlib`, `json`) plus `evidence_packet`, `evidence_synthesis` and
  `market_data`;
- none of them uses any I/O, clock, environment or process name;
- `replay.py` imports only `argparse`, `hashlib`, `json`, `pathlib`, `sys` and `evidence_synthesis`, and never
  writes files.

`runner.py` and `replay.py` read only the files named on the command line.

## 12. Contradiction validation (Task L)

Each of the 7 codes is checked in its own case, with exact subjects and pointers.

- **Order:** contradictions are sorted by (code, subjects) and never duplicated.
- **Pointers:** every contradiction pointer, and every timeframe, reference and news source pointer, resolves to
  an existing location in the packet. This was checked for all 24 cases.
- **`mixed`:** a `mixed` state alone never creates a contradiction.
- **Disagreement is exposed, never resolved:** the opposing facts are copied unchanged, and no field chooses a
  side.

**Observation (not changed):** a relation that involves a *missing* timeframe points at
`technical.<i>.row.technical_state`, but that row is null. The relation is always `unavailable` in that case.
Changing the pointer would change valid-packet output and `synthesis_id`s, which Phase 7H must not do. See
§16.

## 13. Performance (Task N)

Measured on Python 3.14.4 in WSL2, single-threaded, for synthesis plus canonical serialization. These numbers
are characterization only: no code changed and no semantics depend on them.

| Workload | Wall time | Per packet |
|---|---|---|
| 1 packet (`meta_real_shaped`, median of 50) | 1.47 ms | 1.47 ms |
| 100 packets (corpus + 729 mix, median of 3) | 146 ms | 1.46 ms |
| 1,000 packets (median of 3) | 1.43 s | 1.43 ms |

## 14. Failures found: a Phase 7G validation defect, fixed

**Phase 7G validation defect found during Phase 7H and fixed without changing valid-packet synthesis
semantics** (commit `b65d76a`, `evidence_synthesis/validation.py` only).

Phase 7G accepted resealed packets that the Phase 7C assembler can never produce:

| # | Accepted input | Effect | Phase 7C guarantee |
|---|---|---|---|
| P1 | Duplicate exclusion reasons | Output depended on list order | Unique reasons (sorted `Counter`) |
| P2 | Benchmark named `"self"` | Two references both named `self` | Benchmarks are tickers |
| P3 | Technical `bar_end` after `as_of` | Negative `age_seconds` | Timeframe marked missing, `after_as_of` |
| P4 | `published_at` or `observed_at` after `as_of`; date-only publication not before `as_of`'s New York date | Negative ages | Item excluded (`after_as_of` / `observed_after_as_of`) |
| P7 | Exclusion count ≤ 0 | Copied into the output | Counts ≥ 1 |

**The fix:**

- **P1, P7:** exclusion reasons must be unique, and each count must be an integer ≥ 1.
- **P3:** `bar_end` must be at or before `as_of`.
- **P4 (timestamps):** `published_at` and `observed_at` must be at or before `as_of`.
- **P4 (date-only):** `publication_date` must be strictly before `as_of`'s America/New_York date. This mirrors
  the assembler's own `after_as_of` rule, which is stricter than "not after".
- **P2:** benchmark names must match `market_data.models.SYMBOL`. That is the existing canonical ticker
  validator the Phase 7B runner applies to `--benchmarks`, and the regex `validation.py` already used for the
  packet symbol, now imported instead of duplicated. It rejects the reserved lowercase `"self"`; no new regex
  was invented.

**Why valid output is unchanged:**

- The fix only rejects more input.
- Schema, `phase7g-v1`, `phase7g-rules-v1`, canonicalization and the builder are unchanged.
- All 9 Phase 7G goldens, the 15 replay fixtures and the real-context replay are byte-identical before and
  after the fix, with the same `synthesis_id` values.
- The Phase 7G tests (40) still pass, and 6 rejection tests were added.

The `docs/phase7g-evidence-synthesis.md` validation list (§11) predates the fix. This section supersedes it.

## 15. Real packet replay (Task H)

The strict Phase 7E META and NVDA packets (`b96a80b4…` and `71945f55…`) were never written to disk, and
rebuilding them needs the database, which is credentialed and run by the user. Instead, packets were built
**locally, with no database or network access**:

- **Tooling:** the existing Phase 7D loader (`load_market_context`) and the Phase 7C assembler.
- **Market context:** the real Phase 7E MarketContext file (`/tmp/mias_context.json`, session 2026-09-25, SPY
  and QQQ benchmarks).
- **Cutoff:** the Phase 7E §8.2 `AS_OF` of 2026-09-27T06:50:10Z.
- **Other domains:** technical and news are `unavailable/not_supplied`.

These packets contain provider prices, so they are **not committed**.

| Symbol | `packet_id` | `synthesis_id` |
|---|---|---|
| META | `sha256:9d20c7ed…5372a6e3` | `sha256:4d37b6b8…13ba02b7` |
| NVDA | `sha256:5c0f4aaf…0bf781ec` | `sha256:67d87190…4e559a72` |

Full ids:

- META `packet_id` `sha256:9d20c7ed2450bb045d7104a2b5c22b572dd06bc60fa2bfb302e14f525372a6e3`, `synthesis_id`
  `sha256:4d37b6b87cb87b3c0970f58f1d88ba588e0857181f7e4d17d05eac5513ba02b7`;
- NVDA `packet_id` `sha256:5c0f4aaf014b911febacc44db7e02d5be2e0319312667035040316bc0bf781ec`, `synthesis_id`
  `sha256:67d871900bbebc09fe53a37918b05dbdace3451d42dae4e46ef3b0074e559a72`.

**Results:**

- Each packet was replayed 100 times in each of three fresh processes, which differed in `PYTHONHASHSEED`,
  working directory, `HOSTNAME` and an unrelated variable.
- The three reports were byte-identical (report SHA-256 `108833bc…3799d9`), and identical again after the
  §14 fix.
- The synthesized content:
  - pattern `incomplete`;
  - session 2026-09-25, calendar `closed`, freshness `market_closed` (preserved), context age 940 s;
  - META references all negative;
  - NVDA `self`/`prev_close` positive and all other references negative;
  - contradictions `timeframe_unavailable` (×3) and `news_unavailable`.

**Optional full-domain replay (user-run, needs `DATABASE_URL`):** rebuild the strict packets with the existing
read-only runner, then replay them:

```bash
python -m evidence_packet.runner --symbol META --as-of 2026-09-27T06:50:10Z --market-context /tmp/mias_context.json --output /tmp/replay/meta.packet.json
python -m evidence_synthesis.replay --corpus /tmp/replay --repeat 100
```

## 16. Residual risks and open questions

- **Non-canonical decimal strings.** Strings such as `" 1 "`, `"1_000"` and `"1E+2"` are accepted, because
  `Decimal` parses them. Only the sign is used, so output stays deterministic. A canonical decimal grammar is
  not part of the established Phase 7C contract, so validation was deliberately not tightened.
- **Relation pointers for missing timeframes.** They name an absent row (§12). This is harmless for consumers
  who read `relation == "unavailable"`, but a Phase 8 consumer should not assume every relation pointer
  resolves.
- **`market_context_opposes_timeframe` is frequent.** It is a description, not a verdict: a directional
  timeframe next to an oppositely signed return.
- **Real full-domain replay** (technical and news from the database) needs the optional user-run step in §15.

## 17. Phase 6 contamination safeguards

- No read of `evidence/` or `evaluation/`, the ledger, or forward returns.
- No evaluation was executed, and no prospective outcome statistics were computed.
- The frozen registry, protocol, H1′/H2′ hashes, pin, start and earliest-evaluation dates are unchanged and were
  recomputed after the work.
- No migration: the head stays `0007_technical_evidence_ledger`.

## 18. Production Phase 7G code changed?

| Change | Effect |
|---|---|
| `evidence_synthesis/validation.py` | The §14 fix (rejects more; valid-packet output unchanged) |
| `evidence_synthesis/replay.py` | New, read-only tool (addition) |
| builder, rules, model, canonical, runner | Unchanged |
| EvidencePacket, Phase 6 | Unchanged |

## 19. GO / NO-GO for Phase 8

**GO.** Phase 7G synthesis is:

- byte-deterministic across repetitions, input forms, processes and environments;
- content-addressed with a stable hash;
- order-canonical wherever order is not semantic;
- fail-closed on every tampered or invalid case tested;
- explicit about incomplete evidence;
- isolated from I/O, the clock, the environment and Phase 6.

Phase 8 should consume it with these conditions:

- treat contradictions and relations as descriptions;
- rely only on the `phase7g-v1` / `phase7g-rules-v1` contract pinned here;
- not assume relation pointers for missing timeframes resolve.
