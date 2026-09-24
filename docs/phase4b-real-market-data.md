# Phase 4B: Real Market Data and Incremental Technical Engine

Phase 4B extends the Phase 4 technical engine (see
`docs/phase4-technical-signals.md`) so MIAS (Market Intelligence Alert System)
can:

- consume real historical market bars;
- understand exchange sessions and holidays;
- update one completed bar at a time;
- report several timeframes side by side.

It remains **decision support only**. It has no orders, no broker APIs
(Application Programming Interfaces), no options, no Telegram and no fusion
with the News, SEC (U.S. Securities and Exchange Commission), Fed (Federal
Reserve), Macro, Treasury or Geopolitical collectors. Phase 4 indicator formulas
are unchanged.

New and changed code:

| Path | Purpose |
|---|---|
| `market_data/calendar.py` | XNYS (New York Stock Exchange) calendar, session windows, calendar-aware classification |
| `market_data/config.py` | environment-backed provider settings |
| `market_data/http.py` | bounded, credential-safe HTTPS (Hypertext Transfer Protocol Secure) JSON (JavaScript Object Notation) client |
| `market_data/providers/polygon.py` | Polygon.io / Massive aggregates adapter |
| `market_data/aggregation.py` | session-respecting bar aggregation |
| `market_data/completion.py` | completed vs forming bars |
| `market_data/validation.py` | adds `validate_calendar_series` |
| `market_data/models.py` | adds `Session.CLOSED` |
| `technical/incremental.py` | `IncrementalTechnicalEngine` |
| `technical/states.py` | descriptive state helpers shared by both engines |
| `technical/multitimeframe.py` | `MultiTimeframeSnapshot` builders |
| `technical/runner.py` | one-shot job entry point |
| `technical/engine.py` | optional calendar-aware gaps; bounded pivot history for levels |
| `technical/formatter.py` | adds the multi-timeframe view |
| `evaluation/` | replay harness, metrics, performance check |

## 1. Provider choice

**Polygon.io, now rebranded Massive: the stock aggregates REST
(Representational State Transfer) endpoint**,
`GET /v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from}/{to}`.

Alternatives considered:

| Option | Why not chosen for Phase 4B |
|---|---|
| Yahoo chart endpoints | unofficial and undocumented, with unclear licensing (excluded by the Phase 4B rules) |
| Alpaca market data | documented and has a free tier, but it is a brokerage platform. Keeping any brokerage account/API out of MIAS avoids any path towards order APIs |
| Alpha Vantage | documented free tier, but its daily request quota (about 25/day) is too small for 5m/1h/1d warm-ups on two symbols |
| Tiingo / Twelve Data | viable documented alternatives; the provider interface allows adding either later |

Polygon/Massive has:

- a documented, versioned API;
- OHLCV (Open, High, Low, Close, Volume) aggregates for all U.S. stocks,
  including META and NVDA;
- intraday history on its free tier (limited request rate, about 5/minute, and
  limited history depth);
- key-based authentication through an HTTP header.

## 2. Licensing and API considerations

- The free tier is for personal and non-commercial use. MIAS uses it for
  development and research, not for redistribution. The operator must accept the
  vendor's terms before creating a key.
- **No vendor data is committed.** All test data is synthetic
  (`tests/market_data_fakes.py`, `tests/technical_fixtures.py`). Evaluation output
  holds statistics only, never raw bars.
- Vendor plans differ in history depth, delay and real-time access. MIAS treats
  data as available only up to `now - MARKET_DATA_DELAY_SECONDS` (default 15
  minutes) and never assumes real time.
- The adapter sends the key only as `Authorization: Bearer …`. It never puts the
  key in a query string, so it cannot leak through URLs.
- Pagination links (`next_url`) are followed only to the configured host over
  HTTPS.
- **Live validation was not performed in Phase 4B**: no credential is available to
  this environment, and `.env` is never read. Section 17 lists the exact steps.

## 3. Credentials and configuration

Settings are read from the process environment by
`market_data.config.load_market_data_settings(os.environ)`. The loader never
reads `.env`, and it validates every value. Errors name the setting but never its
value. `safe_view()` reports the key only as `set` or `unset`, and the settings'
`repr` hides it.

