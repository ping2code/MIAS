# Phase 4D: Technical Evaluation Report

**Status: evaluation matrix complete; observational evidence only.**

- This is not a profitability backtest: there are no orders, fills, costs, sizing
  or P&L (profit and loss).
- No production threshold or signal semantics changed.
- No result here is used to tune anything.

The method is in `docs/phase4d-evaluation-methodology.md`. The numbers below are
generated directly from the 56 `phase4d-v1` JSON outputs:

- 32 production runs;
- 24 research pivot-window runs;
- run with seed 42042 and 1,000 bootstrap/random iterations.

Every output passed integrity checks before use:

- state counts sum to the bar count;
- runs × mean duration equal each state's count;
- transitions equal runs − 1;
- every result under 30 observations carries only its count and
  `INSUFFICIENT_SAMPLE`.

**Units and notation:**

- Returns are in basis points (bps; 1 bp = 0.01%).
- "Excess" = the state's mean forward return minus the all-bars baseline mean of
  the same run.
- `[a, b]` = the bootstrap 95% interval of the excess mean. It is optimistic (too
  narrow) because observations overlap.
- `pNN` = the state's percentile among 1,000 random-entry means of the same sample
  size.

## Run inventory

| Symbol | Period | Dates (first → last bar) | 5m bars | 1h bars | 1d bars | Final 1d state |
|---|---|---|---|---|---|---|
| META | A | 2025-01-02 → 2026-09-23 | 33,588 | 3,015 | 432 | breakout_watch |
| META | B | 2024-10-01 → 2024-12-31 | 4,920 | 442 | 64 | mixed |
| NVDA | A | 2025-01-02 → 2026-09-23 | 33,588 | 3,015 | 432 | bullish_momentum |
| NVDA | B | 2024-10-01 → 2024-12-31 | 4,920 | 442 | 64 | breakdown_watch |
| MSFT | A | 2025-01-02 → 2026-09-23 | 33,588 | 3,015 | 432 | range |
| MSFT | B | 2024-10-01 → 2024-12-31 | 4,920 | 442 | 64 | range |
| SPY | A | 2025-01-02 → 2026-09-23 | 33,588 | 3,015 | 432 | mixed |
| SPY | B | 2024-10-01 → 2024-12-31 | 4,920 | 442 | 64 | mixed |

- Every Period A run has 432 / 3,015 / 33,588 bars (5m / 1h / 1d).
- Every Period B run has 64 / 442 / 4,920 bars.

Both match the XNYS (New York Stock Exchange) calendar exactly, including early
closes (3 in Period A, 2 in Period B), so no bars are missing.

- **Requests:** 64 for the main matrix, plus 16 for each of the two SPY Period A reruns. There
  were no HTTP 429 responses. The estimate was an upper bound of 88.
- **Runtime:** about 16 minutes for the main matrix.
- **Reproducibility on real data:** SPY Period A was run twice (the second time to
  record the overnight count in `plan.json`). All 7 SPY reports were
  **byte-identical** across the two runs.

### Provider-contract finding: overnight bars

The first matrix run failed for **SPY Period A only**. Its 30m series contained a
bar stamped **2025-02-20 20:00 ET**, from overnight trading on 24-hour venues. That
lies outside every MIAS session:

- pre-market 04:00–09:30;
- regular 09:30–16:00;
- post-market until 20:00.

The adapter correctly rejected the series instead of repairing it.

**Fix: an explicit supported-session filter, not a repair.**

- Intraday bars that *start* before 04:00 or at/after 20:00 ET are excluded before
  any analysis and counted (`excluded_overnight_bars`).
- The whole series is still checked for ordering, duplicates and OHLC first.
- Everything inside 04:00–20:00 keeps the full calendar contract: holiday,
  weekend, off-grid and half-day after-close bars are still rejected.
- There is no overnight session and no change to session semantics.
- The boundaries are tested: 04:00 kept, 19:55 kept, 20:00 excluded.

**SPY Period A overnight bars excluded: 24 (12 in the 30m source series, 12 in the 5m series; 8 pages each).** All other SPY bars passed the
full contract. META, NVDA and MSFT
passed the original strict validation in both periods, so they had no overnight
bars and the filter cannot change their results.

## 1. Purpose

Measure what followed each technical state across more symbols, a second period,
fixed 1h horizons, baselines and uncertainty estimates, **before** any threshold
could ever be tuned.

## 2. Phase 4C findings (recap)

- 5m states showed no separation.
- 1h 10-bar session-bound labels were empty.
- Daily results were dominated by drift.
- The setup-lag hypothesis was untested.
- Several daily samples were small.

## 3. Session-bound problem, confirmed

With session-bound labels, 1h horizons are now 1, 2 and 3 bars, and every 1h
session-bound result at those horizons has observations. A 10-bar horizon remains
structurally impossible under that policy (7 bars per session).

## 4. No-session-bound mode

The cross-session 1h profile (`xs`, horizons 1/3/5/10) has observations at every
horizon. For example, the 10-bar all-bars baseline has about 3,000 labels per
Period A run.

- It answers a different question: holding across the overnight gap.
- Its results differ from session-bound ones. For example, 1h cross-session has
  more 95% intervals excluding zero (20% vs 5%; section 7), because overnight gaps
  add large, clustered moves.
- States are identical under both policies (tested).

## 5. Horizons

| Profile | Horizons | Representative horizon in this report |
|---|---|---|
| 5m session-bound | 1, 3, 5, 10 | 5 |
| 1h session-bound | 1, 2, 3 | 3 |
| 1h cross-session | 1, 3, 5, 10 | 5 |
| 1d | 1, 3, 5, 10 | 5 |

The per-run JSON outputs contain every horizon.

## 6. All-bars baseline (drift)

