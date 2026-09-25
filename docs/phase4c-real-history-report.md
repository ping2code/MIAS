# Phase 4C: Real-History Technical Evaluation Report

## Status: LIVE CONTRACT VERIFIED; REAL-HISTORY EVALUATION COMPLETE (6 of 6 runs)

**First live check (2026-09-24, 20:21 America/New_York): FAILED.** It found
fractional volume, which the adapter wrongly rejected, and HTTP 429 without
`Retry-After` once the free tier's 5/minute quota was exceeded. Both are fixed:

- exact `Decimal` volume, with migrations `0005` and `0006` (NUMERIC);
- request pacing at 12 s, and a 15 s fallback wait on a bare 429.

**Second live check (2026-09-24, 22:12-22:32): PASSED.** It used 24 requests with
no 429s, and every contract check passed for META and NVDA on 5m, 1h and 1d. The
data runs only through the previous trading day (status `DELAYED`). Final states on
the 23 September bar:

| Symbol | 1d | 1h | 5m |
|---|---|---|---|
| META | breakout_watch (HIGH), trend range, 449 bars | range (LOW), 623 bars | bearish_momentum (MEDIUM), trend mixed, 702 bars |
| NVDA | bullish_momentum (MEDIUM), trend mixed, 449 bars | mixed (LOW), trend bearish, 623 bars | breakout_watch (LOW), trend mixed, 702 bars |

These are single-bar observations, not evaluation results.

Real-history evaluation (`evaluation.technical_replay`) was run by the operator
for META and NVDA on 1d, 1h and 5m, from 2 January 2025 to 23 September 2026.
Results are under "Evaluation results" below. Every report passed integrity checks
before use:

- state counts sum to the bar count;
- runs × mean duration equal each state's count;
- transitions equal runs − 1;
- MFE and MAE are oriented to the state's direction.

## How to produce this report

Run the following with a development key for Polygon.io/Massive, whose terms the
operator has accepted, exported in the **process environment**. Every command is
read-only and bounded. The output is JSON statistics only, with no raw vendor bars.

```
export MARKET_DATA_PROVIDER=polygon
export MARKET_DATA_API_KEY=<development key>

# 1. Contract check (recent 5 trading days, at most 40 requests):
python -m market_data.live_check --symbols META,NVDA --intervals 5m,1h,1d --days 5 --states

# 2. Observational evaluation (no thresholds are tuned; not a profitability backtest):
python -m evaluation.technical_replay --symbol META --interval 5m --start <YYYY-MM-DD> --end <YYYY-MM-DD>
python -m evaluation.technical_replay --symbol META --interval 1h --start <YYYY-MM-DD> --end <YYYY-MM-DD>
python -m evaluation.technical_replay --symbol META --interval 1d --start <YYYY-MM-DD> --end <YYYY-MM-DD>
# ...and the same three commands for NVDA.
```

Record only the following per symbol and interval:

- provider;
- date range;
- bar count;
- technical state counts;
- transition counts;
- 1/3/5/10-bar forward-return summary;
- MFE/MAE summary.

Do not commit raw vendor datasets.

## Results

| Symbol | Interval | Provider | Date range | Bars | Result |
|---|---|---|---|---|---|
| META | 5m | polygon (Massive) | 2025-01-02 → 2026-09-23 | 33588 | recorded; final bearish_momentum (MEDIUM) |
| META | 1h | polygon (Massive) | 2025-01-02 → 2026-09-23 | 3015 | recorded; final range (LOW) |
| META | 1d | polygon (Massive) | 2025-01-02 → 2026-09-23 | 432 | recorded; final breakout_watch (HIGH) |
| NVDA | 5m | polygon (Massive) | 2025-01-02 → 2026-09-23 | 33588 | recorded; final breakout_watch (LOW) |
| NVDA | 1h | polygon (Massive) | 2025-01-02 → 2026-09-23 | 3015 | recorded; final breakout_watch (LOW) |
| NVDA | 1d | polygon (Massive) | 2025-01-02 → 2026-09-23 | 432 | recorded; final bullish_momentum (MEDIUM) |

## Evaluation results

These are descriptive statistics of what followed each technical state. **This is
not a backtest of profitability**: there are no orders, fills, costs or position
sizing. Each state is computed only from data through its bar; forward returns are
labels computed afterwards. Returns are close-to-close, and MFE/MAE use the highs
and lows within the horizon. Intraday horizons are session-bounded, meaning a label
is dropped when `t+h` falls in another session.

Caveats that apply to every table:

- two symbols over a single roughly 21-month period;
- several small samples, especially on 1d;
- overlapping horizons, so consecutive observations are not independent;
- no significance testing;
- no comparison against a random-entry baseline beyond the all-bars mean.

**No threshold has been changed on the basis of these numbers.**

### Cross-run summary

**State frequency (share of bars)**