| Variable | Default | Range / values |
|---|---|---|
| `MARKET_DATA_PROVIDER` | `none` | `none`, `polygon` (alias `massive`) |
| `MARKET_DATA_API_KEY` | unset | required when the provider is not `none` |
| `MARKET_DATA_BASE_URL` | `https://api.polygon.io` | plain `https://` origin (the Massive host can be set here) |
| `MARKET_DATA_HTTP_TIMEOUT_SECONDS` | 10 | 1-120 |
| `MARKET_DATA_MAX_RETRIES` | 3 | 0-5 |
| `MARKET_DATA_RETRY_BACKOFF_SECONDS` | 1.0 | 0-60 (doubles per retry) |
| `MARKET_DATA_MAX_RATE_LIMIT_WAIT_SECONDS` | 60 | 1-600 |
| `MARKET_DATA_DELAY_SECONDS` | 900 | 0-86400 |
| `MARKET_DATA_ADJUSTED` | true | split-adjusted bars |
| `MARKET_DATA_INCLUDE_EXTENDED_HOURS` | false | keep pre/post-market intraday bars |

**HTTP behaviour** (`market_data/http.py`):

- There is a timeout on every request, and redirects are never followed.
- At most `MARKET_DATA_MAX_RETRIES` retries, and only for transient failures:
  connection errors, timeouts, HTTP 500/502/503/504 and 429 (rate limit).
- Backoff is `backoff x 2^attempt`. For 429, a numeric `Retry-After` header is
  honoured, capped at `MARKET_DATA_MAX_RATE_LIMIT_WAIT_SECONDS`.
- **Not retried:** 401/403 (authentication or entitlement), other 4xx, redirects
  and malformed JSON.
- Log lines are `event=market_data_retry target=<host/path> reason=… attempt=…`.
  Query strings, headers and keys never appear.

## 4. Market calendar

The source is the maintained **`exchange_calendars`** package (4.13.2, calendar
`XNYS`), pinned in `requirements.txt` with its dependencies (pandas, numpy and
others). MIAS maintains no holiday list. The package provides:

- trading days;
- regular and observed holidays (for example, Friday 3 July 2026 for Independence
  Day on a Saturday);
- ad-hoc closures;
- early closes (13:00 on the day after Thanksgiving, on Christmas Eve, and on
  3 July when applicable).

The calendar is built once per process with **fixed bounds** (2005-01-03 to
2035-12-31), so results never depend on the day the process starts. Dates outside
the bounds raise `MarketDataError`. Future years are rule-based projections and
cannot include future ad-hoc closures.

API (`market_data.calendar.default_calendar()`):

- `session_times(day)`
- `is_trading_day`
- `previous_trading_day` / `next_trading_day`
- `trading_days(start, end)`
- `classify(ts)`
- `bar_end(bar)`

## 5. Sessions

All sessions use America/New_York exchange time, with DST (Daylight Saving
Time) handled by `zoneinfo`. For example, the open is 14:30 UTC (Coordinated
Universal Time) on 6 March 2026 and 13:30 UTC on 9 March 2026.

| Session | Window on a trading day |
|---|---|
| `pre` | 04:00 to the open |
| `regular` | 09:30 to 16:00, or 13:00 on an early-close day |
| `post` | the close + 4 hours (20:00, or 17:00 on an early-close day) |
| `closed` | anything else, and all of a non-trading day |

The extended-hours windows are a MIAS convention matching common U.S. electronic
venues.

- **Phase 4 compatibility:** `market_data.models.classify_session` (weekday rules,
  no calendar) is unchanged. `Session.CLOSED` is new and is set only by
  calendar-aware code.
- **VWAP (Volume Weighted Average Price):** the formula is unchanged. It still
  resets per regular session and excludes pre/post-market bars. With
  calendar-labelled bars:
  - there is no carry across weekends or holidays (they have no bars);
  - on a half-day, bars from 13:00 are `post` and are excluded.
- **Gaps:** a gap compares the current regular open with the **previous trading
  session's** final regular close, found via `previous_trading_day`, never the
  previous calendar day. The tests cover:
  - Monday vs Friday;
  - the Tuesday after Labor Day vs the Friday before;
  - Monday vs the prior half-day, using its 12:30-13:00 regular bar's close, not
    post-market.

  If the previous session is missing from the data, the gap is **unknown (None)**.
  It is never measured against an older session. Daily gaps require the previous
  bar to be the previous trading day. Without a calendar, Phase 4 behaviour is
  unchanged.