Mean forward return of **all** bars (bps), Period A / Period B:

| Profile | h (bars) | META A / B | NVDA A / B | MSFT A / B | SPY A / B |
|---|---|---|---|---|---|
| 5m | 5 | +0.2 / +0.3 | -0.0 / +0.2 | +0.2 / +0.8 | +0.1 / +0.1 |
| 1h session-bound | 3 | +1.8 / -1.2 | +3.7 / +7.2 | +1.2 / -0.8 | +1.3 / -1.5 |
| 1h cross-session | 5 | +6.1 / +3.4 | +11.0 / +17.9 | +4.2 / +0.9 | +4.9 / +3.7 |
| 1d | 5 | +33.0 / +27.8 | +70.3 / +116.3 | +29.3 / +32.8 | +33.1 / +38.2 |

Daily drift is large and differs by symbol: 29–70 bps per 5 days in Period A, and
28–116 bps in Period B. A raw daily return must be read against it. Intraday
baselines are close to zero.

## 7. Random-entry baseline

For every state/horizon with n ≥ 30: the share outside the 2.5–97.5 percentile band
of 1,000 same-size random-entry means, and the share whose excess 95% CI excludes
zero.

| Interval | Policy | Period | Tested | Below p2.5 | Above p97.5 | Outside band | Excess CI excludes 0 |
|---|---|---|---|---|---|---|---|
| 1d | daily | A | 84 | 8 | 5 | 15.5% | 17 (20%) |
| 1d | daily | B | 2 | 0 | 0 | 0.0% | 0 (0%) |
| 1h | cross-session | A | 128 | 17 | 11 | 21.9% | 25 (20%) |
| 1h | cross-session | B | 79 | 5 | 4 | 11.4% | 14 (18%) |
| 1h | session-bound | A | 92 | 4 | 0 | 4.3% | 5 (5%) |
| 1h | session-bound | B | 43 | 1 | 0 | 2.3% | 3 (7%) |
| 5m | session-bound | A | 128 | 13 | 10 | 18.0% | 19 (15%) |
| 5m | session-bound | B | 128 | 4 | 13 | 13.3% | 21 (16%) |

Overall, **95 of 684 (13.9%)** fall outside the
random-entry band, against 5% expected if a state's observations were independent
draws. **That excess is not evidence of predictive value.**

- A state's observations come in consecutive runs, and their forward windows
  overlap. So a state's sample mean varies much more than the mean of independent
  random bars, which inflates exceedances even for a meaningless label.
- The profile with the least overlap, **1h session-bound, sits at 2.3–4.3%, at or
  below chance.** The heavily overlapping profiles (5m, 1h cross-session, 1d) sit
  at 11–22%. That is the pattern clustering would produce.
- A block/circular-shift null, which preserves run structure, is recommended
  before treating any exceedance as meaningful (section 20).

## 8. Drift-adjusted results (Period A)

Each cell shows: excess mean (bps) `[95% CI]`, random-entry percentile, n.

**5m, h = 5 bars**

| State | META | NVDA | MSFT | SPY |
|---|---|---|---|---|
| bullish_setup | -1.9 [-3.0, -0.9] p0, n=4356 | -1.0 [-2.2, +0.1] p9, n=4722 | +0.6 [-0.2, +1.4] p94, n=4955 | +0.1 [-0.2, +0.5] p66, n=6439 |
| bullish_momentum | -2.2 [-5.0, +0.7] p4, n=1113 | -1.5 [-4.0, +1.2] p15, n=1247 | +0.4 [-1.2, +2.0] p65, n=1277 | +0.4 [-0.5, +1.3] p80, n=1594 |
| breakout_watch | -1.2 [-3.9, +1.8] p12, n=1520 | -1.6 [-4.5, +1.1] p9, n=1821 | +0.4 [-1.9, +2.6] p70, n=1187 | +0.4 [-2.7, +3.5] p68, n=560 |
| bearish_setup | -0.2 [-1.3, +1.0] p35, n=4489 | +0.0 [-1.6, +1.6] p50, n=3929 | +0.1 [-0.7, +0.9] p56, n=4716 | +0.9 [+0.3, +1.4] p100, n=4273 |
| bearish_momentum | +1.6 [-0.8, +4.2] p89, n=1184 | -2.7 [-6.3, +0.8] p4, n=1021 | -2.3 [-4.1, -0.5] p1, n=1270 | +1.0 [-0.2, +2.5] p96, n=1114 |
| breakdown_watch | +2.1 [-0.4, +4.9] p97, n=1538 | +0.4 [-2.7, +3.5] p64, n=1811 | +1.1 [-1.2, +3.6] p89, n=1086 | -1.8 [-4.3, +0.7] p1, n=549 |
| range | +0.1 [-0.9, +1.0] p56, n=6336 | +1.7 [+0.6, +2.8] p100, n=6574 | +0.4 [-0.3, +1.1] p84, n=6114 | -0.4 [-1.0, +0.1] p4, n=6046 |
| mixed | +0.7 [-0.1, +1.4] p95, n=10873 | -0.1 [-1.0, +0.9] p48, n=10284 | -0.4 [-1.0, +0.1] p6, n=10804 | -0.3 [-0.7, +0.1] p8, n=10834 |

**1h session-bound, h = 3 bars**

