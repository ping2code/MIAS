# Phase 4D: Technical Evaluation Methodology

Phase 4D hardens how MIAS (Market Intelligence Alert System) *evaluates* its
technical states, before any threshold is ever tuned. It is **research only**:

- production thresholds and signal semantics are unchanged;
- there is no signal fusion, no options analytics and no trading;
- nothing here is a profitability claim.

Results live in `docs/phase4d-evaluation-report.md`.

| Code | Purpose |
|---|---|
| `evaluation/technical_replay.py` | one evaluation run; output format `phase4d-v1`; CLI (Command-Line Interface) |
| `evaluation/statistics.py` | baselines, excess returns, bootstrap intervals, random entry, sample rules |
| `evaluation/setup_lag.py` | setup-lag research metrics |
| `evaluation/research.py` | research-only configurations (pivot window only) |
| `evaluation/summary.py` | cross-symbol, cross-period and pivot-window summaries |
| `evaluation/matrix.py` | bounded symbols × periods × intervals runner with budget estimate |

## 1. Purpose

Phase 4C produced descriptive statistics that were hard to interpret: raw returns
mixed with symbol drift, there was no reference distribution or uncertainty,
small samples were reported alongside large ones, and 1h horizons were broken.
Phase 4D fixes the measurement, so any later tuning decision would rest on
comparable, reproducible evidence.

## 2. Phase 4C findings

- 5m states showed no forward-return separation (within about ±4 bps).
- 1h session-bound 10-bar horizons had zero observations, and 5-bar horizons
  covered only each session's first two bars.
- Daily results were dominated by drift: the all-bars 5-day mean was +33 bps for
  META and +70 bps for NVDA.
- Setup-state lag was a plausible but untested hypothesis.
- Several daily samples were small (n < 30).

## 3. The session-bound problem

A **session-bound** label requires `t + h` to fall in the same trading session. A
regular U.S. equity session has 7 hourly bars (09:30 … 15:30; 4 on an early close),
so:

- a 10-bar 1h horizon is **structurally impossible**;
- a 5-bar horizon exists only for the first two bars of each session, which biases
  it toward the open.

This is a property of the measurement, not of the market.

## 4. No-session-bound mode

`--no-session-bound` counts the next `h` **completed bars**, whatever the session:

- Overnight, weekend and holiday gaps are skipped naturally, because no bars exist,
  so the return *includes* the gap.
