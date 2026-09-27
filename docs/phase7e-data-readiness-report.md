# Phase 7E — Durable data readiness and first complete evidence packets

**STATUS: IN PROGRESS**

This is an operational report. Phase 7E adds no synthesis, scoring, schema change, migration or packet change. Every
value below is counts, statuses or identifiers from runs in the operator's shell. No secrets, database URLs, API keys,
bars, payloads or article text are recorded.

## 1. Environment readiness (2026-09-27, operator shell)

- Commands ran from the `MIAS-codex` worktree at `4e6e027` (identical to `main`), with the environment loaded from
  `~/.mias-env` by the operator. The contents were never read or printed.
- `DATABASE_URL`, `MARKET_DATA_PROVIDER` and `MARKET_DATA_API_KEY` were **set** (checked for presence only). The
  worktree was clean.

## 2. PostgreSQL readiness (persistent `mias-postgres`, PostgreSQL 16, database `mias`)

- Schema head: `0007_technical_evidence_ledger`.
- **Before Phase 7E**, every table held 0 rows: `events`, `event_versions`, `event_history`, `event_provenance`,
  `technical_snapshots`, `technical_snapshot_conflicts`, `technical_evidence_ledger`, `geopolitical_anchor_registry`.
  There were no events in any family.
- All counts were read in a read-only transaction.

## 3. Redis

- `mias-redis` has been running since 2026-09-18 04:08Z. It was not used by any Phase 7E step so far, and was never
  flushed, restarted or modified.

## 4. Persistence switches

| Switch (existing) | Used |
|---|---|
| `TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED=true` | yes, per command, for the technical runner only |
| `NEWS_PERSISTENCE_SHADOW_ENABLED` | **not yet**: blocked (see §13) |
| `SEC_PERSISTENCE_SHADOW_ENABLED` | **not yet**: blocked (see §13) |
| Phase 6 `TECHNICAL_EVIDENCE_LEDGER_ENABLED` | **disabled** (see §15) |
| Technical scheduler | **disabled** (see §12) |

## 5. Technical persistence (real data, Friday 2026-09-25 session)

```
MARKET_DATA_PROVIDER=polygon TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED=true \
  python -m technical.runner --symbols META,NVDA --timeframes 1d,1h,5m
```

- **First run:** `persisted=6 duplicate=0 conflict=0 failed=0 invalid_row=0 drained=true`; symbols `failed=0`.
- **Warm-up:** 1d 450 bars (from 2024-12-09), 1h 630 (from 2026-05-19), 5m 780 (from 2026-09-14). These are the
  frozen runner windows.
- **Last completed bars:** 1d `2026-09-25` (session bar), 1h `15:30 ET`, 5m `15:55 ET`. All end at the 16:00 ET close.
- **Rerun (idempotency):** `duplicate=6 conflict=0 failed=0`. Still exactly 6 rows.
- **`technical_snapshot_tools status`:** 6 groups (META and NVDA × 1d/1h/5m), one row each, all `phase4c-v2`,
  0 conflicts.
- **`technical_snapshot_tools audit --engine-version phase4c-v2 --provider polygon`:** **0 problems** (no hash
  mismatches, duplicates, invalid enums, missing metrics, off-grid timestamps, rows persisted before bar completion,
  or unexpected providers/engines); exit 0.

## 6. MarketContext

```
python -m market_context.runner --symbols META,NVDA --benchmarks SPY,QQQ --out /tmp/mias_context.json
```

- Exit 0 with no errors.
- For both symbols: `now` 2026-09-27T04:29:56Z; `as_of` 04:14:56Z (the configured 900 s delay); session 2026-09-25;
  freshness `market_closed` (weekend).
- The Phase 7B runner reads the clock, and its `now`/`as_of` are recorded in the file. The packet cutoff below
  enforces both.

## 7. Strict evidence packets (no `--allow-partial`)

The cutoff was `AS_OF=2026-09-27T04:31:26Z`, fixed after the technical rows were recorded and after the context's
`now`.

| Symbol | Exit | `packet_id` |
|---|---|---|
| META | 0 | `sha256:fee3614bb743c10a48594aaacfea531994aa139107a41c826cfff801e82c86c9` |
| NVDA | 0 | `sha256:486fb1f12a2a1af5e6f668587ff029b6c131e1e63f7d3ecad0e915ff37ba118e` |