| State | META | NVDA | MSFT | SPY |
|---|---|---|---|---|
| bullish_setup | -9.3 [-21.4, +2.9] p16, n=123 | +4.9 [-6.7, +16.9] p73, n=182 | +2.7 [-5.1, +9.9] p73, n=200 | -2.8 [-6.6, +0.9] p22, n=216 |
| bullish_momentum | +12.0 [-18.5, +45.4] p80, n=38 | -18.5 [-49.6, +13.9] p17, n=40 | -14.6 [-27.6, -2.3] p6, n=52 | +3.7 [-2.1, +9.2] p78, n=95 |
| breakout_watch | +3.0 [-11.1, +17.8] p70, n=224 | -7.3 [-23.9, +7.9] p18, n=233 | +3.9 [-8.2, +14.9] p78, n=173 | +2.7 [-3.7, +8.5] p73, n=150 |
| bearish_setup | -2.8 [-17.1, +12.3] p36, n=131 | +16.5 [-6.0, +39.8] p93, n=111 | -0.8 [-11.7, +10.7] p45, n=143 | +7.8 [-6.7, +21.2] p92, n=96 |
| bearish_momentum | INSUFFICIENT (n=24) | INSUFFICIENT (n=26) | INSUFFICIENT (n=25) | INSUFFICIENT (n=24) |
| breakdown_watch | +9.8 [-3.0, +23.4] p92, n=221 | +9.7 [-6.1, +26.7] p89, n=212 | +5.2 [-3.8, +14.3] p87, n=210 | +2.8 [-5.0, +11.0] p76, n=152 |
| range | -1.6 [-11.1, +8.3] p37, n=276 | -3.9 [-15.3, +7.2] p30, n=244 | -6.6 [-13.6, +0.6] p6, n=247 | -4.2 [-8.7, +0.2] p11, n=281 |
| mixed | -3.0 [-10.4, +6.0] p24, n=670 | +0.3 [-8.7, +9.6] p51, n=659 | +1.6 [-3.8, +7.7] p74, n=657 | +1.1 [-3.9, +6.2] p71, n=693 |

**1h cross-session, h = 5 bars**

| State | META | NVDA | MSFT | SPY |
|---|---|---|---|---|
| bullish_setup | -4.9 [-23.9, +14.8] p39, n=241 | +19.4 [-1.9, +39.9] p95, n=368 | +7.4 [-9.4, +24.2] p82, n=378 | -5.8 [-11.0, -0.8] p9, n=414 |
| bullish_momentum | -34.9 [-77.3, +6.9] p7, n=79 | -8.6 [-51.1, +28.3] p38, n=87 | +50.7 [-8.7, +119.0] p100, n=83 | +5.2 [-3.1, +13.4] p77, n=175 |
| breakout_watch | +2.0 [-19.4, +24.2] p54, n=314 | -10.9 [-32.7, +10.5] p20, n=315 | -0.6 [-18.7, +20.5] p51, n=252 | +2.1 [-9.1, +13.2] p66, n=217 |
| bearish_setup | -28.1 [-52.2, -6.0] p1, n=279 | +21.0 [-13.2, +54.7] p90, n=197 | -6.2 [-22.2, +10.7] p25, n=272 | +28.0 [+8.5, +45.6] p100, n=183 |
| bearish_momentum | -2.3 [-48.1, +41.9] p48, n=48 | -68.5 [-155.0, +7.1] p1, n=63 | +4.5 [-29.9, +41.3] p59, n=48 | +4.6 [-41.0, +56.9] p67, n=53 |
| breakdown_watch | -6.4 [-28.9, +16.1] p33, n=312 | +7.0 [-20.0, +32.9] p70, n=310 | -4.5 [-20.5, +12.8] p32, n=292 | +5.2 [-5.4, +15.2] p82, n=227 |
| range | +34.9 [+15.4, +55.0] p100, n=484 | -8.3 [-29.6, +11.6] p22, n=435 | -16.8 [-28.6, -4.6] p2, n=452 | -10.6 [-17.1, -4.2] p0, n=482 |
| mixed | -4.5 [-16.7, +8.1] p24, n=1234 | -3.5 [-17.3, +9.9] p30, n=1216 | +2.2 [-7.7, +11.4] p70, n=1214 | -0.8 [-5.6, +4.0] p37, n=1240 |

**1d, h = 5 bars**

| State | META | NVDA | MSFT | SPY |
|---|---|---|---|---|
| bullish_setup | INSUFFICIENT (n=24) | +17.2 [-63.0, +95.3] p62, n=94 | +69.7 [+20.9, +118.1] p92, n=83 | -13.3 [-40.4, +10.3] p27, n=99 |
| bullish_momentum | INSUFFICIENT (n=12) | INSUFFICIENT (n=23) | INSUFFICIENT (n=20) | -65.9 [-109.4, -23.7] p2, n=49 |
| breakout_watch | +53.3 [-92.3, +214.0] p76, n=48 | +99.5 [-54.5, +255.3] p88, n=45 | -33.2 [-185.8, +112.9] p34, n=41 | +52.0 [-5.5, +102.2] p93, n=38 |
| bearish_setup | +157.4 [+13.8, +301.4] p99, n=70 | INSUFFICIENT (n=17) | +118.9 [+15.9, +224.8] p96, n=50 | INSUFFICIENT (n=22) |
| bearish_momentum | INSUFFICIENT (n=15) | INSUFFICIENT (n=3) | INSUFFICIENT (n=19) | INSUFFICIENT (n=8) |
| breakdown_watch | -179.7 [-351.0, -22.8] p2, n=43 | +176.3 [-15.7, +366.2] p96, n=32 | -69.9 [-170.2, +38.4] p16, n=43 | INSUFFICIENT (n=27) |
| range | +19.9 [-81.4, +135.4] p65, n=55 | -334.6 [-456.4, -203.3] p0, n=57 | -49.0 [-126.7, +27.5] p28, n=34 | +22.3 [-45.4, +85.9] p73, n=35 |
| mixed | -26.8 [-116.7, +68.7] p26, n=141 | +87.2 [-13.9, +183.8] p96, n=137 | -34.4 [-137.1, +79.5] p21, n=118 | -14.2 [-57.3, +27.6] p20, n=130 |

