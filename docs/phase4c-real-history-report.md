# Phase 4C: Real-History Technical Evaluation Report

## Status: LIVE CHECK EXECUTED, FAILED; NO REAL-HISTORY RESULTS YET

**First attempt (2026-09-24, 20:21 America/New_York, free-tier development key, run
by the operator in their own terminal):** the live check failed, so no evaluation
could run.

- **Fractional volume.** Every response that got through (META 5m, the META 30m
  source for 1h/1d, and NVDA 5m) was rejected at its first result with
  `volume must be a whole number`. The vendor reports fractional-share volume,
  which the adapter wrongly treated as invalid. It is now fixed: volume is an
  exact `Decimal` end to end, and migration `0005_technical_fractional_vol`
  applies.
- **Rate limit.** After 5 successful requests, every request got HTTP 429 with no
  `Retry-After`. Retries were exhausted, and 17 requests were used in total. It is
  now fixed with client-side pacing (12 s default) and a 15 s fallback wait on 429.

As a result, this report still contains **no real-market results**. Nothing below
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
| META | 5m | — | — | — | NOT PRODUCED (first live check failed; rerun after fix) |
| META | 1h | — | — | — | NOT PRODUCED (first live check failed; rerun after fix) |
| META | 1d | — | — | — | NOT PRODUCED (first live check failed; rerun after fix) |
| NVDA | 5m | — | — | — | NOT PRODUCED (first live check failed; rerun after fix) |
| NVDA | 1h | — | — | — | NOT PRODUCED (first live check failed; rerun after fix) |
| NVDA | 1d | — | — | — | NOT PRODUCED (first live check failed; rerun after fix) |