- **Validation** (`validate_calendar_series`) rejects:
  - bars on holidays, weekends or overnight (`closed`);
  - session labels that disagree with the calendar;
  - intraday bars off their session grid (for example, a clock-aligned 10:00
    hourly bar, whose 09:00 sibling would span the open);
  - daily bars not stamped at midnight exchange time on a trading day.

## 6. Incremental architecture

`IncrementalTechnicalEngine.update(bar) -> TechnicalSnapshot` keeps independent
state per (symbol, interval) and replays nothing.

- EMA (Exponential Moving Average), RSI (Relative Strength Index), ATR (Average
  True Range), VWAP, volume, pivots, labels, levels, breakouts and gaps are all
  advanced from compact state.
- Descriptive labels (EMA, VWAP and momentum states, gap classification) come from
  `technical/states.py`, which the replay engine also uses. All numeric work is
  computed independently by the two engines.

**Equivalence:** the incremental snapshot after every bar equals
`TechnicalEngine.replay` **field for field with zero tolerance** (`==` on the
frozen dataclasses). This holds because both engines perform the same
floating-point operations in the same order. Python 3.12+ `sum()` uses compensated
summation, so warm-up seeds are summed with `sum()` over the same values in both
engines, not with a running `+=`. The tests cover:

- every bar of all 10 Phase 4 scenarios, for both META and NVDA, with and without
  the calendar;
- three alternative configurations;
- a 30-session sequence that exercises the pivot-history bound;
- daily bars.

## 7. Memory bounds

Retained state per series is independent of the number of bars processed:

| Component | Bound |
|---|---|
| EMA (each period) | up to N seed values, released after warm-up; then 1 value |
| RSI / ATR | up to N seed values, released; then the running averages |
| VWAP | 2 accumulators for the current session |
| volume | `volume_lookback` (20) volumes |
| pivot window | `2 x pivot_window + 1` (5) bars |
| pivots for levels | `max_pivot_history` (200) confirmed pivots |
| latest labelled high/low, pivot counts | constant |
| breakouts | `failure_lookback` (3) closes and at most 4 events |
| previous levels, latest snapshot | at most 100 levels (a level needs at least 2 pivots) and 1 snapshot |

`max_pivot_history` is a new `TechnicalConfig` field (default 200). The replay
engine applies the same rule, so levels come from the most recent 200 confirmed
pivots in both. Phase 4 fixtures never reach 200 pivots, so their results are
unchanged. Structure counts still cover all pivots.

The tests assert the container sizes after thousands of bars, and use
`tracemalloc` to check that another 800 bars add no retained memory.

## 8. Warm-up and restart

- `warmup(bars)` validates a historical series and feeds it through `update`,
  returning the last snapshot.
- A restart is a warm-up: a fresh engine warmed with the same bars reaches
  identical state.
- The tests warm up to several cut points, then feed the remaining bars live. Every
  live snapshot equals the full replay.

**Out-of-order bars:**

- a bar with the last bar's timestamp raises `DuplicateBarError`;
- an older bar raises `OutOfOrderBarError`;
- both are `MarketDataError` subclasses, and a rejected bar never changes state;
- with a calendar, each bar also passes `validate_calendar_series`;
- history is never rewritten in live mode. Use `TechnicalEngine.replay` for
  historical recomputation.

## 9. Multi-timeframe analysis

`MultiTimeframeSnapshot(symbol, timestamp, timeframes, missing)`:

- `timeframes` maps `1d`, `1h` and `5m` (configurable) to that timeframe's own
  `TechnicalSnapshot`, or to None.
- `missing` gives the reason for every None, such as
  `"no completed bars processed"`.

There is **no composite field, score or combined state**: 1d never overrides 5m,
and nothing is BUY or SELL. `analyze_timeframes(...)` and `from_engine(...)`
build it. `format_multi_timeframe` prints each timeframe independently:

```
META

Daily:
  State: bullish_setup (confidence MEDIUM)
  Structure: HH / HL (bullish)
  RSI: 54.6
  Price: 849.17
  Bar: 2026-09-23T00:00:00-04:00

1h:
  State: mixed (confidence LOW)
  Structure: LH / LL (bearish)
  RSI: 50.0
  Price: 849.17, above vwap
  Bar: 2026-09-23T15:30:00-04:00

5m:
  State: bullish_setup (confidence HIGH)
  Structure: HH / HL (bullish)
  RSI: 59.8
  Price: 769.22, above vwap
  Bar: 2026-09-23T15:55:00-04:00

Timeframes are independent; no combined signal is produced.
```