Reading these tables:

- **5m:** every excess is within about ±3 bps. Several intervals exclude zero only
  because n is in the thousands, and the signs disagree across symbols. That is
  economically negligible, and in any case not a claim.
- **1h session-bound:** only one interval excludes zero (MSFT `bullish_momentum`,
  −14.6 bps). `bearish_momentum` is below 30 observations for all four symbols.
- **1h cross-session:** the intervals that exclude zero point in **opposite
  directions across symbols**. For example, `bearish_setup` is −28.1 for META and
  +28.0 for SPY; `range` is +34.9 for META and −10.6 to −16.8 for MSFT/SPY.
- **1d:** `bearish_setup` is above drift with intervals excluding zero for META
  (+157) and MSFT (+119); NVDA and SPY have too few observations. The other daily
  outliers disagree in sign, for example NVDA `range` −335 and META
  `breakdown_watch` −180.

## 9. Bootstrap confidence intervals

The percentile bootstrap resamples each state's own observations with replacement,
with 1,000 iterations and seed 42042. Intervals are reported for the mean, the
excess mean and the positive share. The share of excess intervals excluding zero
is in the table in section 7: 5–7% for 1h session-bound, 15–20% for the
overlapping profiles.

The intervals are **optimistic** (section 7) and show spread, not causality.

## 10. Sample rules

Production state/horizon results by status (`insufficient_data` rows included):

| Interval | Policy | Period | OK | INSUFFICIENT_SAMPLE |
|---|---|---|---|---|
| 1d | daily | A | 84 | 60 |
| 1d | daily | B | 2 | 98 |
| 1h | cross-session | A | 128 | 16 |
| 1h | cross-session | B | 79 | 61 |
| 1h | session-bound | A | 92 | 16 |
| 1h | session-bound | B | 43 | 62 |
| 5m | session-bound | A | 128 | 16 |
| 5m | session-bound | B | 128 | 16 |

Of 1029 production state/horizon results, the Period B daily runs are almost
entirely `INSUFFICIENT` (only 2 results reach 30 observations): 64 sessions is
simply too short. Period B 1h is also thin. No number is shown for any of them.

## 11. Symbols

- **META** (Period A):
  - daily drift +33 bps per 5 days;
  - daily `bearish_setup` +157 bps excess (n=70) and `breakdown_watch` −180 bps
    (n=43), the second opposite to NVDA;
  - 1h cross-session `range` +35 and `bearish_setup` −28 bps;
  - 5m everything within ±2.2 bps.
- **NVDA:**
  - the strongest drift (+70 bps per 5 days in A, +116 in B);
  - daily `range` −335 bps (n=57) and `breakdown_watch` +176 (CI includes zero);
  - most bearish daily states have too few observations;
  - every 1h interval at the representative horizons includes zero (for example,
    cross-session `bearish_momentum` −68.5 bps [−155, +7], n=63).
- **MSFT** (added large-cap tech):
  - daily `bullish_setup` +70 and `bearish_setup` +119 bps (both CIs exclude
    zero): above drift for *both* directions;
  - 1h cross-session `bullish_momentum` +51 (n=83, CI includes zero);
  - 5m `bearish_momentum` −2.3 bps.
- **SPY** (broad-index ETF):
  - drift +33 bps per 5 days;
  - daily `bullish_momentum` −66 bps (n=49);
  - 1h cross-session `bearish_setup` +28 and `range` −11 bps;
  - the smallest intraday excursions of the four (5m MFE 8.9-20.7 bps vs
    19.5-41.0 for the stocks; section B below);
  - too few daily bearish setups to evaluate at any pivot window (21-22 bars).
- **Differences between the symbols come mostly from drift and sample size.**
  Frequencies and durations are broadly similar. The sign of excess returns is
  usually *not* shared (section 15).

## 12. Periods

- **Period A:** 2025-01-02 → 2026-09-23.
- **Period B:** 2024-10-01 → 2024-12-31, the earliest non-overlapping window inside
  the free tier's roughly two-year history.

Period B is 64 sessions, so it supports 5m and, marginally, 1h comparisons. It
cannot support daily ones. Both periods start cold (no prior warm-up), as in
Phase 4C. The periods are called A and B because nothing is fitted in either.

## 13. Setup-lag research

Production pivot window 2. The "move before setup" is the close at the setup bar
divided by the structure-completing pivot's price, minus 1.

| Symbol | Interval | Period | bullish_setup: runs: mean / median delay (bars) / move before setup | bearish_setup: same |
|---|---|---|---|---|
| META | 5m | A | 975: 3.36 / 2 / +0.18% | 1004: 3.38 / 2.0 / -0.18% |
| META | 5m | B | 152: 3.56 / 2.0 / +0.19% | 166: 3.66 / 2.5 / -0.24% |
| META | 1h | A | 109: 3.49 / 3 / +0.60% | 125: 3.88 / 3 / -0.71% |
| META | 1h | B | 15: INSUFFICIENT | 19: INSUFFICIENT |
| NVDA | 5m | A | 1108: 3.38 / 2.0 / +0.19% | 1017: 3.56 / 3 / -0.28% |
| NVDA | 5m | B | 197: 3.55 / 3 / +0.30% | 157: 3.27 / 3 / -0.16% |
| NVDA | 1h | A | 140: 3.40 / 3.0 / +1.00% | 98: 3.56 / 3.0 / -1.28% |
| NVDA | 1h | B | 22: INSUFFICIENT | 12: INSUFFICIENT |
| MSFT | 5m | A | 956: 3.30 / 2.0 / +0.14% | 918: 3.18 / 2.0 / -0.14% |
| MSFT | 5m | B | 139: 3.17 / 2 / +0.12% | 116: 3.24 / 2.0 / -0.08% |
| MSFT | 1h | A | 146: 3.78 / 3.0 / +0.77% | 101: 3.48 / 3 / -0.54% |
| MSFT | 1h | B | 13: INSUFFICIENT | 11: INSUFFICIENT |
| SPY | 5m | A | 928: 2.90 / 2.0 / +0.08% | 774: 3.08 / 2.0 / -0.07% |
| SPY | 5m | B | 128: 2.87 / 2.0 / +0.08% | 107: 2.91 / 2 / -0.05% |
| SPY | 1h | A | 143: 3.69 / 3 / +0.30% | 89: 3.40 / 3 / -0.35% |
| SPY | 1h | B | 20: INSUFFICIENT | 10: INSUFFICIENT |

