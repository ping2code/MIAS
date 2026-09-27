# Phase 7E — Durable data readiness and first complete evidence packets

**STATUS: IN PROGRESS.** Acceptance criteria 1–9 are met on real data. Pending: the 2026-09-28 Massive Starter delay
measurement, and the Phase 6 ledger and scheduler decisions.

This is an operational report. Phase 7E adds no synthesis, scoring, schema change, migration or packet-format
change.
- **Values:** every value below is a count, status or identifier from runs in the operator's shell.
- **Never recorded:** secrets, database URLs, API keys, bars, payloads or article text.
- **Code changes during Phase 7E:** the approved collector side-effect gates (`5574101`) and the audit
  zero-result printing fix (`b2ddfff`). Both are merged, and neither changes persistence, identity, scoring or
  packet semantics.

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Persistent PostgreSQL contains real durable technical snapshots | ✅ 6 rows, clean audit, idempotent |
| 2 | News persistence path exercised | ✅ 23 durable news events |
| 3 | SEC persistence path exercised | ✅ 20 durable SEC events |
| 4 | Strict META packet without `--allow-partial` | ✅ all domains available |
| 5 | Strict NVDA packet without `--allow-partial` | ✅ all domains available |
| 6 | Both packets rerun byte-identically | ✅ |
| 7 | No future leakage | ✅ |
| 8 | No packet-schema change | ✅ `phase7c-v1` unchanged |
| 9 | No production-semantic change | ✅ only the approved side-effect gates (defaults unchanged) and an output fix |
| 10 | Data-readiness report complete | ⏳ this report stays in progress until the pending items below close |

## 1. Environment readiness (2026-09-27, operator shell)

- Commands ran from the `MIAS-codex` worktree at the current `main` for each step.
- The environment was loaded from `~/.mias-env` by the operator; its contents were never read or printed.
- `DATABASE_URL`, `MARKET_DATA_PROVIDER` and `MARKET_DATA_API_KEY` were **set** (checked for presence only).

## 2. PostgreSQL (persistent `mias-postgres`, PostgreSQL 16, database `mias`)

- Schema head: `0007_technical_evidence_ledger`. All counts were read in a read-only transaction.
- **Before Phase 7E:** every table held 0 rows.
- **Now:**

| Table | Rows |
|---|---|
| `events` | **43** (`news` 23, `sec` 20) |
| `event_versions` | 43 |
| `event_provenance` | 43 |
| `event_history` | 84 |
| `technical_snapshots` | 6 |
| `technical_snapshot_conflicts` | 0 |
| `technical_evidence_ledger` | 0 |
| `geopolitical_anchor_registry` | 0 |

- **Versions and provenance:** one version per event (first observation, nothing re-versioned) and one provenance
  row per version, so no event lacks provenance.
- **Why 84 history rows:**
  - a stored `processed` event gets two rows, `score` and `decision`;
  - a `near_duplicate_suppressed` news item gets only `decision`, by design;
  - 43 × 2 = 86, minus the **2 near-duplicate** news items = **84**;
  - **no `ai` history rows**, consistent with the no-AI run.

## 3. Redis

- `mias-redis` (running since 2026-09-18 04:08Z) was **not used** by any Phase 7E step. It was never flushed,
  restarted or modified.
- The news and SEC collection runs used an **isolated, disposable Redis**, as reported by the operator, so the
  collectors' dedupe keys were written there and not to `mias-redis`.

## 4. Persistence switches and execution modes

| Switch or mode (existing) | Used |
|---|---|
| `TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED=true` | technical runner, per command |
| `NEWS_PERSISTENCE_SHADOW_ENABLED=true` | news collector, per command |
| `SEC_PERSISTENCE_SHADOW_ENABLED=true` | SEC collector, per command |
| News collector | `python -m collector.multi_source_collector --no-ai --no-send-alerts` |
| SEC collector | `python -m collector.sec_collector --no-send-alerts` |
| Phase 6 `TECHNICAL_EVIDENCE_LEDGER_ENABLED` | **disabled** (§13) |
| Technical scheduler | **disabled** (§12) |

