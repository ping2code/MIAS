# Phase 4C: Real-History Technical Evaluation Report

## Status: LIVE VALIDATION NOT EXECUTED

**Reason:** no market data credential was available to the Phase 4C process
environment. `MARKET_DATA_PROVIDER` and `MARKET_DATA_API_KEY` were both unset,
and `.env` is never read by design. The bounded live check was run and reported
exactly that:

```
$ python -m market_data.live_check
{"live_validation": "NOT EXECUTED", "reason": "MARKET_DATA_PROVIDER is not configured in the process environment"}
(exit code 2)
```

As a result, this report contains **no real-market results**. Nothing below is
estimated or simulated in place of real data:

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
| META | 5m | — | — | — | NOT EXECUTED (no credential) |
| META | 1h | — | — | — | NOT EXECUTED (no credential) |
| META | 1d | — | — | — | NOT EXECUTED (no credential) |
| NVDA | 5m | — | — | — | NOT EXECUTED (no credential) |
| NVDA | 1h | — | — | — | NOT EXECUTED (no credential) |
| NVDA | 1d | — | — | — | NOT EXECUTED (no credential) |