- **Setups usually fire on or just after the bar the pivot is confirmed.** The
  median delay is 2-3 bars on both 5m and 1h, where the pivot window is 2. Mean
  delays of about 2.9-3.9 bars reflect later confirmations.
- **By then price has already moved in the setup's direction:** about 0.05-0.30%
  on 5m and about 0.30-1.28% on 1h (Period A). That is comparable to a typical
  5-bar excursion on the same interval (section B).
- 5m Period B values are close to Period A for every symbol.
- **This is consistent with the lag hypothesis** (setups describe part of a move
  that has already happened). It does **not** show that the lag costs anything:
  setup forward returns are near baseline (section 8).
- Daily setup runs are 3–29 per symbol and window, all below 30, so **daily lag
  statistics are suppressed**. Measuring pivot-window lag needs 1h research runs
  (section 20).

## 14. Pivot-window comparison (research only; daily, Period A)

Only `pivot_window` varies; production is 2. No window is selected.

| Symbol | Pivot window | Setup bars bull / bear | Setup runs bull / bear (lag n) | Bullish setup 5-bar excess (bps, 95% CI, n) | Bearish setup 5-bar excess |
|---|---|---|---|---|---|
| META | 1 | 35 / 65 | 18 / 27 | +31.7 [-119.1, +210.6], n=32 | +180.1 [+60.9, +303.5], n=65 |
| META | 2 (production) | 24 / 70 | 8 / 23 | INSUFFICIENT (n=24) | +157.4 [+13.8, +301.4], n=70 |
| META | 3 | 30 / 49 | 9 / 11 | -284.4 [-424.9, -122.1], n=30 | +113.7 [-81.6, +295.4], n=49 |
| META | 4 | 35 / 38 | 3 / 6 | -56.0 [-194.0, +74.2], n=35 | +231.4 [+41.0, +417.3], n=38 |
| NVDA | 1 | 73 / 14 | 19 / 9 | -116.2 [-214.4, -20.3], n=73 | INSUFFICIENT (n=14) |
| NVDA | 2 (production) | 94 / 17 | 20 / 10 | +17.2 [-63.0, +95.3], n=94 | INSUFFICIENT (n=17) |
| NVDA | 3 | 92 / 29 | 20 / 9 | +39.8 [-41.2, +119.3], n=92 | INSUFFICIENT (n=29) |
| NVDA | 4 | 85 / 35 | 18 / 9 | +96.8 [+10.4, +179.8], n=81 | +147.8 [-117.8, +407.9], n=35 |
| MSFT | 1 | 60 / 47 | 25 / 19 | +23.0 [-48.2, +90.2], n=60 | +136.5 [-22.7, +302.2], n=47 |
| MSFT | 2 (production) | 83 / 50 | 15 / 18 | +69.7 [+20.9, +118.1], n=83 | +118.9 [+15.9, +224.8], n=50 |
| MSFT | 3 | 89 / 41 | 15 / 17 | -6.4 [-57.5, +50.7], n=89 | +117.0 [-15.7, +261.6], n=41 |
| MSFT | 4 | 75 / 61 | 18 / 17 | -82.3 [-151.4, -14.8], n=74 | +31.1 [-72.9, +142.0], n=61 |
| SPY | 1 | 106 / 22 | 29 / 8 | -39.5 [-63.7, -13.7], n=105 | INSUFFICIENT (n=22) |
| SPY | 2 (production) | 99 / 22 | 25 / 5 | -13.3 [-40.4, +10.3], n=99 | INSUFFICIENT (n=22) |
| SPY | 3 | 130 / 21 | 19 / 7 | -5.2 [-30.1, +18.7], n=130 | INSUFFICIENT (n=21) |
| SPY | 4 | 137 / 22 | 19 / 6 | +9.5 [-10.5, +30.9], n=137 | INSUFFICIENT (n=22) |

**Trade-offs, descriptively:**

- **Setup bars and runs:** a longer window generally lowers or holds the number of
  setup runs (META bullish 18 → 3, SPY 29 → 19, NVDA about flat at 18-20), while
  bar counts move less.
- **Bullish-setup excess does not respond to the window consistently:**
  - it rises with the window for NVDA (−116 → +97 bps) and SPY (−39 → +10);
  - it falls for MSFT (+23 → −82);
  - it is non-monotonic for META.
- **Bearish-setup excess is positive at every window for META and MSFT**, where
  the sample allows. It is insufficient for NVDA and SPY.
- None of this identifies a better window. The samples are small, the directions
  disagree, and 16 configurations × 2 states were examined.

## 15. Cross-symbol consistency

| Interval | Policy | Period | directionally_consistent | mixed_across_symbols | insufficient_evidence | Consistent share |
|---|---|---|---|---|---|---|
| 1d | daily | A | 5 | 19 | 8 | 21% |
| 1d | daily | B | 0 | 0 | 28 | 0% |
| 1h | cross-session | A | 4 | 28 | 0 | 12% |
| 1h | cross-session | B | 2 | 18 | 12 | 10% |
| 1h | session-bound | A | 8 | 15 | 1 | 35% |
| 1h | session-bound | B | 3 | 9 | 12 | 25% |
| 5m | session-bound | A | 2 | 30 | 0 | 6% |
| 5m | session-bound | B | 5 | 27 | 0 | 16% |