- **Guarantee:** `--no-ai` and `--no-send-alerts` are explicit `if` gates, checked before any OpenAI or Telegram
  call. The run doesn't rely on missing credentials.
- **Stored semantics:**
  - **SEC:** records are identical with delivery on or off.
  - **News:** DISPLAY_ONLY and IGNORE records are identical with AI on or off. ALERT items keep the deterministic
    pre-AI score and decision. That's the same state production stores when enrichment fails, and it can be
    identified by the absence of `original_impact_score` and `quality_adjustment`.

## 5. Technical persistence (real data, Friday 2026-09-25 session)

```
MARKET_DATA_PROVIDER=polygon TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED=true \
  python -m technical.runner --symbols META,NVDA --timeframes 1d,1h,5m
```

- **First run:** `persisted=6 duplicate=0 conflict=0 failed=0 invalid_row=0 drained=true`; symbols `failed=0`.
- **Warm-up:** 1d 450 bars (from 2024-12-09), 1h 630 (from 2026-05-19), 5m 780 (from 2026-09-14), the frozen
  runner windows.
- **Last completed bars:** 1d `2026-09-25`, 1h `15:30 ET`, 5m `15:55 ET`. All end at the 16:00 ET close.
- **Rerun (idempotency):** `duplicate=6 conflict=0 failed=0`, still exactly 6 rows.
- **`status`:** 6 groups, one row each, all `phase4c-v2`, 0 conflicts.
- **`audit --engine-version phase4c-v2 --provider polygon`:** **0 problems**, exit 0.

## 6. News and SEC persistence (real data)

- **News:** 23 durable events (all `news-url-v1`), including 2 `near_duplicate_suppressed`.
- **SEC:** 20 durable events (`sec-v1`): the collector's up to 10 most recent filings for each of META and NVDA.
- **Read-only audits** (exit 0 for all four):

| Audit | Result |
|---|---|
| `python -m persistence.news_audit url-variants` | `variant_group_count: 0` |
| `python -m persistence.news_audit repeats` | `repeated_events: 0` |
| `python -m persistence.sec_audit shared-accessions` | `shared_accession_count: 0` |
| `python -m persistence.sec_audit repeats` | `repeated_events: 0` |

- **Audit printing fix:** zero-result human-readable output originally crashed after printing the correct count.
  That was fixed separately (`b2ddfff`), with no change to audit semantics or `--json`.

## 7. MarketContext

```
python -m market_context.runner --symbols META,NVDA --benchmarks SPY,QQQ --out /tmp/mias_context.json
```

- Exit 0 with no errors on both runs. Session 2026-09-25 (weekend: freshness `market_closed`), configured 900 s
  delay.
- **Run 1:** `as_of` 04:14:56Z.
- **Run 2:** `as_of` 2026-09-27T06:34:29Z.
- The Phase 7B runner reads the clock and records its `now`/`as_of`; the packet cutoff enforces both.

## 8. Strict evidence packets (no `--allow-partial`)

### 8.1 Technical and market context only (before news/SEC collection)

