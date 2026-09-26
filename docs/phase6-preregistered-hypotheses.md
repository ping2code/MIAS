# Phase 6 — preregistered prospective hypotheses (H1′, H2′)

**Status: FROZEN** (`registry_version = phase6-v1`, `prospective_format_version = phase6-v1`).
The definitions below are the human-readable form of `evidence/registry.py`, which is authoritative. They were frozen
before any prospective evidence exists. Any revision needs new IDs (H1″, H2″, …), which produce a new registry hash
and a separate ledger identity. A revision can never overwrite or mix with `phase6-v1` evidence.

| Item | SHA-256 |
|---|---|
| H1′ | `d89efb30b22af34f3ffe6a7b4cd500af8a4a9f95b5f1a903e32f8fec6dca8bca` |
| H2′ | `78e82413275f0bc8a7e4500cbbecbb263ed4414fb4b699838a18c7e212838e4a` |
| Protocol | `15587aed49589b8d8535be636b07a3b285cf0c71e0a2b0df63c0d663f349d3c8` |
| Registry (stored in every ledger row) | `b1080c2e9347285bd3a56cad55b44e46649ea049bfaaedcc7e15488a45d87acd` |

- The hashes were computed on 2026-09-26 at 00:47:03 UTC, and `tests/test_prospective_validation.py` pins them.
- The freeze moment is the committer timestamp of the Phase 6 freeze commit. It is recorded, with the resolved start
  session, in `evidence/prospective_start.json`, which is committed right after the freeze commit.

## Why these two

Phase 5 (retrospective, META/NVDA) ended with these verdicts:

- H1 UNSUPPORTED;
- H1X INSUFFICIENT;
- H2 MIXED;
- H3 UNSUPPORTED.

Moving from the independent-random-entry null to the circular-shift null cut exceedance from 13.9% to 6.6%.

Neither hypothesis below is a claim that the Phase 5 results hold. Each is a sharper, independent, forward-only test of
a question Phase 5 left open:

- **H1′** asks whether the daily `bearish_setup` excess survives on symbols it was not selected on, pooled with equal
  weight.
- **H2′** asks whether the confirmation lag has the pivot-kind-specific sign that its mechanics imply.

## H1′ — pooled daily bearish_setup excess

| Field | Frozen value |
|---|---|
| State / interval / horizon | `bearish_setup`, 1d, 5 bars |
| Symbols | META, MSFT, JPM, UNH, CAT, XOM (one per sector; NVDA and SPY excluded) |
| Configuration | production (pivot_window 2, all production thresholds, engine `phase4c-v2`) |
| Metric | `pooled_excess` = mean over qualifying symbols of (symbol mean 5-bar forward close-to-close return of prospective `bearish_setup` bars − symbol mean 5-bar forward return of all prospective bars) |
| Weighting | equal per qualifying symbol, not per observation |
| Null | per-symbol circular shift of the prospective state sequence against unchanged returns, shift in [12, N−12], 1000 iterations, seed 42042, stream key per symbol. The pooled null is the equal-weight mean of the per-symbol null excesses at the same iteration |
| Minimum sample | a qualifying symbol needs ≥ 5 observations; the pool needs ≥ 30 observations and ≥ 4 qualifying symbols |

Verdict rules, applied in order:

1. **INSUFFICIENT** if the pooled sample rule fails.
2. **UNSUPPORTED** if any of these holds:
   - `pooled_excess` ≤ 0;
   - \|`pooled_excess`\| < 10 bps;
   - `pooled_excess` ≤ the pooled null p97.5.
3. **SUPPORTED** if `pooled_excess` > the pooled null p97.5 and at least 2/3 of qualifying symbols have positive excess.
4. **MIXED** otherwise.

## H2′ — confirmation lag with pivot-kind semantics (5m, 1h)

| Field | Frozen value |
|---|---|
| States / intervals | `bullish_setup`, `bearish_setup`; 5m and 1h |
| Symbols | all eight: META, NVDA, MSFT, SPY, JPM, UNH, CAT, XOM |
| Occurrence | the first bar of a setup run |
| Completing pivot | the more recently confirmed of the snapshot's significant high and low; its kind is `low` or `high` |
| Metrics | `confirmation_lag_bars` = confirmed index − pivot index; `move_before_confirmation` = close at the confirmation bar ÷ pivot price − 1 |
| Expected sign | low pivot > 0; high pivot < 0 |
| Primary cells | symbol × interval × **aligned** kind: `bullish_setup` completed by a low, `bearish_setup` completed by a high. Counter-kind cells are reported descriptively and never vote |
| Magnitude reference | median \|2-bar close-to-close return\| over all prospective eligible bars, same symbol and interval |
| Evaluable | a primary cell with ≥ 30 occurrences |

A primary cell is **consistent** when both hold:

- its 95% bootstrap CI of the mean `move_before_confirmation` (seed 42042, 1000 iterations) excludes zero on the
  expected side;
- its mean \|move\| ≥ 0.25 × the magnitude reference.

Verdict rules:

- Fewer than 4 evaluable cells: **INSUFFICIENT**.
- At least 75% of evaluable cells consistent: **SUPPORTED**.
- Below 25%: **UNSUPPORTED**.
- Otherwise: **MIXED**.
- If any evaluable cell is significant on the *opposite* side, the verdict is capped at **MIXED**.

## Protocol (shared)

- **Collection universe:** META, NVDA, MSFT, SPY, JPM, UNH, CAT, XOM, at intervals 5m, 1h and 1d. The provider is
  Polygon/Massive.
- **Prospective start:** the first XNYS session whose regular-market open is *strictly after* the freeze commit's
  committer timestamp.
- **Earliest evaluation:** the first XNYS session on or after the start plus 6 calendar months. Evaluation also needs
  **≥ 120 complete sessions**.
- **Complete session:** accepted ledger evidence exists for all 8 symbols × 3 intervals, and none of those identities
  has a conflict.
- **Session completeness:** the session has closed before the data cutoff, and the regular bar count equals the XNYS
  expectation (5m 78, or 42 on an early close; 1h 7, or 4; 1d 1). Otherwise the attempt fails closed as
  `incomplete_session`.
- **Evaluation data:**
  - bars are re-fetched at the gate;
  - states may use pre-start history as warm-up (the runner's windows: 5m 10, 1h 90, 1d 450 sessions);
  - every observation bar, and every bar its measurement reads, must lie in complete sessions whose re-fetched
    `bar_content_hash` equals the ledger;
  - mismatching sessions are excluded and reported.
- **Pre-start data:** used for operational validation only. It is never evidence.
