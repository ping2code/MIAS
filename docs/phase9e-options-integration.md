# Phase 9E — Options integration and replay

**Status:** implemented on `claude/phase9e-options-integration`; not merged.

> Phase 9E makes the Phase 9 flow operational with a local replay runner. It changes none of OptionsSnapshot
> (`phase9-snapshot-v1`), OptionsIntelligence (`phase9-v1` / `phase9-rules-v1`) or the Phase 9C adapter, and adds no
> selection, filtering, ranking or trading semantics.

## 1. End-to-end flow

```
Massive API ──(network, key)──► options_data.runner ──► OptionsSnapshot file (canonical JSON)
                                                              │
MarketIntelligence file (optional) ─────────────────────────► options_intelligence.runner (local, no network)
                                                              │
                                                              ▼
                                                   OptionsIntelligence file (canonical JSON)
```

The commands:

```bash
OPTIONS_DATA_PROVIDER=massive OPTIONS_DATA_API_KEY=... python -m options_data.runner --symbol META --output /tmp/META-options-snapshot.json
```

```bash
python -m options_intelligence.runner --snapshot /tmp/META-options-snapshot.json --output /tmp/META-options-intelligence.json
```

```bash
python -m options_intelligence.runner --snapshot /tmp/META-options-snapshot.json --market-intelligence /path/to/market-intelligence.json --output /tmp/META-options-intelligence.json
```

Only step 1 needs the network and a key. Deriving and replaying intelligence are purely local.

## 2. `options_intelligence.runner`

**CLI flags:** `--snapshot`, `--output`, `--market-intelligence` (optional) and `--overwrite`. There are
deliberately no other flags: no best strike or expiry, direction, minimum volume, maximum spread or delta
target.

**Steps:**
1. Read the snapshot JSON, rejecting unreadable files, invalid JSON and **duplicate keys**.
2. Read the optional MarketIntelligence JSON the same way.
3. Build with the pure Phase 9D builder. This fully validates the snapshot (id, version, keys, cutoff, identity)
   and the MarketIntelligence reference (keys, versions, id, symbol, `as_of` not after the snapshot).
4. Verify the result by full re-derivation, which must match byte for byte.
5. Write the canonical JSON atomically: a temporary file in the target directory, fsync, then a no-clobber hard
   link, or `--overwrite` to replace. An existing file is never overwritten silently, and a failed write leaves no
   partial or temporary file.
6. Print a metadata-only summary.

**Summary fields:**
- ids: `options_intelligence_id`, `snapshot_id`, `symbol`, `as_of`;
- counts: contract and expiration counts, `truncated`, `market_intelligence_attached`, quote-state counts,
  day-session counts, IV, Greeks and open-interest counts;
- attention: its count, and the count for each code;
- `underlying_price_status`;
- output: its path and size;
- timings: `build_seconds` and `verification_seconds`.

It never includes strikes, prices, IV or Greek values, contracts or payloads.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Written |
| 1 | Invalid or unreadable input, or verification failure |
| 2 | Usage error |
| 3 | The output exists or cannot be written |

**Boundaries:**
- no network, database, AI or environment-driven semantics;
- no wall-clock reads: all timing comes from `snapshot.as_of`;
- the summary timings use a monotonic performance counter and are never part of the content-addressed object;
- session classification uses the XNYS calendar (`market_data.calendar`), as the Phase 9D builder does.

**Imports:** the runner loads none of `options_data.massive`, `options_data.runner`, `options_data.provider`,
`options_data.config`, `market_data.http`, `requests`, persistence, the evidence layers or `market_intelligence`.

## 3. MarketIntelligence reference

It is optional and validated, then referenced only. Attaching it changes exactly three things:
- `market_intelligence_ref`;
- `provenance.market_intelligence_id`;
- `options_intelligence_id`.

Every chain-derived field is identical, and a test proves it on a real Phase 8 object.

## 4. Replay guarantees

- The same canonical snapshot, plus the same MarketIntelligence if any, always gives byte-identical
  OptionsIntelligence.
- This holds across repeated runs, fresh processes, different hash seeds, working directories, `TZ`, `LANG` and
  `HOSTNAME`, and unrelated or options and market environment variables.
- **End-to-end test:** `options_data.runner` (fixture provider) writes a snapshot, `options_intelligence.runner`
  derives intelligence from it, and a replay produces identical bytes.
- **Live replay (2026-09-30, local `/tmp` artifacts, not committed):**

  | | META | NVDA |
  |---|---|---|
  | `options_intelligence_id` | `sha256:cd3e7f33…` (equals Phase 9D) | `sha256:30fb2023…` (equals Phase 9D) |
  | Contracts / expirations | 7,780 / 23 | 3,886 / 24 |
  | Quote states | `unavailable` (all) | `unavailable` (all) |
  | Build / verify | 3.2 s / 4.4 s | 1.6 s / 2.3 s |
  | Output | 12.2 MB | 6.3 MB |

  Repeated runs are byte-identical.

## 5. Large outputs (assessment only; no schema change)

The canonical OptionsIntelligence is a **verification object**, and it can be larger than its snapshot:
- META: 12.2 MB against a 9.9 MB snapshot;
- a synthetic 10,000-contract chain: about 14 MB.

The main contributors are:
- the attention `contract_ids` lists: on the Starter plan, `quote_incomplete` and `time_basis_unverified` list
  every contract;
- the per-contract `source_pointers`.

Both are needed for exact provenance and verification, so Phase 9E doesn't change them.

A compact **presentation** view (for example counts plus a sample of ids, or pointers expressed as a template) is
a future API or UI concern (Phase 13). It would need to be a separate, clearly non-canonical, derived artifact and
must never replace or alter the content-addressed object. No lossy format is implemented here.

File handling is unaffected at these sizes: atomic writes, verification in about 4 s, and the snapshot and
intelligence both held in memory.

## 6. Phase 10 boundary

Phase 10 owns:
- direction, and the call/put choice;
- contract, strike and expiration selection;
- liquidity and maximum-spread thresholds, and delta targets;
- entry, invalidation, stop and target;
- sizing, maximum risk and risk/reward;
- ranking and recommendation.

Phase 9E adds none of these.