`AS_OF=2026-09-27T04:31:26Z`. Both exit 0, with market context and technical available and news `available_empty`
(no durable rows yet, so this didn't exercise news/SEC storage).

| Symbol | `packet_id` |
|---|---|
| META | `sha256:fee3614bb743c10a48594aaacfea531994aa139107a41c826cfff801e82c86c9` |
| NVDA | `sha256:486fb1f12a2a1af5e6f668587ff029b6c131e1e63f7d3ecad0e915ff37ba118e` |

### 8.2 All domains (after news/SEC collection)

`AS_OF=2026-09-27T06:50:10Z`, 72 h publication-defined news lookback.

| | META | NVDA |
|---|---|---|
| exit | 0 | 0 |
| `packet_id` | `sha256:b96a80b464244be95065a9ffe84c894e046f9061df9b290c5e8942c1dbd49726` | `sha256:71945f553f7c51483c6b69f11f1d2bf3137f4d10231589e4a0d1b461be87e22c` |
| availability | market context, technical, news all `available` | same |
| technical | 1d/1h/5m found, 1 candidate each | same |
| news: loaded / passed / included | 23 / 23 / 11 | 23 / 23 / 10 |
| SEC: loaded / passed / included | 1 / 1 / 1 | 1 / 1 / 0 |
| exclusions | `near_duplicate_suppressed` 2, `symbol_mismatch` 10 | `near_duplicate_suppressed` 2, `symbol_mismatch` 12 |
| serialized items | 12 (11 news + 1 SEC) | 10 (news) |

**Exclusion accounting:** each packet receives 24 inputs (23 news + 1 SEC).
- **META:** 11 news + 1 SEC included + 2 near-duplicates + 10 symbol mismatches = **24**.
- **NVDA:** 10 news included + 2 near-duplicates + 12 symbol mismatches = **24**. The 12 mismatches include the
  META-only SEC filing.
- 11 META + 10 NVDA news items + 2 near-duplicates account for all 23 stored news events.

**SEC 72-hour window:** the lookback is defined by **publication time**; `observed_at` is only the upper-bound
availability check. Only one durable SEC event, META's, has a filing date inside the window. The other 19 SEC rows
were filed earlier, so they're not candidates and never loaded.

## 9. Reproducibility, leakage and field validation

- **Reproducibility:** each packet (both stages) was run twice with identical inputs: **byte-identical**, same
  `packet_id`. For stage 1, `--pretty` output also re-canonicalized exactly to the canonical bytes.
- **No future leakage:**
  - no `after_as_of`, `observed_after_as_of` or `unknown_publication_time` exclusions;
  - the MarketContext `now`/`as_of` and every technical `bar_end` (2026-09-25 20:00Z) precede `AS_OF`.
- **Field check on the real populated packets:**
  - `phase7c-v1`, exactly 8 top-level keys;
  - no packet-level `score`, `confidence`, `direction`, `sentiment`, `recommendation`, `options` or
    `generated_at`;
  - **no `ai_` anywhere** in the serialized packets.

## 10. Packet-quality review (descriptive only)

| Domain | Availability | Freshness / alignment at `AS_OF` 06:50:10Z |
|---|---|---|
| Technical | 1d/1h/5m present, both symbols | every `bar_end` is 2026-09-25 20:00Z (the latest completed session; weekend) |
| MarketContext | available, both symbols | data cutoff 06:34:29Z, the same session as the technical rows |
| News | available: 11 (META) / 10 (NVDA) items | all published within the 72 h window |
| SEC | available: 1 (META) / 0 (NVDA) items | one filing inside the window |

No pass/fail freshness thresholds are applied, because no existing contract defines them.

## 11. Massive Starter delay

**Pending:** scheduled for 2026-09-28 during regular market hours. It has not been run early or simulated.

## 12. Scheduler

**Disabled.** The recommendation (a manual daily run, or a once-daily scheduler using the existing configuration)
follows the delay measurement.

## 13. Phase 6 evidence ledger

**Disabled**, because:
- the delay measurement is still pending;
- collection timing hasn't been confirmed against it;
- the first prospective session is 2026-09-28, collected around 2026-09-29 08:00 ET.

Natural backfill (5m: 10 sessions) allows enabling shortly after the measurement without losing sessions. An exact
proposal will follow, for approval.

## 14. Remaining risks

- Upstream news scores depend on scoring time (the existing wall-clock recency bonus). ALERT items collected in
  no-AI mode carry pre-AI scores.
- MarketContext is only as replayable as the supplied file.
- The news and SEC collectors were run once. Repeat observations, and re-versioning across runs, have not yet been
  exercised on real data.
- The known timing-sensitive geopolitical identity test (50 ms lookup) can flake under load; it's tracked
  separately.

## 15. Readiness for a synthesis architecture assessment

**Data readiness is demonstrated:** real durable technical, news and SEC evidence, plus explicit MarketContext,
produce strict, reproducible, cutoff-safe `phase7c-v1` packets for META and NVDA.

A synthesis architecture assessment should still wait until the Massive delay measurement and the ledger and
scheduler decisions close Phase 7E. This report contains no synthesis design.