This is `python -m technical.runner --symbols META --text` output against the synthetic fake-HTTP data in
`tests/test_technical_runner.py`, not real prices. The 5m series is generated independently of the 30m series, so the
prices differ.

## 10. Aggregation

`market_data.aggregation.aggregate(bars, target, calendar)`:

- open = first, high = max, low = min, close = last, volume = sum.
- **Intraday targets** are anchored at the session segment start (04:00 pre, the
  regular open, the close for post):
  - hourly regular buckets are 09:30, 10:30, … 15:30;
  - the last bucket is truncated at the segment end (15:30-16:00; 12:30-13:00 on a
    half-day);
  - buckets never span two segments or two days.
- **Daily** bars come from regular-session bars only, stamped at midnight exchange
  time.
- The target must be a whole multiple of the source interval.
- Missing source bars are never invented.

The provider uses aggregation for **1h** (from 30m) because the vendor's hourly
bars are clock-aligned (09:00-10:00 mixes pre-market and regular trading). It also
uses it for **1d** (from regular-session 30m bars), so daily bars follow the same
session model.

## 11. Completed-bar policy

- A bar is **completed** when its end is at or before the data cut-off
  (`now - MARKET_DATA_DELAY_SECONDS`). Its end is `calendar.bar_end`: start +
  interval, truncated at the segment end; the close for daily bars.
- An aggregated bucket must also end within the coverage of the source data it was
  built from (`derive_completed`). A bucket whose final source bars have not
  arrived stays **forming**.
- Providers return completed bars only, and the engine never sees a forming bar. So
  no pivot, breakout or state is derived from an incomplete bar. There is no
  "preliminary" mode in Phase 4B.

## 12. Evaluation harness

`evaluation/technical_replay.py` and `evaluation/metrics.py` describe states over
historical bars. **This is not a profitability backtest**: there are no orders,
fills, costs, sizing or options P&L (profit and loss). The harness reports:

- state frequency, run durations and transition counts;
- for each state and each horizon (1, 3, 5 and 10 bars):
  - the forward return `close[t+h] / close[t] - 1`;
  - `max_up` / `max_down` excursions;
  - for directional states, MFE (Maximum Favorable Excursion) and MAE (Maximum
    Adverse Excursion), oriented to the state's direction.

With `session_bounded` (the default for intraday), a label is None when `t+h`
falls in another session, so no overnight hold is implied.

CLI (Command-Line Interface):
`python -m evaluation.technical_replay --symbol META --interval 5m --start … --end …`
prints JSON statistics only. `python -m evaluation.performance` runs the
performance sanity check (section 17).

**No threshold tuning:** no RSI zone, pivot window, buffer, volume threshold or
confidence rule was changed in Phase 4B. The harness collects evidence for a later
tuning phase.

## 13. No-look-ahead guarantees

- States come from the causal replay engine. Its `replay[i] == analyze(bars[:i+1])`
  property is proven in Phase 4, and the incremental engine now matches it on
  every bar.
- Forward labels are computed **after** and **separately** from the states; they
  never enter signal calculation. The tests show that:
  - truncating the data leaves earlier states unchanged;
  - changing the evaluation horizons changes no state.
- The completed-bar policy keeps forming bars, and bars past the data cut-off, out
  of the engine.
- Gaps and VWAP use only past sessions (sections 5 and 11).

## 14. Provider limitations

- **Not live-validated** (no credential in this environment); only the HTTP layer
  is tested, with fakes. Field names (`t`, `o`, `h`, `l`, `c`, `v`, `status`,
  `next_url`) follow the vendor documentation. A live check (section 17) should
  confirm them before any scheduled use.
- Free tiers are rate-limited (about 5 requests/minute) and delayed. The runner
  uses 2 requests per symbol (5m, plus 30m for 1h and 1d). Bursts beyond the limit
  wait on 429 `Retry-After`, bounded.
- Vendor volume must be a whole number; fractional values are rejected rather than
  rounded. If a plan returns fractional volumes, that must be decided explicitly.
- Minute aggregates approximate official auction prints: daily open and close
  built from 30m bars can differ slightly from official exchange values.