| State | META 1d | NVDA 1d | META 1h | NVDA 1h | META 5m | NVDA 5m |
|---|---|---|---|---|---|---|
| bullish_setup | 5.6% | 21.8% | 8.0% | 12.2% | 13.9% | 15.0% |
| bullish_momentum | 2.8% | 5.8% | 2.6% | 2.9% | 3.5% | 3.9% |
| breakout_watch | 11.8% | 10.6% | 10.4% | 10.5% | 4.8% | 5.9% |
| bearish_setup | 16.2% | 3.9% | 9.3% | 6.5% | 14.4% | 12.7% |
| bearish_momentum | 3.5% | 0.7% | 1.6% | 2.1% | 3.8% | 3.2% |
| breakdown_watch | 10.0% | 7.4% | 10.3% | 10.3% | 5.0% | 5.8% |
| range | 13.2% | 13.2% | 16.1% | 14.5% | 20.0% | 20.8% |
| mixed | 32.6% | 32.2% | 41.0% | 40.4% | 34.6% | 32.7% |

**Mean 5-bar forward return in basis points** (n in parentheses). Compare each value
with the run's all-bars baseline, because the daily runs carry strong upward drift.

| State | META 1d | NVDA 1d | META 1h | NVDA 1h | META 5m | NVDA 5m |
|---|---|---|---|---|---|---|
| bullish_setup | -252.6 (24) | +87.4 (94) | +1.4 (53) | +23.0 (73) | -1.8 (4356) | -1.0 (4722) |
| bullish_momentum | -111.0 (12) | -211.0 (23) | +10.3 (16) | -39.5 (18) | -2.1 (1113) | -1.5 (1247) |
| breakout_watch | +86.3 (48) | +169.8 (45) | -6.9 (153) | -8.1 (155) | -1.1 (1520) | -1.6 (1821) |
| bearish_setup | +190.4 (70) | +650.3 (17) | +18.0 (56) | +48.2 (49) | -0.0 (4489) | -0.0 (3929) |
| bearish_momentum | -31.9 (15) | +188.6 (3) | -7.4 (12) | -49.0 (9) | +1.8 (1184) | -2.7 (1021) |
| breakdown_watch | -146.7 (43) | +246.6 (32) | +8.5 (149) | +1.4 (145) | +2.3 (1538) | +0.4 (1811) |
| range | +52.9 (55) | -264.4 (57) | +2.4 (125) | +10.6 (115) | +0.2 (6336) | +1.7 (6574) |
| mixed | +6.2 (141) | +157.5 (137) | +1.0 (288) | +6.4 (288) | +0.8 (10873) | -0.1 (10284) |
| *all bars (baseline)* | +33.0 (427) | +70.3 (427) | +2.8 (858) | +5.7 (858) | +0.2 (31428) | -0.0 (31428) |

### Observations (descriptive only; for review, not tuning)

1. **5-minute states show no forward-return separation.** With about 33,600 bars
   per symbol, every state's mean forward return is within about ±4 bps at 1, 3,
   5 and 10 bars, close to the ~0 bps baseline. The single exception is NVDA
   `bearish_momentum` at 10 bars (−8.2 bps, n=957), which is in the state's own
   direction but tiny. At this timescale the
   engine describes structure; the states do not anticipate the next few bars.
2. **1-hour results are limited by a harness choice.** Sessions have only 7 hourly
   bars, so session-bounded 10-bar labels never exist (n=0), and 5-bar labels come
   only from each session's first two bars. The 1h forward-return tables are
   therefore thin and biased toward the open. **Recommended harness change** (next
   phase): a `--no-session-bound` option, or intraday-appropriate horizons such as
   1/2/3 bars for 1h.
3. **Daily results are inconsistent across symbols and dominated by drift and small
   samples.**
   - The baseline is +33 bps (META) and +70 bps (NVDA) per 5 days.
   - `bullish_setup` was −253 bps (META, n=24) vs +87 bps (NVDA, n=94).
   - `bearish_setup` was positive for both, at +190 and +650 bps, but NVDA's n is
     only 17.
   - `breakout_watch` was above baseline for both (+86 and +170 bps).
   - `breakdown_watch` diverged: −147 bps (META) vs +247 bps (NVDA).

   No state's direction is consistently confirmed by what followed on 1d.
4. **Setup states lag structure (hypothesis).** On daily bars, setup states require
   confirmed HH/HL or LH/LL swings, which confirm `pivot_window` bars after the
   pivot. The META pattern (bullish setups followed by weakness, bearish setups by
   strength) is consistent with confirming moves that are largely complete. NVDA
   does not show the same pattern. Test this with more symbols and periods before
   drawing any conclusion.
5. **`mixed` is the most common state everywhere (33-41%), and `range` the second
   most common intraday (15-21%).** Both have near-baseline forward returns, which
   is consistent with no-agreement labels.