- It answers a different question ("what happened over the next `h` trading
  hours, holding across sessions") and is labelled as cross-session research
  (profile `xs`).
- Labels only use bars `t+1 … t+h`, and **state generation is identical under
  both policies**. The tests verify that states do not change and that labels
  never read beyond `t+h`.

The default stays `--session-bound` for backward compatibility.

## 5. Horizons

| Interval | Profile | Policy | Horizons |
|---|---|---|---|
| 5m | `sb` | session-bound | 1, 3, 5, 10 |
| 1h | `sb` | session-bound | **1, 2, 3** |
| 1h | `xs` | no-session-bound (research) | 1, 3, 5, 10 |
| 1d | `d` | n/a (daily) | 1, 3, 5, 10 |

## 6. All-bars baseline

For every symbol, interval, period, session policy and horizon, the baseline is the
count, mean, median and positive share of forward returns over **all** bars with a
valid label. Every state is compared with the baseline of its own run.

## 7. Random-entry baseline

For each state and horizon with enough data:

- draw `R` random entry sets (default 1000) of the **same size as the state's
  sample**, uniformly with replacement, from the bars with a valid label in the
  same run (same symbol, interval, period, policy and horizon);
- record each set's mean forward return.

Reported: the 2.5th, 50th and 97.5th percentiles of those means, and the state's
percentile within them. For example, a percentile near 50 means the state is
indistinguishable from random entries of that size.

## 8. Drift adjustment

- `excess_mean = state_mean − baseline_mean`
- `excess_median = state_median − baseline_median`

Reports show the raw mean, the baseline mean and the excess. A positive raw return
that does not beat the baseline is **not** evidence of anything: it is the
symbol's drift. This matters most for daily bars.

## 9. Bootstrap method

- Percentile bootstrap, 95% interval, `B` iterations (default 1000;
  `--iterations`).
- **Resampling unit:** one labelled observation of the state, resampled with
  replacement to the state's own sample size.
- Intervals are reported for the mean, the excess mean (the baseline treated as
  fixed) and the positive share.
- **Caveat:** consecutive observations overlap in time and are autocorrelated, so
  the intervals are **optimistic (too narrow)**. They indicate spread; they are
  not proof and say nothing about causality.

**Reproducibility.** Every resampling stream is a numpy PCG64 generator seeded with
`(seed, crc32(key))`, where the key names symbol, interval, period, pivot window,
session policy, state, horizon and statistic.

- The seed comes from `--seed`, else `EVALUATION_RANDOM_SEED`, else 42042.
- The same data, configuration and seed give byte-identical output (tested).
- Draws are chunked (100 per block) to bound memory, without changing the
  sequence.

## 10. Sample rules

| n (observations) | Label | Reported |
|---|---|---|
| < 30 | `INSUFFICIENT` | count and `status: INSUFFICIENT_SAMPLE` only: no mean, excess, CI (confidence interval), percentile or MFE/MAE |
| 30-99 | `SMALL` | full statistics |
| 100-499 | `MODERATE` | full statistics |
| ≥ 500 | `LARGE` | full statistics |

The rule also applies to setup-lag summaries, which need at least 30 occurrences.
The unsuppressed Phase 4C `forward` section was removed from the output, so the
rule cannot be bypassed.

## 11. Symbols

The default matrix covers:

- **META** and **NVDA** (Phase 4C);
- **MSFT**, an additional large-cap technology stock;
- **SPY**, a broad-market index ETF (exchange-traded fund).

Symbols are plain configuration (`--symbols`), and no code path is
symbol-specific.

## 12. Periods

| Label | Dates | Why |
|---|---|---|
| **Period A** | 2025-01-02 to 2026-09-23 (end exclusive 2026-09-24) | the Phase 4C period |
| **Period B** | 2024-10-01 to 2024-12-31 (end exclusive 2025-01-01) | the earliest non-overlapping window inside the free tier's roughly two-year history |

- Period B has only **64 sessions**, so most daily Period B statistics will be
  `INSUFFICIENT`. That is reported as a limitation, and no longer period is
  fabricated.
- The periods are called **A and B**, never "training" and "testing": nothing is
  fitted in either.
- Both periods start cold, with no earlier warm-up history, exactly as in
  Phase 4C.
- Overlapping periods are rejected by the matrix tool.

## 13. Setup-lag research

**Hypothesis:** confirmed pivots need `pivot_window` future bars, so setup states
may identify structure after much of the move has happened.

For each setup occurrence (the first bar of a `bullish_setup` / `bearish_setup`
run), the structure-completing pivot (the more recently confirmed of the latest
swing high and low) gives:

- `pivot_candidate_timestamp` and `pivot_confirmed_timestamp`;
- `confirmation_lag_bars` (= `pivot_window`);
- `setup_delay_bars` (setup bar − pivot bar) and `setup_after_confirmation_bars`;
- `move_before_setup`: close at the setup bar divided by the pivot price, minus 1.
  That is how far price had already moved when the setup appeared.

These fields live only in evaluation output (`--setup-events` lists every event).
The live snapshot schema is unchanged.

## 14. Pivot-window comparisons

`evaluation.research.research_config(w)` returns the production configuration
with **only** `pivot_window` changed.

- Windows 1, 2, 3 and 4 are evaluated on daily bars; 2 is production.
- Any attempt to vary another field raises `ResearchConfigError`.
- Research runs carry `research_config: true` in their metadata and are excluded
  from the cross-symbol and cross-period summaries.
- The live runner never imports the evaluation package, which a test verifies in
  a clean subprocess.

The comparison reports each window's trade-offs (occurrences, delay, move before
the setup, excess returns, MFE/MAE) **side by side, without selecting a winner**.

## 15. Cross-symbol consistency

For each period, interval, session policy, state and horizon: which symbols meet
the minimum sample, the range of excess means and medians, and **direction**
(`positive` / `negative` / `mixed`). Direction means only the sign of the excess
return. The labels are:

