# Phase 5: Pre-registered Hypotheses

**Frozen:** 2026-09-25T19:21:11Z, on branch `claude/phase5-technical-signal-robustness`
(base `ed39f3b`), **before** any Phase 5 analysis code was written or any Phase 5
data was fetched.

The machine-readable definitions are in `evaluation/hypotheses.py`. Each
hypothesis's SHA-256 content hash (below) is pinned by
`tests/test_phase5_robustness.py`, so a later edit fails the test suite. The Phase 5
matrix records the hashes in its `plan.json`.

This is research only. No hypothesis or classification can change a production
threshold, signal or schedule, and none of the labels is a trading
recommendation.

| ID | SHA-256 |
|---|---|
| H1 | `a4d93c803683213428ac87515b168757f249423b6e30355c89a4dbebebde9b71` |
| H1X | `127b1d753b0768da5a77ba45951a1fb01fdda35f103139d76ef9653516863546` |
| H2 | `67f79e81489528aa81d4e7d63d109b77df7b692d6b7eae8da05756123358231b` |
| H3 | `bc3de4174b3fa93898c72caadd282029368d99a4ebff22449b35a11f8c2b7cf8` |

## Holdout availability, stated before any data

- **Phase 4D periods (already seen):**
  - Period A: 2025-01-02 → 2026-09-23;
  - Period B: 2024-10-01 → 2024-12-31.
- **Genuinely unseen time:** 4 sessions before Period B (2024-09-25 → 09-30, the
  start of the free tier's roughly two-year history), and 1 session after Period A
  (2026-09-24). The free tier serves only through the previous trading day.
  **Neither is enough for validation.**
- **Consequence, fixed now:** no hypothesis can be marked SUPPORTED on the strength
  of a time holdout. Reused periods are **not** called holdouts.
- **Unseen dimension that does exist:** JPM (financials), UNH (healthcare),
  CAT (industrials) and XOM (energy) were never evaluated in Phase 4D. Where a
  hypothesis uses them, they are reported separately as **cross-sectional
  out-of-sample** evidence (H1X, the H2 new-symbol cells, H3).
- **Pseudo-holdout (secondary, descriptive only):** Period A split at 2025-11-03
  into A1 and A2. Both halves are in-sample for H1, because H1 came from all of
  Period A. It is never called validation.

## H1: daily bearish_setup above drift (META, MSFT)

| Field | Value |
|---|---|
| State / interval | `bearish_setup` / 1d |
| Horizon | **5 bars (fixed)**. 5 bars was Phase 4D's pre-set representative daily horizon, and it is the horizon at which the observation was made (META +157 bps, MSFT +119 bps). Choosing 5 now avoids picking a new, better-looking horizon. |
| Symbols | META, MSFT |
| Metric | excess mean return = state mean 5-bar forward return − all-bars mean (same run) |
| Expected | excess > 0 for each symbol |
| Null | circular-shift state labels, 1,000 iterations, seed 42042 |
| Pivot window | 2 (production) |
| Primary period | Period A. **In-sample:** it is where the pattern was observed. |
| Holdout | required, but none available (see above). Pseudo-holdout A1/A2 is reported descriptively. |
| Minimum sample | 30 (100+ described as stronger) |

**Classification (frozen).** Per symbol:

- **INSUFFICIENT** if n < 30.
- **UNSUPPORTED** if the excess is ≤ 0, or |excess| < 10 bps ("near zero"), or the
  excess lies inside the circular-shift 95% null interval.
- **CONSISTENT** otherwise.

Overall:

- Any INSUFFICIENT: INSUFFICIENT. All UNSUPPORTED: UNSUPPORTED. A mix: MIXED.
- All CONSISTENT: **SUPPORTED only if the holdout is also CONSISTENT for every
  symbol.** Otherwise INSUFFICIENT (holdout missing or too small), or MIXED
  (holdout disagrees).

Because no holdout exists, **H1 cannot be SUPPORTED in Phase 5 by construction**.
The in-sample result is still reported with its null percentile and effect size.

## H1X: H1's pattern on unseen symbols (cross-sectional out-of-sample)

These are the same state, interval, horizon, metric, null and period as H1, applied
to JPM, UNH, CAT and XOM, which were never evaluated in Phase 4D.

- Per-symbol rules are those of H1, without the holdout requirement.
- Overall: SUPPORTED if at least 75% of the evaluable symbols are CONSISTENT;
  UNSUPPORTED if fewer than 25%; MIXED otherwise.
- INSUFFICIENT if fewer than 4 symbols are evaluable. With four symbols, all four
  must be evaluable.

## H2: intraday confirmation lag exists

| Field | Value |
|---|---|
| States | `bullish_setup`, `bearish_setup` |
| Intervals | 5m, 1h |
| Symbols | META, NVDA, MSFT, SPY, JPM, UNH, CAT, XOM |
| Pivot window | 2 (production) |
| Periods | A and B (both reported). The JPM/UNH/CAT/XOM cells are reported separately as unseen. |
| Metrics | `confirmation_lag_bars`; **`move_before_confirmation`** = close at the pivot-confirmation bar ÷ pivot price − 1, per setup occurrence (mean and 95% bootstrap CI) |
| Expected | bullish > 0, bearish < 0 |

**Honest prior:** a confirmed pivot low is, by definition, below the following
`pivot_window` bars' lows, so a positive bullish move before confirmation is
**partly mechanical**. The existence test is therefore expected to pass. The
informative part is the **magnitude**, which is reported against the all-bars
median |2-bar return| of the same run.

**Classification (frozen):**

- A cell (symbol × interval × period × direction) is evaluable if n ≥ 30.
- It is consistent if its 95% CI excludes zero on the expected side.
- ≥ 75% of evaluable cells consistent: SUPPORTED; < 25%: UNSUPPORTED; otherwise
  MIXED. Fewer than 4 evaluable cells: INSUFFICIENT.
- Any evaluable cell significant on the opposite side caps the status at MIXED.

## H3: 1h pivot windows change lag and sample availability

| Field | Value |
|---|---|
| Interval | 1h |
| Pivot windows | 1, 2, 3, 4. Research only; production stays 2, and nothing else differs from production. |
| Symbols | the same eight |
| Period | A (primary) |
| Metrics | setup occurrences; mean \|move_before_confirmation\|; duration; confirmation lag. Reported but not classified: session-bound 1/3-bar and cross-session 1/3/5/10-bar forward and excess returns with circular-shift null percentiles. |
| Expected | as the window grows 1 → 4: occurrences non-increasing and mean \|move_before_confirmation\| non-decreasing |

**Classification (frozen):**

- A cell (symbol × direction) is evaluable if every window has ≥ 30 occurrences.
- It is consistent if both expected monotonic patterns hold (ties allowed).
- The share rules are those of H2.

**No window is ranked, selected or recommended;** the returns are reported only as
trade-offs.

## Evidence labels

The labels are **SUPPORTED, MIXED, UNSUPPORTED and INSUFFICIENT**. They describe the
evidence under these frozen rules, nothing more. None of them justifies a
threshold change on its own. Any future calibration phase would need its own
pre-registration and genuinely unseen data.