- A final bucket whose last source minutes had no trades stays forming until a
  later source bar extends the coverage.
- Adjusted (split-adjusted) prices can change retroactively after corporate
  actions. Warm-ups re-fetch history, so a restart reflects the adjustment.

## 15. Scheduler integration

**Deferred; the scheduler is unchanged.** The provider has not been validated
against the live API, and the prompt allows scheduler integration only once the
provider and the incremental engine are stable. Adding a registry entry would also
change the six-collector registry and its tests.

Instead, Phase 4B ships the job entry point the scheduler would call:

```
python -m technical.runner [--symbols META,NVDA] [--timeframes 1d,1h,5m] [--text]
```

It:

- reads provider settings from the environment;
- fetches completed bars (2 requests per symbol);
- warms a fresh incremental engine;
- logs `event=technical_snapshot symbol=… interval=… bars=… last_bar=… state=…
  confidence=… trend=…`.

It never sends to Telegram, makes no alert decision and persists nothing. Exit
codes are 0 success, 1 provider/data failure (other symbols still run) and 2
configuration/usage error. It is one-shot, not a daemon.

A later phase can register it in `orchestrator/registry.py` behind
`TECHNICAL_SCHEDULE_ENABLED=false`.

## 16. Snapshot persistence decision

**Not added: it would need a new migration.** The existing schema (`events`,
`event_versions`, `provenance`, the macro shadow history and the geopolitical
anchor registry) is event-centric. For example, `event_versions` requires a
headline, publisher and publication basis. Storing technical snapshots there would
misuse those tables. Following the Phase 4B rule, MIAS stops here and reports
instead of adding schema. A dedicated `technical_snapshots` table is a Phase 4C
decision.

## 17. Remaining risks

- **Live provider unverified:** run a bounded live check with a development key
  before any scheduled use:

  ```
  MARKET_DATA_PROVIDER=polygon MARKET_DATA_API_KEY=<key> \
      python -m technical.runner --symbols META,NVDA
  ```

  Record only symbol, interval, date range, bar count, validation result and final
  state.
- **Calendar projection:** future ad-hoc closures (for example, national days of
  mourning) are unknown until the package is updated. Keep `exchange_calendars`
  current.
- **Performance** (`python -m evaluation.performance`, synthetic 5m NVDA-style bars):

  | Sessions | Bars | Replay | Incremental | Incremental per bar | Replay peak memory | Incremental peak memory |
  |---|---|---|---|---|---|---|
  | 20 | 1,560 | 2.26 s | 2.08 s | 1.34 ms | 11.1 MiB | 0.10 MiB |
  | 60 | 4,680 | 9.42 s | 8.11 s | 1.73 ms | 39.7 MiB | 0.11 MiB |
  | 120 | 9,360 | 21.64 s | 18.20 s | 1.94 ms | 79.5 MiB | 0.13 MiB |
  | 240 | 18,648 | 50.33 s | 33.76 s | 1.81 ms | 149.8 MiB | 0.16 MiB |

  Memory is constant for incremental and linear for replay. Incremental per-bar
  time plateaus (about 1.8 ms once 200 pivots are retained), while replay per-bar
  time keeps rising (1.45 ms to 2.70 ms), because it filters all pivots at every
  bar. Both are dominated by level clustering
  (a mean recomputed per pivot over up to 200 pivots each bar), kept bit-identical
  to Phase 4. A faster exact clustering is a candidate optimization.
- **Thresholds are still untuned defaults.** Real-history evaluation should come
  before any change.
- **The runner is stateless per run:** incremental state is not persisted across
  runs; each run re-warms from history.

## Recommended Phase 4C

1. **Credentialed live validation** of the Polygon/Massive adapter for META/NVDA:
   field names, volume type, delay, pagination and rate limits. Then register
   `technical.runner` in the scheduler, disabled by default
   (`TECHNICAL_SCHEDULE_ENABLED=false`).
2. **A `technical_snapshots` migration** for shadow persistence, following the
   Phase 2 shadow-writer pattern and off by default.
3. **Real-history evaluation reports** for META/NVDA (5m/1h/1d) with the Phase 4B
   harness, reviewed before any threshold change.
4. **A faster exact level-clustering algorithm** (bit-identical output), then a
   re-measurement.

Options analytics and news/technical fusion remain separate later phases.