- `insufficient_evidence` (fewer than two qualifying symbols);
- `mixed_across_symbols`;
- `directionally_consistent`.

## 16. Cross-period consistency

For each symbol, interval, session policy, state and horizon:

- Period A vs B counts;
- excess means, whether they have the **same sign**, and the B/A magnitude ratio;
- whether the 95% excess-mean intervals **overlap**.

The labels are `insufficient_evidence`, `mixed_across_periods` and
`directionally_consistent`. There is no automatic pass/fail threshold.

## 17. Limitations

- **Two short periods:** Period B is only about 3 months.
- **Four symbols,** all large U.S. equities or ETFs, which move together.
- **Optimistic bootstrap intervals** (section 9).
- **Warm-up differences:** evaluation replays from each period's start, while the
  live runner uses session-anchored windows. The final states can differ
  (Phase 4C: NVDA 1h).
- **Minute aggregates approximate official prints,** so the daily open and close
  are derived from 30m bars.
- **Free-tier data lags** by about one trading day, so the latest session is never
  included.

## 18. Multiple-comparison warning

Every report carries this warning. About 4 symbols × 3 intervals × 2 profiles ×
2 periods × 9 states × 3-4 horizons × 4 pivot windows means **hundreds of
comparisons**. Some will look "strong" by chance alone. Phase 4D applies no
statistical selection and no correction procedure; the risk is documented instead.
Any future claim would need pre-registered hypotheses and fresh data.

## 19. No threshold tuning

No production value changed:

- RSI (Relative Strength Index) zones 70/55/45/30;
- pivot window 2;
- relative-volume threshold 1.5;
- breakout buffer 0.1% / 0.25 ATR (Average True Range);
- confidence rules;
- support/resistance clustering 0.35% / 0.5 ATR, with minimum 2 touches.

Alternative pivot windows exist only inside research runs.

## 20. Recommended next step

See `docs/phase4d-evaluation-report.md` §19, which is based on the actual matrix
results.

## Provider-contract finding: supported-session filter

The matrix found overnight bars from 24-hour venues in SPY's vendor aggregates (for
example 2025-02-20 20:00 ET; 24 bars excluded in Period A).
`market_data.providers.polygon.exclude_unsupported_sessions` now excludes intraday
bars that start before 04:00 or at/after 20:00 ET.

- The excluded bars are counted (`excluded_overnight_bars` in provider
  diagnostics, `live_check` and the matrix `plan.json`) and never reach
  aggregation, indicators, pivots, levels, evaluation or persistence.
- The whole series is still checked for ordering, duplicates and OHLC first.
- Bars inside 04:00–20:00 keep the full calendar contract: holiday, weekend,
  off-grid and half-day after-close bars are still rejected.
- The MIAS session definitions are unchanged, and there is no overnight session.
- The boundaries are tested: 04:00 kept, 19:55 kept, 20:00 excluded.

## Operational note: once-daily technical scheduling (optional, off by default)

The free tier serves data only through the previous trading day, so once daily is
the only meaningful cadence.

- **The technical job stays disabled** (`TECHNICAL_SCHEDULE_ENABLED=false`).
- To enable it, set `TECHNICAL_SCHEDULE_ENABLED=true` and
  `TECHNICAL_INTERVAL_SECONDS=86400` (validated range 60-86,400, tested), and keep
  `TECHNICAL_TIMEOUT_SECONDS=900`. A run needs about 20 paced requests (about
  4 minutes).
- The scheduler's cadence is relative to when it starts, not wall-clock anchored.
  So either start it after the provider has published the previous session (for
  example about 08:00 America/New_York), or call `python -m technical.runner` from
  an external daily timer at that time.
- There is no Telegram path. Shadow persistence
  (`TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED`) is independent and
  non-critical.
- Tests cover:
  - the job stays disabled by default;
  - the once-daily interval is accepted;
  - overlapping runs are skipped;
  - the provider key is never logged;
  - a persistence failure never changes the runner's output.

## Running the matrix

```
python -m evaluation.matrix --out /tmp/mias-4d --dry-run     # budget only, no requests
python -m evaluation.matrix --out /tmp/mias-4d               # at most ~88 paced requests (64 used in practice) plus compute
```