6. **The final state depends on the warm-up history.** For the 23 September bar, five
   of six evaluation final states match the live runner. **NVDA 1h does not**: the
   runner reported `mixed` (LOW) and the evaluation `breakout_watch` (LOW).
   - The runner warms 1h from 90 sessions of history. The evaluation replays from
     January 2025, so the set of confirmed pivots, and therefore the
     support/resistance levels, differs.
   - This is expected under the Phase 4C design, which is deterministic for a given
     window but not window-independent.
   - It matters for comparing persisted snapshots with evaluations: compare within
     the same warm-up policy.

### Per-run detail

### META 1d

- Provider: polygon (Massive); range 2025-01-02 → 2026-09-23; **432 bars**; horizons 1, 3, 5, 10 bars; session-bounded: false.
- Final state: breakout_watch (HIGH), trend range.

**State frequency and duration**

| State | Bars | Share | Runs | Mean run | Max run |
|---|---|---|---|---|---|
| bullish_setup | 24 | 5.6% | 8 | 3.00 | 7 |
| bullish_momentum | 12 | 2.8% | 5 | 2.40 | 5 |
| breakout_watch | 51 | 11.8% | 42 | 1.21 | 2 |
| bearish_setup | 70 | 16.2% | 23 | 3.04 | 10 |
| bearish_momentum | 15 | 3.5% | 7 | 2.14 | 4 |
| breakdown_watch | 43 | 10.0% | 38 | 1.13 | 2 |
| range | 57 | 13.2% | 23 | 2.48 | 8 |
| mixed | 141 | 32.6% | 56 | 2.52 | 12 |
| insufficient_data | 19 | 4.4% | 1 | 19.00 | 19 |

**Forward return by state** (mean / median / share positive; n = observations)

| State | Dir | 1-bar | 3-bar | 5-bar | 10-bar |
|---|---|---|---|---|---|
| bullish_setup | bullish | -0.41% / -0.42% / 25% (n=24) | -2.04% / -1.87% / 21% (n=24) | -2.53% / -2.35% / 33% (n=24) | -2.38% / -2.19% / 38% (n=24) |
| bullish_momentum | bullish | -0.35% / -0.65% / 50% (n=12) | -0.55% / +0.67% / 58% (n=12) | -1.11% / -0.42% / 42% (n=12) | -3.92% / -2.60% / 17% (n=12) |
| breakout_watch | bullish | +0.19% / +0.18% / 58% (n=50) | +0.86% / +0.73% / 59% (n=49) | +0.86% / +0.76% / 58% (n=48) | +0.43% / +0.39% / 57% (n=47) |
| bearish_setup | bearish | +0.53% / +0.26% / 56% (n=70) | +1.41% / +0.94% / 54% (n=70) | +1.90% / +2.01% / 61% (n=70) | +3.90% / +2.51% / 64% (n=70) |
| bearish_momentum | bearish | -0.88% / -0.19% / 40% (n=15) | -1.57% / -2.11% / 40% (n=15) | -0.32% / +1.84% / 60% (n=15) | +2.60% / +6.01% / 67% (n=15) |
| breakdown_watch | bearish | -0.19% / -0.19% / 40% (n=43) | -0.88% / -1.23% / 37% (n=43) | -1.47% / -1.82% / 33% (n=43) | +0.38% / -0.52% / 47% (n=43) |
| range | none | +0.18% / +0.21% / 51% (n=57) | +0.19% / -0.41% / 41% (n=56) | +0.53% / -0.03% / 49% (n=55) | -0.64% / -0.86% / 39% (n=51) |
| mixed | none | -0.00% / +0.19% / 55% (n=141) | +0.17% / +0.41% / 55% (n=141) | +0.06% / +0.13% / 51% (n=141) | -0.81% / -0.84% / 44% (n=141) |
| insufficient_data | none | +0.75% / +0.85% / 74% (n=19) | +2.12% / +2.48% / 79% (n=19) | +3.70% / +4.19% / 84% (n=19) | +8.42% / +8.66% / 95% (n=19) |
| *all bars (baseline)* | — | +0.08% mean (n=431) | +0.23% mean (n=429) | +0.33% mean (n=427) | +0.61% mean (n=422) |