"Consistent" means only that the excess signs agree. If signs were random, all `k`
qualifying symbols would agree with probability 2/2^k: 50% for 2, 25% for 3, 12.5%
for 4. Most rows here have 3-4 qualifying symbols, and the observed rates are near
those chance levels: 5m 6% (A) / 16% (B), 1h 10-35%, daily Period A 21%.

The directionally consistent rows are listed below. They should be read as
candidates for chance agreement, not findings.

| State | Interval | Policy | Period | h | Symbols (n ≥ 30) | Excess mean range (bps) | Direction |
|---|---|---|---|---|---|---|---|
| bearish_setup | 1d | xs/daily | A | 10 | META, MSFT | +32.6 … +329.0 | positive |
| bearish_setup | 1d | xs/daily | A | 3 | META, MSFT | +60.0 … +117.7 | positive |
| bearish_setup | 1d | xs/daily | A | 5 | META, MSFT | +118.9 … +157.4 | positive |
| bullish_setup | 1d | xs/daily | A | 10 | MSFT, NVDA, SPY | +16.7 … +171.3 | positive |
| mixed | 1d | xs/daily | A | 10 | META, MSFT, NVDA, SPY | -141.9 … -4.8 | negative |
| breakdown_watch | 1h | xs/daily | A | 1 | META, MSFT, NVDA, SPY | +3.0 … +11.6 | positive |
| breakdown_watch | 1h | xs/daily | A | 3 | META, MSFT, NVDA, SPY | +3.5 … +22.4 | positive |
| breakout_watch | 1h | xs/daily | A | 1 | META, MSFT, NVDA, SPY | +1.5 … +7.4 | positive |
| mixed | 1h | xs/daily | A | 10 | META, MSFT, NVDA, SPY | -9.3 … -3.9 | negative |
| breakdown_watch | 1h | sb | A | 1 | META, MSFT, NVDA, SPY | +1.7 … +3.5 | positive |
| breakdown_watch | 1h | sb | A | 2 | META, MSFT, NVDA, SPY | +3.9 … +8.5 | positive |
| breakdown_watch | 1h | sb | A | 3 | META, MSFT, NVDA, SPY | +2.8 … +9.8 | positive |
| breakout_watch | 1h | sb | A | 1 | META, MSFT, NVDA, SPY | +1.7 … +3.7 | positive |
| bullish_setup | 1h | sb | A | 1 | META, MSFT, NVDA, SPY | -3.2 … -0.2 | negative |
| bullish_setup | 1h | sb | A | 2 | META, MSFT, NVDA, SPY | -6.6 … -0.4 | negative |
| mixed | 1h | sb | A | 1 | META, MSFT, NVDA, SPY | -1.9 … -0.0 | negative |
| range | 1h | sb | A | 3 | META, MSFT, NVDA, SPY | -6.6 … -1.6 | negative |
| bearish_setup | 5m | sb | A | 3 | META, MSFT, NVDA, SPY | +0.0 … +0.6 | positive |
| breakout_watch | 5m | sb | A | 1 | META, MSFT, NVDA, SPY | -1.1 … -0.1 | negative |
| breakout_watch | 1h | xs/daily | B | 1 | META, MSFT, NVDA | -11.1 … -1.1 | negative |
| breakout_watch | 1h | xs/daily | B | 10 | META, MSFT, NVDA | -41.9 … -26.0 | negative |
| mixed | 1h | sb | B | 2 | META, MSFT, NVDA, SPY | -5.0 … -2.4 | negative |
| mixed | 1h | sb | B | 3 | META, MSFT, NVDA, SPY | -8.7 … -2.8 | negative |
| range | 1h | sb | B | 3 | META, MSFT, SPY | +6.8 … +10.1 | positive |
| bullish_momentum | 5m | sb | B | 1 | META, MSFT, NVDA, SPY | +0.3 … +2.6 | positive |
| bullish_momentum | 5m | sb | B | 10 | META, MSFT, NVDA, SPY | +1.7 … +8.0 | positive |
| bullish_momentum | 5m | sb | B | 3 | META, MSFT, NVDA, SPY | +0.8 … +7.7 | positive |
| bullish_momentum | 5m | sb | B | 5 | META, MSFT, NVDA, SPY | +0.8 … +7.7 | positive |
| mixed | 5m | sb | B | 5 | META, MSFT, NVDA, SPY | +0.0 … +1.6 | positive |

## 16. Cross-period consistency

| Interval | Policy | directionally_consistent | mixed_across_periods | insufficient_evidence | Same-sign share | Chance |
|---|---|---|---|---|---|---|
| 1d | daily | 1 | 1 | 82 | 50% | 50% |
| 1h | cross-session | 30 | 49 | 45 | 38% | 50% |
| 1h | session-bound | 16 | 27 | 50 | 37% | 50% |
| 5m | session-bound | 55 | 73 | 0 | 43% | 50% |

- Where both periods meet the minimum, the excess signs agree **37-43% of the
  time, at or below the 50% expected from coin flips.** Daily comparisons are
  almost all `insufficient_evidence` (Period B is too short).
- Among the comparable pairs, the 95% intervals of A and B overlap 92% of
  the time. The intervals are wide, so overlap is weak evidence either way.
- There is **no evidence that any state's excess return persists** from Period B
  to Period A.

## Descriptive profiles (Period A, production)

### A. State durations