Both packets:
- `phase7c-v1`, exactly 8 top-level keys;
- market context `available`;
- technical `available`, with 1d/1h/5m each `found` (1 candidate each);
- news `available_empty` (`no_matching_items`): the event tables are empty, which does **not** exercise news/SEC
  storage.

## 8. Reproducibility and field validation

- Each packet was run twice with identical inputs: **byte-identical**, same `packet_id`.
- `--pretty` output re-canonicalizes **exactly** to the canonical file bytes.
- Field check on the real packets:
  - no packet-level `score`, `confidence`, `direction`, `sentiment`, `recommendation`, `options` or `generated_at`;
  - no `ai_` anywhere in the serialized packet.

## 9. Packet-quality review (descriptive only)

| Domain | Availability | Freshness / alignment |
|---|---|---|
| Technical | 1d/1h/5m present, both symbols | every `bar_end` = 2026-09-25 20:00Z. Age at `AS_OF`: 32 h 31 m (latest completed session, weekend) |
| MarketContext | available, both symbols | context `now` 90 s before `AS_OF`; data cutoff 16.5 min before; same session as the technical rows |
| News | `available_empty`, loaded 0 | none (no durable rows) |
| SEC | `available_empty`, loaded 0 | none (no durable rows) |

- No future leakage: every technical `bar_end`, the context `now`/`as_of` and all loaded rows are ≤ `AS_OF`.
- There are no exclusions.
- No pass/fail freshness thresholds are applied, because no existing contract defines them.

## 10. Missing or stale evidence

- **News and SEC:** durable collection has not been exercised yet (§13).
- Nothing else is missing for the 2026-09-25 session.

## 11. Massive Starter delay

**Pending:** the measurement is scheduled for 2026-09-28 during regular market hours. It has not been run early or
simulated.

## 12. Scheduler

It stays **disabled**. The recommendation (manual daily run, or once-daily scheduler with the existing configuration)
follows the delay measurement.

## 13. News/SEC safety blocker

- **Before this change:** the news collector always invoked OpenAI and Telegram for ALERT items, and the SEC
  collector always invoked Telegram. There was no explicit switch.
- **Resolved in code** (branch `claude/phase7e-collector-side-effect-controls`, pending merge):
  - the news collector gets `--no-ai --no-send-alerts` (`read_feed(enable_ai, send_alerts)`,
    `collect_all_sources(news_enable_ai, news_send_alerts)`);
  - the SEC collector gets `--no-send-alerts` (`process_sec_filings(send_alerts)`);
  - these are explicit `if` gates before any call, and defaults are unchanged;
  - unknown options fail closed.
- **Stored semantics:**
  - **SEC:** records are identical with delivery on or off.
  - **News:** DISPLAY_ONLY/IGNORE records are identical with AI on or off. With AI off, ALERT items keep the
    deterministic pre-AI score and decision. That's exactly the state production stores when enrichment fails, and it
    can be identified by the absence of `original_impact_score`/`quality_adjustment`.
- **Still open:** the news/SEC **operational run**. It's planned against an **isolated disposable Redis**, so real
  `mias-redis` dedupe state isn't consumed, with storage to `mias`.

## 14. Remaining risks

- Upstream news scores depend on when scoring runs (the existing wall-clock recency bonus).
- MarketContext is only as replayable as the supplied file.
- The known timing-sensitive geopolitical identity test (50 ms lookup) can flake under load. It's tracked separately.

## 15. Phase 6 evidence ledger

It stays **disabled**:
- the Massive delay measurement is still pending;
- collection timing hasn't been confirmed against it;
- the first prospective session is 2026-09-28, collected around 2026-09-29 08:00 ET.

Natural backfill (5m: 10 sessions) allows enabling shortly after the measurement without losing sessions. An exact
proposal will follow the measurement, for approval.

## 16. Readiness for a synthesis architecture assessment (Phase 7F)

**Not yet.** News and SEC durable collection must be exercised first, and the delay and ledger decisions made. The
technical and MarketContext domains and the strict-packet path are ready.