**MFE / MAE by directional state** (mean, oriented to the state's direction)

| State | 1-bar MFE / MAE | 3-bar MFE / MAE | 5-bar MFE / MAE | 10-bar MFE / MAE |
|---|---|---|---|---|
| bullish_setup | +0.78% / -1.52% | +1.13% / -3.54% | +1.39% / -4.35% | +3.28% / -6.89% |
| bullish_momentum | +1.24% / -1.37% | +1.85% / -2.43% | +2.39% / -3.35% | +2.85% / -6.55% |
| breakout_watch | +1.65% / -0.92% | +2.99% / -2.13% | +4.35% / -2.94% | +6.46% / -5.03% |
| bearish_setup | +1.21% / -2.10% | +2.52% / -4.45% | +3.32% / -5.70% | +5.50% / -9.59% |
| bearish_momentum | +2.48% / -0.75% | +5.28% / -2.65% | +6.39% / -4.27% | +7.01% / -7.42% |
| breakdown_watch | +1.46% / -1.09% | +3.23% / -2.48% | +5.11% / -3.35% | +6.66% / -5.33% |

**Most frequent transitions** (202 total): `breakout_watch->mixed` 21, `mixed->breakout_watch` 21, `mixed->breakdown_watch` 19, `breakdown_watch->mixed` 18, `breakout_watch->range` 11, `range->breakout_watch` 9, `breakdown_watch->bearish_setup` 8, `range->mixed` 8.

### NVDA 1d

- Provider: polygon (Massive); range 2025-01-02 → 2026-09-23; **432 bars**; horizons 1, 3, 5, 10 bars; session-bounded: false.
- Final state: bullish_momentum (MEDIUM), trend mixed.

**State frequency and duration**

| State | Bars | Share | Runs | Mean run | Max run |
|---|---|---|---|---|---|
| bullish_setup | 94 | 21.8% | 20 | 4.70 | 38 |
| bullish_momentum | 25 | 5.8% | 10 | 2.50 | 8 |
| breakout_watch | 46 | 10.6% | 45 | 1.02 | 2 |
| bearish_setup | 17 | 3.9% | 10 | 1.70 | 3 |
| bearish_momentum | 3 | 0.7% | 3 | 1.00 | 1 |
| breakdown_watch | 32 | 7.4% | 27 | 1.19 | 3 |
| range | 57 | 13.2% | 18 | 3.17 | 9 |
| mixed | 139 | 32.2% | 52 | 2.67 | 9 |
| insufficient_data | 19 | 4.4% | 1 | 19.00 | 19 |

**Forward return by state** (mean / median / share positive; n = observations)

| State | Dir | 1-bar | 3-bar | 5-bar | 10-bar |
|---|---|---|---|---|---|
| bullish_setup | bullish | +0.11% / +0.18% / 53% (n=94) | +0.24% / +0.23% / 55% (n=94) | +0.87% / +0.86% / 62% (n=94) | +3.08% / +3.05% / 72% (n=94) |
| bullish_momentum | bullish | +0.00% / +0.60% / 54% (n=24) | -1.34% / -1.29% / 39% (n=23) | -2.11% / -2.87% / 26% (n=23) | -3.65% / -2.60% / 9% (n=23) |
| breakout_watch | bullish | +0.07% / +0.23% / 54% (n=46) | +0.83% / +0.63% / 64% (n=45) | +1.70% / +1.79% / 71% (n=45) | +3.02% / +1.49% / 64% (n=44) |
| bearish_setup | bearish | +1.55% / +0.91% / 65% (n=17) | +5.00% / +4.68% / 82% (n=17) | +6.50% / +4.58% / 94% (n=17) | +4.30% / +3.96% / 65% (n=17) |
| bearish_momentum | bearish | -0.61% / -1.55% / 33% (n=3) | -1.00% / -0.33% / 33% (n=3) | +1.89% / +2.62% / 67% (n=3) | +6.35% / +5.00% / 100% (n=3) |
| breakdown_watch | bearish | +0.50% / +0.77% / 59% (n=32) | +1.78% / +0.89% / 56% (n=32) | +2.47% / +2.36% / 66% (n=32) | +2.85% / +3.71% / 68% (n=31) |
| range | none | -0.78% / -0.57% / 39% (n=57) | -1.96% / -2.26% / 37% (n=57) | -2.64% / -3.04% / 30% (n=57) | -0.63% / -1.90% / 39% (n=57) |
| mixed | none | +0.49% / +0.40% / 57% (n=139) | +1.31% / +1.23% / 60% (n=139) | +1.57% / +1.79% / 64% (n=137) | +1.27% / +0.74% / 54% (n=134) |
| insufficient_data | none | -0.60% / -0.04% / 47% (n=19) | -2.76% / -3.13% / 47% (n=19) | -3.69% / -2.80% / 37% (n=19) | -4.00% / -3.20% / 32% (n=19) |
| *all bars (baseline)* | — | +0.15% mean (n=431) | +0.43% mean (n=429) | +0.70% mean (n=427) | +1.37% mean (n=422) |

**MFE / MAE by directional state** (mean, oriented to the state's direction)

| State | 1-bar MFE / MAE | 3-bar MFE / MAE | 5-bar MFE / MAE | 10-bar MFE / MAE |
|---|---|---|---|---|
| bullish_setup | +1.47% / -1.00% | +2.74% / -2.24% | +3.66% / -2.96% | +5.97% / -3.70% |
| bullish_momentum | +1.75% / -1.27% | +2.76% / -3.38% | +3.16% / -5.15% | +3.60% / -7.12% |
| breakout_watch | +1.34% / -1.48% | +3.10% / -2.45% | +4.64% / -3.03% | +7.15% / -4.07% |
| bearish_setup | +1.94% / -3.72% | +3.09% / -7.13% | +3.22% / -8.86% | +5.23% / -11.50% |
| bearish_momentum | +1.41% / -1.49% | +4.02% / -2.20% | +4.54% / -3.62% | +4.54% / -8.29% |
| breakdown_watch | +1.32% / -2.33% | +2.92% / -4.72% | +3.74% / -6.67% | +4.89% / -8.13% |

**Most frequent transitions** (185 total): `breakout_watch->mixed` 22, `mixed->breakout_watch` 21, `breakdown_watch->mixed` 14, `mixed->breakdown_watch` 13, `breakout_watch->bullish_setup` 10, `bullish_setup->breakout_watch` 10, `bullish_setup->mixed` 6, `mixed->range` 6.

### META 1h

- Provider: polygon (Massive); range 2025-01-02 → 2026-09-23; **3015 bars**; horizons 1, 3, 5, 10 bars; session-bounded: true.
- Final state: range (LOW), trend range.

**State frequency and duration**

| State | Bars | Share | Runs | Mean run | Max run |
|---|---|---|---|---|---|
| bullish_setup | 242 | 8.0% | 109 | 2.22 | 10 |
| bullish_momentum | 79 | 2.6% | 37 | 2.14 | 8 |
| breakout_watch | 314 | 10.4% | 275 | 1.14 | 4 |
| bearish_setup | 279 | 9.3% | 125 | 2.23 | 14 |
| bearish_momentum | 48 | 1.6% | 23 | 2.09 | 7 |
| breakdown_watch | 312 | 10.3% | 266 | 1.17 | 4 |
| range | 486 | 16.1% | 204 | 2.38 | 11 |
| mixed | 1236 | 41.0% | 459 | 2.69 | 13 |
| insufficient_data | 19 | 0.6% | 1 | 19.00 | 19 |

**Forward return by state** (mean / median / share positive; n = observations)

| State | Dir | 1-bar | 3-bar | 5-bar | 10-bar |
|---|---|---|---|---|---|
| bullish_setup | bullish | -0.03% / -0.00% / 50% (n=204) | -0.07% / -0.11% / 41% (n=123) | +0.01% / +0.06% / 53% (n=53) | n=0 |
| bullish_momentum | bullish | +0.03% / -0.04% / 45% (n=65) | +0.14% / -0.03% / 47% (n=38) | +0.10% / +0.28% / 62% (n=16) | n=0 |
| breakout_watch | bullish | +0.03% / +0.02% / 52% (n=287) | +0.05% / +0.03% / 50% (n=224) | -0.07% / -0.03% / 49% (n=153) | n=0 |
| bearish_setup | bearish | +0.01% / -0.03% / 48% (n=225) | -0.01% / -0.13% / 47% (n=131) | +0.18% / +0.11% / 54% (n=56) | n=0 |
| bearish_momentum | bearish | +0.03% / +0.09% / 61% (n=41) | +0.09% / +0.16% / 58% (n=24) | -0.07% / +0.03% / 50% (n=12) | n=0 |
| breakdown_watch | bearish | +0.04% / +0.00% / 50% (n=280) | +0.12% / +0.05% / 52% (n=221) | +0.08% / +0.04% / 52% (n=149) | n=0 |
| range | none | +0.01% / -0.01% / 48% (n=408) | +0.00% / -0.02% / 48% (n=276) | +0.02% / -0.00% / 50% (n=125) | n=0 |
| mixed | none | -0.01% / -0.02% / 47% (n=1056) | -0.01% / -0.02% / 49% (n=670) | +0.01% / -0.05% / 47% (n=288) | n=0 |
| insufficient_data | none | +0.17% / -0.04% / 47% (n=17) | +0.41% / +0.35% / 67% (n=12) | +0.76% / +0.85% / 83% (n=6) | n=0 |
| *all bars (baseline)* | — | +0.01% mean (n=2583) | +0.02% mean (n=1719) | +0.03% mean (n=858) | n=0 |

**MFE / MAE by directional state** (mean, oriented to the state's direction)

| State | 1-bar MFE / MAE | 3-bar MFE / MAE | 5-bar MFE / MAE | 10-bar MFE / MAE |
|---|---|---|---|---|
| bullish_setup | +0.33% / -0.35% | +0.57% / -0.63% | +0.83% / -0.91% | — / — |
| bullish_momentum | +0.40% / -0.38% | +0.73% / -0.61% | +0.96% / -0.86% | — / — |
| breakout_watch | +0.46% / -0.42% | +0.73% / -0.67% | +0.86% / -0.87% | — / — |
| bearish_setup | +0.40% / -0.41% | +0.69% / -0.66% | +0.90% / -1.06% | — / — |
| bearish_momentum | +0.47% / -0.40% | +0.69% / -0.75% | +0.93% / -0.94% | — / — |
| breakdown_watch | +0.42% / -0.45% | +0.68% / -0.76% | +0.91% / -0.95% | — / — |

**Most frequent transitions** (1498 total): `mixed->breakdown_watch` 139, `mixed->breakout_watch` 136, `breakout_watch->mixed` 134, `breakdown_watch->mixed` 124, `breakout_watch->range` 70, `range->breakout_watch` 70, `range->mixed` 63, `breakdown_watch->range` 56.

### NVDA 1h

- Provider: polygon (Massive); range 2025-01-02 → 2026-09-23; **3015 bars**; horizons 1, 3, 5, 10 bars; session-bounded: true.
- Final state: breakout_watch (LOW), trend bearish.

**State frequency and duration**

| State | Bars | Share | Runs | Mean run | Max run |
|---|---|---|---|---|---|
| bullish_setup | 368 | 12.2% | 140 | 2.63 | 17 |
| bullish_momentum | 87 | 2.9% | 46 | 1.89 | 6 |
| breakout_watch | 316 | 10.5% | 275 | 1.15 | 4 |
| bearish_setup | 197 | 6.5% | 98 | 2.01 | 7 |
| bearish_momentum | 63 | 2.1% | 31 | 2.03 | 7 |
| breakdown_watch | 311 | 10.3% | 273 | 1.14 | 3 |
| range | 437 | 14.5% | 177 | 2.47 | 8 |
| mixed | 1217 | 40.4% | 450 | 2.70 | 20 |
| insufficient_data | 19 | 0.6% | 1 | 19.00 | 19 |

**Forward return by state** (mean / median / share positive; n = observations)

| State | Dir | 1-bar | 3-bar | 5-bar | 10-bar |
|---|---|---|---|---|---|
| bullish_setup | bullish | +0.01% / +0.02% / 53% (n=306) | +0.09% / +0.06% / 54% (n=182) | +0.23% / +0.27% / 62% (n=73) | n=0 |
| bullish_momentum | bullish | -0.05% / +0.02% / 53% (n=73) | -0.15% / -0.24% / 38% (n=40) | -0.39% / -0.36% / 28% (n=18) | n=0 |
| breakout_watch | bullish | +0.04% / +0.03% / 52% (n=284) | -0.04% / -0.02% / 48% (n=233) | -0.08% / -0.10% / 46% (n=155) | n=0 |
| bearish_setup | bearish | +0.08% / +0.02% / 54% (n=170) | +0.20% / +0.12% / 56% (n=111) | +0.48% / +0.58% / 63% (n=49) | n=0 |
| bearish_momentum | bearish | -0.18% / -0.12% / 40% (n=48) | -0.59% / -0.74% / 23% (n=26) | -0.49% / -0.51% / 33% (n=9) | n=0 |
| breakdown_watch | bearish | +0.05% / +0.07% / 55% (n=274) | +0.13% / +0.21% / 57% (n=212) | +0.01% / +0.00% / 50% (n=145) | n=0 |
| range | none | +0.03% / +0.00% / 50% (n=371) | -0.00% / +0.03% / 52% (n=244) | +0.11% / +0.20% / 59% (n=115) | n=0 |
| mixed | none | -0.01% / +0.01% / 51% (n=1040) | +0.04% / +0.00% / 50% (n=659) | +0.06% / -0.01% / 49% (n=288) | n=0 |
| insufficient_data | none | +0.08% / +0.19% / 65% (n=17) | +0.03% / +0.08% / 58% (n=12) | +0.06% / +0.47% / 67% (n=6) | n=0 |
| *all bars (baseline)* | — | +0.01% mean (n=2583) | +0.04% mean (n=1719) | +0.06% mean (n=858) | n=0 |

**MFE / MAE by directional state** (mean, oriented to the state's direction)

| State | 1-bar MFE / MAE | 3-bar MFE / MAE | 5-bar MFE / MAE | 10-bar MFE / MAE |
|---|---|---|---|---|
| bullish_setup | +0.36% / -0.38% | +0.64% / -0.60% | +0.89% / -0.70% | — / — |
| bullish_momentum | +0.36% / -0.42% | +0.59% / -0.83% | +0.72% / -1.18% | — / — |
| breakout_watch | +0.53% / -0.50% | +0.74% / -0.82% | +0.87% / -1.03% | — / — |
| bearish_setup | +0.53% / -0.61% | +0.82% / -1.11% | +1.05% / -1.49% | — / — |
| bearish_momentum | +0.63% / -0.44% | +1.16% / -0.63% | +1.35% / -0.72% | — / — |
| breakdown_watch | +0.57% / -0.59% | +0.89% / -1.00% | +1.21% / -1.26% | — / — |

**Most frequent transitions** (1490 total): `mixed->breakdown_watch` 148, `breakdown_watch->mixed` 138, `breakout_watch->mixed` 128, `mixed->breakout_watch` 124, `breakout_watch->range` 60, `mixed->bullish_setup` 59, `bullish_setup->mixed` 57, `range->breakout_watch` 57.

### META 5m

- Provider: polygon (Massive); range 2025-01-02 → 2026-09-23; **33588 bars**; horizons 1, 3, 5, 10 bars; session-bounded: true.
- Final state: bearish_momentum (MEDIUM), trend mixed.

**State frequency and duration**

| State | Bars | Share | Runs | Mean run | Max run |
|---|---|---|---|---|---|
| bullish_setup | 4662 | 13.9% | 975 | 4.78 | 35 |
| bullish_momentum | 1161 | 3.5% | 452 | 2.57 | 17 |
| breakout_watch | 1625 | 4.8% | 1538 | 1.06 | 3 |
| bearish_setup | 4834 | 14.4% | 1004 | 4.81 | 40 |
| bearish_momentum | 1274 | 3.8% | 462 | 2.76 | 14 |
| breakdown_watch | 1667 | 5.0% | 1589 | 1.05 | 6 |
| range | 6713 | 20.0% | 1704 | 3.94 | 24 |
| mixed | 11633 | 34.6% | 2748 | 4.23 | 28 |
| insufficient_data | 19 | 0.1% | 1 | 19.00 | 19 |

**Forward return by state** (mean / median / share positive; n = observations)

| State | Dir | 1-bar | 3-bar | 5-bar | 10-bar |
|---|---|---|---|---|---|
| bullish_setup | bullish | -0.00% / -0.00% / 49% (n=4600) | -0.01% / -0.01% / 48% (n=4479) | -0.02% / -0.02% / 46% (n=4356) | -0.03% / -0.04% / 46% (n=4026) |
| bullish_momentum | bullish | -0.01% / -0.01% / 46% (n=1151) | -0.01% / -0.03% / 46% (n=1128) | -0.02% / -0.03% / 46% (n=1113) | -0.01% / -0.06% / 44% (n=1054) |
| breakout_watch | bullish | -0.00% / +0.00% / 51% (n=1598) | -0.01% / -0.02% / 47% (n=1547) | -0.01% / -0.01% / 49% (n=1520) | +0.00% / -0.04% / 47% (n=1455) |
| bearish_setup | bearish | -0.00% / +0.00% / 50% (n=4769) | +0.00% / +0.00% / 50% (n=4642) | -0.00% / +0.00% / 50% (n=4489) | +0.01% / +0.01% / 52% (n=4145) |
| bearish_momentum | bearish | +0.01% / +0.01% / 52% (n=1250) | +0.01% / +0.02% / 53% (n=1217) | +0.02% / +0.01% / 51% (n=1184) | +0.02% / +0.01% / 51% (n=1113) |
| breakdown_watch | bearish | +0.00% / +0.01% / 52% (n=1639) | +0.01% / +0.01% / 51% (n=1575) | +0.02% / +0.01% / 51% (n=1538) | +0.02% / +0.01% / 50% (n=1451) |
| range | none | -0.00% / +0.00% / 50% (n=6647) | -0.00% / -0.00% / 50% (n=6496) | +0.00% / -0.00% / 50% (n=6336) | +0.02% / +0.01% / 51% (n=5913) |
| mixed | none | +0.00% / -0.00% / 49% (n=11483) | +0.01% / -0.00% / 50% (n=11189) | +0.01% / -0.00% / 50% (n=10873) | +0.01% / -0.01% / 49% (n=10092) |
| insufficient_data | none | +0.07% / +0.04% / 63% (n=19) | +0.24% / +0.18% / 58% (n=19) | +0.31% / +0.10% / 63% (n=19) | +0.20% / +0.16% / 53% (n=19) |
| *all bars (baseline)* | — | +0.00% mean (n=33156) | +0.00% mean (n=32292) | +0.00% mean (n=31428) | +0.00% mean (n=29268) |

**MFE / MAE by directional state** (mean, oriented to the state's direction)

| State | 1-bar MFE / MAE | 3-bar MFE / MAE | 5-bar MFE / MAE | 10-bar MFE / MAE |
|---|---|---|---|---|
| bullish_setup | +0.11% / -0.12% | +0.19% / -0.21% | +0.25% / -0.27% | +0.34% / -0.37% |
| bullish_momentum | +0.15% / -0.16% | +0.25% / -0.28% | +0.32% / -0.35% | +0.44% / -0.47% |
| breakout_watch | +0.18% / -0.18% | +0.30% / -0.31% | +0.38% / -0.39% | +0.51% / -0.51% |
| bearish_setup | +0.13% / -0.13% | +0.22% / -0.22% | +0.28% / -0.27% | +0.39% / -0.38% |
| bearish_momentum | +0.14% / -0.14% | +0.24% / -0.25% | +0.31% / -0.32% | +0.42% / -0.44% |
| breakdown_watch | +0.18% / -0.18% | +0.29% / -0.29% | +0.37% / -0.37% | +0.49% / -0.50% |

**Most frequent transitions** (10472 total): `breakdown_watch->mixed` 663, `range->mixed` 640, `mixed->breakdown_watch` 634, `breakout_watch->mixed` 611, `mixed->breakout_watch` 611, `mixed->range` 552, `range->breakout_watch` 325, `breakdown_watch->range` 323.

### NVDA 5m

- Provider: polygon (Massive); range 2025-01-02 → 2026-09-23; **33588 bars**; horizons 1, 3, 5, 10 bars; session-bounded: true.
- Final state: breakout_watch (LOW), trend mixed.

**State frequency and duration**

| State | Bars | Share | Runs | Mean run | Max run |
|---|---|---|---|---|---|
| bullish_setup | 5031 | 15.0% | 1108 | 4.54 | 39 |
| bullish_momentum | 1297 | 3.9% | 491 | 2.64 | 22 |
| breakout_watch | 1967 | 5.9% | 1851 | 1.06 | 4 |
| bearish_setup | 4268 | 12.7% | 1017 | 4.20 | 34 |
| bearish_momentum | 1088 | 3.2% | 429 | 2.54 | 17 |
| breakdown_watch | 1944 | 5.8% | 1818 | 1.07 | 3 |
| range | 6996 | 20.8% | 1897 | 3.69 | 23 |
| mixed | 10978 | 32.7% | 2806 | 3.91 | 45 |
| insufficient_data | 19 | 0.1% | 1 | 19.00 | 19 |

**Forward return by state** (mean / median / share positive; n = observations)

| State | Dir | 1-bar | 3-bar | 5-bar | 10-bar |
|---|---|---|---|---|---|
| bullish_setup | bullish | -0.00% / -0.00% / 49% (n=4980) | -0.01% / -0.01% / 49% (n=4863) | -0.01% / -0.01% / 48% (n=4722) | -0.01% / -0.02% / 48% (n=4372) |
| bullish_momentum | bullish | +0.00% / -0.01% / 48% (n=1287) | +0.00% / -0.01% / 49% (n=1269) | -0.01% / -0.02% / 48% (n=1247) | -0.03% / -0.03% / 47% (n=1185) |
| breakout_watch | bullish | -0.01% / -0.00% / 49% (n=1916) | -0.01% / -0.00% / 50% (n=1839) | -0.02% / -0.02% / 48% (n=1821) | -0.00% / -0.01% / 50% (n=1732) |
| bearish_setup | bearish | -0.00% / +0.00% / 51% (n=4205) | +0.00% / +0.01% / 52% (n=4074) | -0.00% / +0.02% / 53% (n=3929) | +0.00% / +0.01% / 51% (n=3598) |
| bearish_momentum | bearish | -0.00% / +0.00% / 50% (n=1072) | -0.02% / +0.02% / 52% (n=1050) | -0.03% / +0.02% / 52% (n=1021) | -0.08% / -0.00% / 50% (n=957) |
| breakdown_watch | bearish | -0.00% / +0.01% / 52% (n=1916) | +0.01% / +0.01% / 51% (n=1861) | +0.00% / +0.01% / 51% (n=1811) | +0.00% / +0.04% / 53% (n=1709) |
| range | none | +0.01% / +0.00% / 50% (n=6912) | +0.01% / +0.00% / 51% (n=6743) | +0.02% / +0.00% / 50% (n=6574) | +0.04% / +0.01% / 51% (n=6177) |
| mixed | none | +0.00% / +0.00% / 50% (n=10849) | -0.00% / +0.00% / 50% (n=10574) | -0.00% / +0.01% / 51% (n=10284) | +0.00% / +0.02% / 52% (n=9519) |
| insufficient_data | none | +0.02% / +0.00% / 53% (n=19) | +0.21% / +0.11% / 58% (n=19) | +0.32% / +0.26% / 63% (n=19) | +0.41% / +0.40% / 74% (n=19) |
| *all bars (baseline)* | — | +0.00% mean (n=33156) | +0.00% mean (n=32292) | -0.00% mean (n=31428) | +0.00% mean (n=29268) |

**MFE / MAE by directional state** (mean, oriented to the state's direction)

| State | 1-bar MFE / MAE | 3-bar MFE / MAE | 5-bar MFE / MAE | 10-bar MFE / MAE |
|---|---|---|---|---|
| bullish_setup | +0.13% / -0.14% | +0.22% / -0.24% | +0.28% / -0.31% | +0.40% / -0.43% |
| bullish_momentum | +0.15% / -0.15% | +0.26% / -0.27% | +0.31% / -0.33% | +0.41% / -0.47% |
| breakout_watch | +0.20% / -0.22% | +0.33% / -0.36% | +0.41% / -0.44% | +0.56% / -0.61% |
| bearish_setup | +0.17% / -0.16% | +0.29% / -0.28% | +0.37% / -0.35% | +0.52% / -0.49% |
| bearish_momentum | +0.20% / -0.20% | +0.35% / -0.32% | +0.44% / -0.40% | +0.62% / -0.51% |
| breakdown_watch | +0.22% / -0.21% | +0.36% / -0.35% | +0.46% / -0.44% | +0.63% / -0.59% |

**Most frequent transitions** (11417 total): `mixed->breakdown_watch` 711, `breakdown_watch->mixed` 707, `breakout_watch->mixed` 696, `mixed->breakout_watch` 690, `range->mixed` 613, `mixed->range` 532, `breakout_watch->range` 439, `range->breakout_watch` 433.