| Interval | State | Mean run (bars) | Median run | Longest run |
|---|---|---|---|---|
| 5m | bullish_setup | 4.54 … 7.41 | 3 … 5.0 | 68 |
| 5m | bullish_momentum | 2.57 … 3.12 | 2.0 … 2.0 | 22 |
| 5m | breakout_watch | 1.02 … 1.06 | 1.0 … 1.0 | 4 |
| 5m | bearish_setup | 4.20 … 5.94 | 3.0 … 4.0 | 45 |
| 5m | bearish_momentum | 2.54 … 3.05 | 2.0 … 2.0 | 19 |
| 5m | breakdown_watch | 1.01 … 1.07 | 1 … 1 | 6 |
| 5m | range | 3.69 … 4.77 | 3.0 … 4.0 | 26 |
| 5m | mixed | 3.91 … 5.17 | 3.0 … 4 | 45 |
| 1h | bullish_setup | 2.22 … 2.90 | 2 … 2 | 17 |
| 1h | bullish_momentum | 1.89 … 2.36 | 1 … 2.0 | 8 |
| 1h | breakout_watch | 1.06 … 1.15 | 1 … 1 | 4 |
| 1h | bearish_setup | 2.01 … 2.69 | 1.0 … 2 | 17 |
| 1h | bearish_momentum | 1.85 … 2.21 | 1 … 1.5 | 7 |
| 1h | breakdown_watch | 1.07 … 1.17 | 1.0 … 1.0 | 4 |
| 1h | range | 2.38 … 3.06 | 2.0 … 2.5 | 17 |
| 1h | mixed | 2.66 … 2.86 | 2 … 2 | 20 |
| 1d | bullish_setup | 3.00 … 5.53 | 2 … 3.0 | 38 |
| 1d | bullish_momentum | 2.22 … 4.08 | 1.0 … 3.0 | 11 |
| 1d | breakout_watch | 1.02 … 1.24 | 1.0 … 1.0 | 3 |
| 1d | bearish_setup | 1.70 … 4.40 | 1.5 … 3 | 12 |
| 1d | bearish_momentum | 1.00 … 2.67 | 1 … 3 | 4 |
| 1d | breakdown_watch | 1.13 … 1.19 | 1.0 … 1.0 | 3 |
| 1d | range | 2.06 … 3.17 | 2 … 3.0 | 9 |
| 1d | mixed | 2.52 … 2.77 | 2.0 … 2.0 | 15 |

- Watch states last about one bar on every interval, by design.
- 5m states persist longest: setups average 4.2–7.4 bars, with runs up to 68.
- 1h runs are shortest, about 2–3 bars.

### B. MFE / MAE

Mean favorable and adverse excursion (bps), oriented to the state's direction.
The range is across the symbols meeting n ≥ 30.

| Profile | h | State | Mean MFE (bps) | Mean MAE (bps) | MFE ÷ abs(MAE) | Symbols |
|---|---|---|---|---|---|---|
| 5m | 5 | bullish_setup | +8.9 … +28.4 | -31.3 … -9.7 | 0.91 … 1.03 | 4 |
| 5m | 5 | bullish_momentum | +10.9 … +31.5 | -34.6 … -12.0 | 0.91 … 1.01 | 4 |
| 5m | 5 | breakout_watch | +20.7 … +41.0 | -44.2 … -21.3 | 0.93 … 1.00 | 4 |
| 5m | 5 | bearish_setup | +14.0 … +37.0 | -35.2 … -14.9 | 0.94 … 1.05 | 4 |
| 5m | 5 | bearish_momentum | +14.6 … +44.5 | -39.9 … -16.6 | 0.88 … 1.11 | 4 |
| 5m | 5 | breakdown_watch | +23.0 … +46.5 | -43.5 … -21.1 | 0.98 … 1.09 | 4 |
| 1h session-bound | 3 | bullish_setup | +18.3 … +64.1 | -63.4 … -23.0 | 0.79 … 1.06 | 4 |
| 1h session-bound | 3 | bullish_momentum | +21.2 … +72.5 | -83.1 … -20.7 | 0.71 … 1.19 | 4 |
| 1h session-bound | 3 | breakout_watch | +28.6 … +73.9 | -82.1 … -30.8 | 0.90 … 1.09 | 4 |
| 1h session-bound | 3 | bearish_setup | +44.4 … +82.4 | -111.1 … -54.2 | 0.74 … 1.04 | 4 |
| 1h session-bound | 3 | bearish_momentum | INSUFFICIENT |  |  | 0 |
| 1h session-bound | 3 | breakdown_watch | +35.0 … +89.0 | -99.9 … -39.6 | 0.85 … 0.89 | 4 |
| 1h cross-session | 5 | bullish_setup | +32.9 … +151.0 | -129.5 … -40.9 | 0.80 … 1.17 | 4 |
| 1h cross-session | 5 | bullish_momentum | +40.2 … +153.8 | -148.8 … -35.0 | 0.65 … 1.92 | 4 |
| 1h cross-session | 5 | breakout_watch | +49.6 … +132.0 | -125.9 … -46.1 | 1.04 … 1.23 | 4 |
| 1h cross-session | 5 | bearish_setup | +74.1 … +182.6 | -203.3 … -101.4 | 0.73 … 1.39 | 4 |
| 1h cross-session | 5 | bearish_momentum | +84.5 … +256.7 | -152.5 … -97.2 | 0.87 … 1.68 | 4 |
| 1h cross-session | 5 | breakdown_watch | +53.1 … +139.0 | -164.1 … -61.5 | 0.85 … 1.06 | 4 |
| 1d | 5 | bullish_setup | +104.1 … +365.8 | -295.6 … -119.5 | 0.87 … 1.93 | 3 |
| 1d | 5 | bullish_momentum | +81.4 … +81.4 | -143.8 … -143.8 | 0.57 … 0.57 | 1 |
| 1d | 5 | breakout_watch | +161.6 … +463.5 | -302.7 … -94.5 | 1.20 … 1.71 | 4 |
| 1d | 5 | bearish_setup | +265.1 … +332.2 | -569.9 … -345.2 | 0.58 … 0.77 | 2 |
| 1d | 5 | bearish_momentum | INSUFFICIENT |  |  | 0 |
| 1d | 5 | breakdown_watch | +312.9 … +511.3 | -667.0 … -252.3 | 0.56 … 1.52 | 3 |

