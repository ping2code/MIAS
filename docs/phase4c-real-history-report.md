# Phase 4C: Real-History Technical Evaluation Report

## Status: LIVE CONTRACT VERIFIED; REAL-HISTORY EVALUATION NOT YET RUN

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

The evaluation runs (`evaluation.technical_replay`) have **not** been executed yet, so this report contains
**no real-history statistics**. Nothing below
is estimated or simulated in place of real data:

- no META or NVDA state counts;
- no transition counts;
- no forward-return summaries;
- no MFE (Maximum Favorable Excursion) or MAE (Maximum Adverse Excursion)
  summaries.

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
| META | 5m | — | — | — | NOT YET RUN (contract verified; run `evaluation.technical_replay`) |
| META | 1h | — | — | — | NOT YET RUN (contract verified; run `evaluation.technical_replay`) |
| META | 1d | — | — | — | NOT YET RUN (contract verified; run `evaluation.technical_replay`) |
| NVDA | 5m | — | — | — | NOT YET RUN (contract verified; run `evaluation.technical_replay`) |
| NVDA | 1h | — | — | — | NOT YET RUN (contract verified; run `evaluation.technical_replay`) |
| NVDA | 1d | — | — | — | NOT YET RUN (contract verified; run `evaluation.technical_replay`) |
