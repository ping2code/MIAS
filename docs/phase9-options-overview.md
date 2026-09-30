# Phase 9 — Options overview

Phase 9 turns a live options chain into a replayable, content-addressed, **descriptive** description of the options
landscape. It makes no trading decisions.

| Sub-phase | Deliverable | Kind | Doc |
|---|---|---|---|
| 9A | `options_data.massive_check`: provider capability discovery | Live, read-only, metadata only | `phase9a-massive-options-check.md` |
| 9B | OptionsSnapshot `phase9-snapshot-v1`: source facts | Pure | `phase9b-options-snapshot.md` |
| 9C | `options_data.massive` + `options_data.runner`: live chain → snapshot file | Live I/O | `phase9c-massive-options-adapter.md` |
| 9D | OptionsIntelligence `phase9-v1` / `phase9-rules-v1`: description | Pure | `phase9d-options-intelligence.md` |
| 9E | `options_intelligence.runner`: local replay and integration | Local files | `phase9e-options-integration.md` |

## Flow and file boundaries

```bash
OPTIONS_DATA_PROVIDER=massive OPTIONS_DATA_API_KEY=... python -m options_data.runner --symbol META --output /tmp/META-options-snapshot.json
```

```bash
python -m options_intelligence.runner --snapshot /tmp/META-options-snapshot.json --output /tmp/META-options-intelligence.json
```

**The snapshot file is the replay boundary.** Everything downstream is deterministic and offline. Snapshot files
hold provider prices: keep them local, and never commit them.

## Security

- Options use `OPTIONS_DATA_*` only, never `MARKET_DATA_*`, so the Phase 6 settings are untouched.
- The key is only ever the `Authorization: Bearer` header. It never appears in URLs, files, summaries, errors or
  logs.
- The secure client is reused unchanged: HTTPS, timeouts, retries, pacing, no redirects, request budgets.
  Pagination is allowed only to the configured host.

## Current Massive Starter-plan limitations (live, 2026-09-30)

- **Quotes and trades** return 403, so they are `unavailable`. Quote states are `unavailable`, and no mid or
  spread is computed.
- **Underlying price** is not supplied by the chain, so it is `unavailable`, and so are strike relations and
  distances. No second price call is made.
- **IV, Greeks and open interest** have no timestamps, so they are `provider_snapshot_unverified`: not proven
  cutoff-safe.
- **Day records** are each contract's most recent day, and often from earlier sessions: about 68% for META. Every
  day record carries its own `observed_at` and session relation.
- **Real-time status:** the options feed's real-time vs delayed status is unverified. `OPTIONS_DATA_DELAY_SECONDS`
  is unset by default, so `as_of` is the check time.

The schemas already support quotes, trades and an underlying price for richer plans, without changes.

## Key semantics

- **Volume totals** and put/call volume ratios count **current-session day records only**, with the records
  counted and set aside reported. Open-interest totals count every present value. This decision is locked.
- **No lookahead:** facts observed after `as_of` are excluded group by group, and contracts expired before the
  `as_of` exchange date are excluded. Untimed facts are labelled, never assumed safe.
- **Canonical ids:** `snapshot_id` and `options_intelligence_id` are SHA-256 of the canonical body (sorted keys,
  compact, no floats, no NaN, canonical Decimal strings, no `generated_at`). Provider order and dict key order
  never change them.
- **Large outputs:** the canonical intelligence is a verification object (about 12 MB for META). Any compact
  presentation belongs to a future API or UI layer (Phase 13), and must never replace the canonical object.

## Phase 10 boundary

Phase 9 never chooses a direction, calls or puts, a contract, strike or expiration. It never applies liquidity,
spread or delta thresholds, ranks or recommends, and never computes entry, invalidation, stop, target, sizing, risk
or risk/reward. All of that is Phase 10.

## Phase 6 safeguards

- No read of `evidence/`, `evaluation/`, the ledger, forward returns or outcomes.
- No persistence, migrations (the head stays `0007_technical_evidence_ledger`) or AI.
- The frozen Phase 6 hashes are unchanged.
