# Phase 7B.1 — Market Context Engine

Market Context produces **deterministic, provider-independent market-context facts**:
- returns and benchmark-relative returns;
- the current session's range and position within it;
- session volume;
- MIAS VWAP;
- data freshness and provenance.

It has no labels, no interpretation, no signals, no scores and no trading or options recommendations, and it calls no
AI or LLM.

`context_format_version = "phase7b-v1"`; the context interval is **5m** only.

## Components

| File | Role |
|---|---|
| `market_context/models.py` | immutable `SeriesInput`, `Freshness`, `SeriesContext`, `BenchmarkComparison`, `MarketContext`; deterministic `to_dict()` |
| `market_context/engine.py` | **pure**: completed bars + explicit `now`, `as_of` and `ExchangeCalendar` → facts; never calls a provider or reads a clock |
| `market_context/freshness.py` | freshness metadata from timestamps, the configured delay and the calendar |
| `market_context/runner.py` | the only provider boundary: fetches each unique symbol once and prints JSON |

- No persistence, migration, scheduler or evidence-ledger integration.
- No existing file was modified.
- The package never imports `evaluation` or `evidence` (a subprocess test enforces this).

## Input contract

`SeriesInput(symbol, interval="5m", bars, provider_id, delay_seconds)` holds normalized `MarketBar`s. The engine
validates them with existing public helpers:
- `validate_series`: one symbol and interval, strictly increasing, no duplicates;
- `completion.split_completed`: every bar must be completed at `as_of`, and a forming bar raises `MarketDataError`;
- `ExchangeCalendar.classify` and `bar_end`.

`now` and `as_of` must be timezone-aware, with `as_of ≤ now`.

## Regular-session rule

Only bars that `calendar.classify` places in the **regular** session count. Pre-market and after-hours bars are
ignored for every metric: returns, OHLC, volume and VWAP.

The **context session** is the latest regular session containing at least one completed regular bar at `as_of`.

## Exact financial semantics

All returns are **simple returns in exact `Decimal`**:
- a local context of 28 digits;
- quantized to **10 decimal places** with **`ROUND_HALF_EVEN`**;
- stored as **fractions**: `0.0125` = +1.25%. They are never stored as percentage points.

Serialization uses the MIAS canonical text, `format_decimal`, which has no exponent and no trailing zeros. So
`0.0125000000` is written `"0.0125"`.

| Fact | Definition |
|---|---|
| `previous_close` | Close of the **last completed regular 5m bar** of `calendar.previous_trading_day(session_date)`. This is the existing MIAS previous-session semantic, as in `technical.engine._gaps`. Weekends and holidays are skipped by the calendar. There is no vendor "official close" |
| `return_since_prev_close` | `latest_close / previous_close − 1`. `None` with `no_previous_session` when the immediately previous trading session is absent from the supplied bars |
| `return_since_open` | `latest_close / session_open − 1`, where `session_open` is the first regular bar's open |
| `session_open/high/low`, `latest_close`, `session_volume` | `market_data.aggregation.aggregate(session bars, "1d", calendar)`, the existing regular-session aggregator: first open, max high, min low, last close, exact Decimal volume sum |
| `session_range` | `session_high − session_low` (exact) |
| `position_in_range` | `(latest_close − session_low) / (session_high − session_low)`, quantized. 0 = at the low, 1 = at the high. `None` with `zero_range` when high == low |
| `distance_from_high` | `(session_high − latest_close) / latest_close` (≥ 0, quantized) |
| `distance_from_low` | `(latest_close − session_low) / latest_close` (≥ 0, quantized) |
| `bars_completed` | completed regular bars in the context session |
| `bars_expected_so_far` | regular-session grid bars ending at or before `as_of`. The last bar is truncated at the close, so early closes come from the calendar |

## Benchmarks and relative returns

- Benchmarks are passed explicitly. The runner default is `SPY,QQQ`.
- There are no sector, industry or symbol-to-benchmark mappings and no benchmark registry.
- A requested symbol that is also a benchmark is skipped with reason `self`.

For each benchmark and each basis (`prev_close`, `open`):

```
relative_return = symbol_return − benchmark_return
```

This is a difference of quantized **fractional** returns, e.g. 0.0125 − 0.0040 = **0.0085**. A presentation layer may
display it as +0.85 percentage points. It is **a fact, not a trading signal**.

### Common-cutoff comparison

- `cutoff = min(latest completed regular bar_end of symbol, of benchmark)`.
- Each series is evaluated at its last bar with `bar_end ≤ cutoff`, and both evaluated ends are reported
  (`symbol_bar_end`, `benchmark_bar_end`).