- Intraday MFE and |MAE| are nearly symmetric: the ratio is about 0.9–1.1 on 5m
  and mostly 0.7–1.2 on 1h.
- Excursion size scales with the interval.
- On daily bars, `bearish_setup` shows MAE larger than MFE (ratio 0.58–0.77): the
  adverse move dominated, matching its above-drift forward return.
- **MFE is an excursion, not an achievable realized profit.**

### C. Transitions

The most frequent next state, per symbol, and its share of that state's exits.

| Interval | From state | Most frequent next state (per symbol) | Share of exits |
|---|---|---|---|
| 5m | bullish_setup | breakout_watch / mixed / range | 0.26 … 0.37 |
| 5m | bullish_momentum | mixed | 0.38 … 0.49 |
| 5m | breakout_watch | mixed | 0.38 … 0.45 |
| 5m | bearish_setup | breakdown_watch / mixed / range | 0.25 … 0.39 |
| 5m | bearish_momentum | mixed | 0.37 … 0.52 |
| 5m | breakdown_watch | mixed | 0.39 … 0.44 |
| 5m | range | mixed | 0.32 … 0.45 |
| 5m | mixed | breakdown_watch / range | 0.21 … 0.26 |
| 1h | bullish_setup | mixed | 0.41 … 0.59 |
| 1h | bullish_momentum | mixed | 0.43 … 0.65 |
| 1h | breakout_watch | mixed | 0.41 … 0.50 |
| 1h | bearish_setup | mixed | 0.42 … 0.51 |
| 1h | bearish_momentum | breakdown_watch / mixed | 0.35 … 0.65 |
| 1h | breakdown_watch | mixed | 0.47 … 0.52 |
| 1h | range | breakout_watch / mixed | 0.32 … 0.41 |
| 1h | mixed | breakdown_watch / breakout_watch | 0.27 … 0.33 |
| 1d | bullish_setup | breakout_watch / range | 0.33 … 0.50 |
| 1d | bullish_momentum | breakdown_watch / mixed | 0.33 … 0.58 |
| 1d | breakout_watch | mixed | 0.41 … 0.51 |
| 1d | bearish_setup | breakdown_watch / breakout_watch | 0.30 … 0.40 |
| 1d | bearish_momentum | breakdown_watch / breakout_watch | 0.33 … 0.67 |
| 1d | breakdown_watch | mixed | 0.37 … 0.57 |
| 1d | range | breakdown_watch / breakout_watch | 0.33 … 0.47 |
| 1d | mixed | breakdown_watch / breakout_watch | 0.25 … 0.40 |

- On 5m and 1h, most states exit to `mixed` (about 0.4–0.65 of exits).
- On daily bars, setups and `range` most often exit to a watch state.
- These are descriptive shares, not a predictive (Markov) model.

## 17. Limitations

- Two periods, one of them only 64 sessions.
- Four large U.S. equities/ETFs that move together (strongly correlated).
- Optimistic, overlap-affected intervals, and a random-entry null that ignores
  clustering (section 7).
- Both periods start cold, so early bars differ from the live runner's
  session-anchored warm-up.
- The free-tier history (about two years) caps any longer holdout.
- Daily bars are derived from 30m regular-session bars, not official auction
  prints.
- The matrix tool prints no progress while it runs (usability gap; noted for
  Phase 5).
- **Provider contract:** 24-hour-venue overnight bars exist (SPY). They are now
  explicitly excluded and counted.

## 18. Multiple-comparison warning

This report rests on 1029 production state/horizon results, plus 24
research pivot-window runs:

- 4 symbols × 2 periods × 4 interval profiles × 9 states × 3–4 horizons;
- 16 pivot-window configurations.

Hundreds of these meet the minimum sample, so dozens of "significant-looking"
results are expected by chance alone. No statistical selection or correction was
applied. **Every individual result above should be treated as a hypothesis, not a
finding.**

## 19. No threshold tuning

Unchanged production values:

- RSI (Relative Strength Index) zones 70/55/45/30;
- pivot window 2;
- relative-volume threshold 1.5;
- breakout buffer 0.1% / 0.25 ATR (Average True Range);
- confidence rules;
- support/resistance clustering.

Alternative pivot windows ran only as labelled research configurations. The live
runner never imports them (tested).

## 20. Recommended next step (Phase 5)

1. **A clustering-aware null before anything else:**
   - circular-shift or block-bootstrap state labels against the same returns, so
     run structure is preserved;
   - compare exceedance rates against that null, not independent random entries.
2. **Pre-registered, narrow hypotheses tested on fresh data** (for example, a later
   period): only the few patterns that repeat here, such as daily
   `bearish_setup` above drift (META and MSFT) and setup lag on intraday bars,
   each with its horizon fixed in advance.
3. **Setup-lag research on 1h:** research pivot windows 1–4 on 1h, where setup runs
   are numerous enough (about 90–150 per symbol) to measure lag against the
   window. Daily was too sparse.
4. **More independent symbols,** for example from different sectors, and a longer
   history (a paid tier) to make Period B meaningful for daily bars.
5. **Operational:** matrix progress logging. Keep the technical scheduler off by
   default; if enabled, run once daily (see the methodology note).

Until these are done, **no threshold change is justified**. Options analytics and
signal fusion remain out of scope.