- If either evaluated end is earlier than the cutoff (a missing bar):
  - `aligned = false`;
  - reason `misaligned`;
  - `alignment_gap_seconds` is reported;
  - `relative_return = None`, because timestamps are never silently mixed. The individual returns are still reported.
- Different context sessions give reason `session_mismatch`, and nothing is compared.
- Comparison returns are measured at the cutoff. They can therefore differ from the `SeriesContext` returns, which are
  measured at each series' own latest bar.

## VWAP (formula owned by the technical engine)

`vwap` is the value of **`technical.indicators.vwap`** (unchanged) at the latest regular bar:

```
VWAP = Σ(((H+L+C)/3) × volume) / Σ volume
```

- It resets at each regular session and covers regular-session bars only.
- It is computed in float, exactly as in technical snapshots.
- `close_vs_vwap = (latest_close − vwap) / vwap`, also float.
- If the session has no traded volume, both are `None` with `no_vwap`.
- **MIAS session VWAP is not the vendor aggregate `vw`.** The vendor's `vw` is a per-bar trade-weighted price, and it
  is not exposed through `MarketBar`.

There is no Decimal VWAP and no second VWAP formula.

## Volume

Phase 7B.1 exposes only three volume facts:
- `session_volume`;
- `bars_completed`;
- `bars_expected_so_far`.

**Limitations:**
- Partial-session volume must not be compared with full-day averages.
- The technical engine's `relative_volume` measures one bar against the previous 20 bars. It is **not** a session
  relative-volume metric, and Market Context does not use it.
- **Time-of-day relative volume** (cumulative volume up to a given time versus prior sessions up to the same time) is
  **deferred to a possible, separately reviewed Phase 7B.2**.

## Freshness

Freshness is explicit metadata computed from:
- `now` and `as_of`, where `as_of = now − configured delay`;
- `configured_delay_seconds`;
- the interval;
- the calendar;
- `latest_bar_end` and `expected_latest_bar_end`, the latest regular grid bar end ≤ `as_of`.

It also reports `age_seconds = now − latest_bar_end` and `lag_bars`, the number of grid bar ends in
`(latest_bar_end, expected_latest_bar_end]`.

| Status | Rule |
|---|---|
| `current` | `lag_bars = 0` and the market is in its regular session |
| `market_closed` | `lag_bars = 0` and the market is pre-market, post-market or closed |
| `lagging` | `lag_bars ≥ 1` |
| `no_data` | no completed regular bar |
| `unknown` | off-grid latest bar, or not countable |

- **There is no `stale` threshold.**
- No vendor-specific delay is assumed; the delay is the configured `MARKET_DATA_DELAY_SECONDS`. The Massive Starter
  market-hours delay is still pending measurement on 2026-09-28.
- Freshness **never changes any metric**.

## Missing data (explicit reason codes; values are never invented)

| Code | Where | Meaning |
|---|---|---|
| `no_data` | series | no completed regular bar |
| `no_symbol_data` / `no_benchmark_data` | comparison | that side has no data (e.g. provider error or no bars) |
| `no_previous_session` | series / comparison | the immediately previous trading session is absent |
| `zero_range` | series | high == low; `position_in_range = None` |
| `no_vwap` | series | no volume in the session |
| `self` | comparison | benchmark == symbol |
| `session_mismatch` | comparison | different context sessions |
| `misaligned` | comparison | a series lacks the bar ending at the cutoff |

- Contract violations raise `MarketDataError`: a forming bar, a mixed or unordered series, the wrong interval or
  symbol, a naive or inverted clock.
- A provider failure in the runner empties only that series.

## Runner

```
python -m market_context.runner --symbols META,NVDA,MSFT [--benchmarks SPY,QQQ] [--out FILE]
```

- It reads the clock once and uses `as_of = now − MARKET_DATA_DELAY_SECONDS`.
- It fetches the union of symbols and benchmarks, **each exactly once**, as 5m bars. The window runs from midnight
  exchange time two trading sessions before the latest trading day, up to `as_of`. That is one vendor page per symbol.
  The example above makes **5 requests**.
- Only in-memory, per-run caching. No Redis, database, evidence ledger or persistence.
- Output is JSON: `contexts`, `errors` (symbol → provider error kind) and `provider_requests`. It never contains the key.
- Exit codes: 0 OK; 1 any provider or data failure (partial context still printed); 2 configuration or usage error.

## Boundary with the technical engine

Market Context never recomputes RSI, EMA, ATR, structure, levels, breakouts, technical state or confidence; those
stay with `technical/*` (phase4c-v2, unchanged). It only calls pure shared helpers (`aggregate`, `vwap`, calendar,
completion, validation).

Phase 6 (frozen hypotheses, registry and protocol hashes, prospective start, evidence ledger, migration 0007) and
Phase 7A (the providers) are untouched.
