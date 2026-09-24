# Phase 4: Market Technical Signal Engine

Phase 4 adds a deterministic technical-analysis engine to MIAS (Market
Intelligence Alert System). It describes price structure and indicators for
equities such as META and NVDA. It is **decision support only**:

- it never outputs BUY or SELL;
- it never places, sizes or suggests trades;
- it never talks to a broker;
- it never sends anything to Telegram.

Technical intelligence is independent of the event collectors (News, SEC
(U.S. Securities and Exchange Commission), Fed (Federal Reserve), Macro, Treasury,
Geopolitical). The two are not mixed or scored together in Phase 4.

Code:

| Package / file | Contents |
|---|---|
| `market_data/models.py` | `MarketBar`, `Interval`, `Session`, exchange-time helpers |
| `market_data/validation.py` | series validation (ordering, duplicates, symbol/interval mix) |
| `market_data/provider.py` | `MarketDataProvider` contract and `FixtureProvider` |
| `technical/indicators.py` | SMA, EMA, RSI, TR, ATR, VWAP, relative volume |
| `technical/structure.py` | pivots, confirmation, HH/HL/LH/LL labels, trend state |
| `technical/levels.py` | support/resistance clustering, breakout/breakdown |
| `technical/models.py` | `TechnicalConfig`, `TechnicalSnapshot`, `TechnicalSignal` |
| `technical/signals.py` | evidence, state classification, confidence |
| `technical/engine.py` | `TechnicalEngine.replay` / `analyze`, gaps, EMA/VWAP/momentum states |
| `technical/formatter.py` | human-readable terminal text |
| `tests/technical_fixtures.py` | synthetic META/NVDA scenario corpus |

## 1. Purpose

The engine answers "what is price doing?" in a reproducible, explainable way.
Every snapshot lists its evidence, and every state carries its reasons. The same
bars always produce the same output. There is no AI (artificial intelligence),
ML (machine learning) or opaque scoring. Options, Greeks and implied volatility
are out of scope.

## 2. Market data model

A `MarketBar` is one OHLCV (Open, High, Low, Close, Volume) bar:

| Field | Rule |
|---|---|
| `symbol` | upper-case ticker (`[A-Z][A-Z0-9.-]{0,9}`) |
| `timestamp` | bar **start**, timezone-aware (naive datetimes are rejected) |
| `interval` | an `Interval` (see section 3) |
| `open`, `high`, `low`, `close` | `Decimal`, positive; `float` input is rejected so no binary rounding enters the data |
| `volume` | integer >= 0 |
| `session` | `regular`, `pre` or `post`, derived from exchange time for intraday bars; `None` for daily bars |

Validation also requires `high >= max(open, close, low)` and
`low <= min(open, close, high)`.

`validate_series` requires one symbol and one interval, and strictly increasing
timestamps. Duplicate instants are rejected, even when the same instant is
written in another timezone. Out-of-order bars are rejected. Missing bars (halts,
weekends, holidays) are allowed and **never filled**.

**Decimal vs float.** Bars keep exact `Decimal` prices. Indicator math converts
to `float`, which is IEEE-754 (Institute of Electrical and Electronics Engineers
floating-point standard) double precision with about 15-16 significant digits.
For prices from tens to thousands of dollars, the error is about 1e-12, ten
orders of magnitude below a $0.01 tick. Indicator outputs are descriptive and
never order prices.

**Sessions** use America/New_York exchange time:

- regular: 09:30-16:00;
- `pre`: before 09:30;
- `post`: 16:00 onwards.

There is no holiday calendar; a missing day simply has no bars.

**Provider.** `MarketDataProvider.get_bars(symbol, interval, start, end)`
returns validated bars with `start <= timestamp < end`, where both bounds are
timezone-aware. `FixtureProvider` serves in-memory series and rejects duplicate
series.

**No live provider ships in Phase 4.** The repository has no market-price source
or dependency. The free Yahoo chart endpoints are unofficial, with unclear
licensing and reliability. Per the Phase 4 rules, Phase 4 stops at the provider
abstraction plus fixtures. A live adapter is a separate, licensing-reviewed
decision (see section 20).

## 3. Supported intervals

`1m`, `5m`, `15m`, `30m`, `1h` and `1d` (`Interval.M1` … `Interval.D1`), handled
generically by length in seconds. Intervals under a day are intraday. No symbol
has special handling. `Interval.parse` accepts the enum, the label (`"5m"`) or
the name (`"M5"`).

## 4. EMA (Exponential Moving Average)

- Periods are configurable; the defaults are 9, 20, 50 and 200.
- The first value, at index N-1, is the SMA (Simple Moving Average) of the first
  N closes (the StockCharts convention).
- After that: `ema = (close - previous_ema) * k + previous_ema`, with
  `k = 2 / (N + 1)`.
- Values before index N-1 are `None`; warm-up values are never fabricated. For
  example, 78 five-minute bars never produce an EMA200.

**Verification:** the 10-period EMA reproduces the published StockCharts
"ChartSchool" worked example to within one rounding step. The published table
rounds intermediate values to cents, so one row shows 23.54 where the exact
value rounds to 23.53.

**EMA state** (descriptive, never buy/sell):

- alignment: `bullish_alignment` (EMA9 > EMA20 > EMA50), `bearish_alignment`
  (EMA9 < EMA20 < EMA50) or `mixed`;
- price above or below EMA20. The snapshot field is `price_vs_ema20`; it always
  compares with the configured medium EMA;
- EMA9/20 and EMA20/50 `bullish_cross` or `bearish_cross` on the bar where the
  sign of the difference flips.

## 5. VWAP (Volume Weighted Average Price)

```
typical_price = (high + low + close) / 3
VWAP = sum(typical_price x volume) / sum(volume)   (cumulative within the session)
```

- VWAP **resets** at each regular session, keyed by exchange-time date.
- Only regular-session bars count; pre- and post-market bars get `None`.
- A zero-volume bar adds nothing. A session with no volume yet has `None`
  (no division by zero).
- Missing bars change nothing: the sums are over the bars that exist.
- Daily bars get `None`, because VWAP is an intraday measure.

**VWAP state:**

- `above_vwap` / `below_vwap`;
- `crossing_above_vwap` / `crossing_below_vwap`, when the previous close in the
  **same session** was on the other side;
- distance in dollars and in percent.

## 6. RSI (Relative Strength Index)

This is Wilder's RSI, period 14 by default and configurable:

- the first average gain and loss are simple means of the first N close-to-close
  changes (defined at index N);
- after that, `avg = (previous_avg x (N - 1) + current) / N`;
- `RSI = 100 - 100 / (1 + avg_gain / avg_loss)`.

Edge cases never divide by zero:

| Case | RSI |
|---|---|
| gains only | 100 |
| losses only | 0 |
| flat | 50 |
| fewer than N + 1 closes | `None` |

**Verification:** it matches the published StockCharts 14-period example exactly
to two decimals (the first value is 70.53).

**Momentum state** (descriptive; configurable thresholds):

| RSI | State |
|---|---|
| >= 70 | `overbought_like` |
| >= 55 | `strong` |
| <= 30 | `oversold_like` |
| <= 45 | `weak` |
| otherwise | `neutral` |

RSI above 70 is **not** a sell signal, and below 30 is **not** a buy signal.

## 7. ATR (Average True Range)

- TR (True Range) = `max(high - low, |high - previous_close|, |low - previous_close|)`.
  It includes overnight gaps. The first bar has no previous close, so its TR is
  `high - low`.
- ATR uses Wilder smoothing, period 14 by default: the first ATR is the mean of
  the first N TRs (index N-1); after that,
  `atr = (previous x (N - 1) + tr) / N`.
- ATR measures volatility, not direction. It scales level-clustering tolerance
  and the breakout buffer.
- It is tested against a from-scratch brute-force computation, including
  gap-up and gap-down bars.

## 8. Volume

- `volume`: the current bar's volume.
- `average_volume`: the mean volume of the **previous** `volume_lookback` bars
  (default 20). The current bar is excluded, so a spike does not dilute its own
  baseline.
- `relative_volume = volume / average_volume`. It is `None` until the lookback
  exists, or when the average is 0.
- At `elevated_relative_volume` (default 1.5x) or above, a reason is added and
  directional confidence gets one extra agreeing item (section 15).

## 9. Gaps

For intraday bars, a session's first regular open is compared with the previous
session's last regular close:

```
gap_absolute = open - previous_close
gap_percent  = gap_absolute / previous_close x 100
```

The label is `gap_up` if `gap_percent >= gap_threshold_pct` (default 0.5), and
`gap_down` if it is `<= -gap_threshold_pct`. Anything smaller is `no_gap`, so
tiny noise is not labelled significant. Every regular bar of the session carries
that session's gap.

It is `None` when:

- no previous session exists in the data;
- the bar is extended hours;
- the bar is the last bar of the previous day, before the next open exists.

For daily bars, the gap is `open` vs the previous bar's `close`.

## 10. Swing detection

This uses a symmetric N-bar window (`pivot_window`, default 2). Bar `i` is:

- a **swing high** if its high is strictly greater than each of the N highs to
  the left, **and** greater than or equal to each of the N highs to the right;
- a **swing low** is the mirror (strictly lower than the left, lower than or equal
  to the right).

Other rules:

- **Ties:** in a flat top or bottom, only the **leftmost** bar of the plateau
  qualifies. It beats the bars to its right, and later bars fail the strict left
  comparison.
- An outside bar can be both a swing high and a swing low.
- The first N and last N bars can never be pivots.
- Only N-bar extremes count, not every price change. A larger window means fewer,
  more significant pivots.

**Confirmation:** a pivot at bar `i` is confirmed at bar `i + N`, when its right
side exists. Before then, it is invisible to the engine.

## 11. HH/HL/LH/LL (Higher High, Higher Low, Lower High, Lower Low)

In confirmation order:

- each confirmed swing high is compared **only with the previous confirmed swing
  high**: HH, LH, or EH (Equal High, within `equal_tolerance_pct`, default 0 =
  exact);
- each swing low is compared **only with the previous swing low**: HL, LL, or EL
  (Equal Low);
- a high is never compared with a low;
- the first high and first low are unlabelled.

Trend from the latest high label and low label:

| Last high | Last low | Trend |
|---|---|---|
| HH | HL | `bullish` |
| LH | LL | `bearish` |
| LH | HL | `range` (contracting) |
| EH | any | `range` |
| any | EL | `range` |
| HH | LL | `mixed` (expanding, conflicting) |
| fewer than 2 highs or 2 lows | | `insufficient_data` |

The structure evidence is `trend`, `last_high_type`, `last_low_type`,
`last_significant_high`, `last_significant_low` (price, time, bar index,
confirmation index) and the counts of confirmed highs and lows.

## 12. Support/resistance

Levels are built from **confirmed** pivots only (highs and lows together,
because a level can change roles). At each bar:

1. Sort the pivot prices and cluster them greedily. A pivot joins the current
   cluster while it is within `max(cluster_pct% x price, cluster_atr x ATR)` of
   the cluster mean. The defaults are 0.35% and 0.5 x ATR.
2. Keep clusters with at least `min_touches` pivots (default 2).
3. Each level has:
   - `price` (the mean);
   - `type` (`resistance` above the close, `support` below);
   - `touches`, `first_seen`, `last_seen`;
   - strength evidence (the touch count, the high/low mix and the time span).

The snapshot lists supports and resistances nearest first. The output does not
depend on input order.

## 13. Breakout detection

The buffer is `max(buffer_pct% x close, buffer_atr x ATR)`, 0.1% and
0.25 x ATR by default. A one-cent crossing never counts, and only **closes**
count, not wicks. A bar is judged against the levels known at the **previous**
bar, so the breakout bar never helps form the level it breaks.

States, in evaluation order:

1. `failed_breakout`: a breakout of level L happened within the last
   `failure_lookback` bars (default 3), and the close is now back below L.
   `failed_breakdown` is the mirror. These are checked first, so falling straight
   back through a just-broken level is reported as a failure, not as a new
   breakdown.
2. `breakout`, when all three hold:
   - `close > L + buffer`;
   - the previous close was `<= L + buffer` (so this bar is the break);
   - at least one close in the last `failure_lookback` bars was `<= L`, so L
     really was resistance. A bounce off support within the buffer is not a
     breakout.
3. `breakdown`: the mirror of breakout.
4. `none`.

## 14. Signal classification

Evidence items, each with a direction:

| Evidence | Bullish (+1) | Bearish (-1) |
|---|---|---|
| structure | trend `bullish` | trend `bearish` |
| EMA alignment | EMA9 > EMA20 > EMA50 | EMA9 < EMA20 < EMA50 |
| price vs EMA20 | above | below |
| VWAP (intraday) | above / crossing above | below / crossing below |
| momentum | `strong` / `overbought_like` | `weak` / `oversold_like` |
| gap | `gap_up` | `gap_down` |

States (first match wins; **never** BUY or SELL):

1. `insufficient_data`: EMA20, RSI or ATR is still warming up (the reason names
   which).
2. `breakout_watch` / `breakdown_watch`: this bar broke a level beyond the buffer.
3. `bullish_setup`: all three of:
   - bullish structure;
   - bullish EMA alignment;
   - price above VWAP (above EMA20 when VWAP is undefined, e.g. daily bars).
4. `bearish_setup`: the mirror.
5. `range`: range structure. A confirmed range outranks short-term momentum inside
   it; it is left through a breakout/breakdown or a new confirmed structure.
6. `bullish_momentum`: all of:
   - structure not bearish;
   - bullish EMA alignment;
   - price above EMA20;
   - RSI `strong` or higher;
   - not below VWAP.
7. `bearish_momentum`: the mirror.
8. `mixed`: everything else. A failed breakout or breakdown lands here or in
   `range`, and is named first in the reasons.

Every state carries reasons, for example:

- `confirmed HH/HL structure`
- `EMA9 > EMA20 > EMA50`
- `price above EMA20`
- `above vwap (+0.80%)`
- `RSI 61.4 (strong)`
- `relative volume 1.7x (elevated)`

## 15. Confidence

LOW / MEDIUM / HIGH is deterministic and explainable. Only directional states
(setups, momentum, breakout/breakdown watch) can rise above LOW. `range`,
`mixed` and `insufficient_data` are always LOW. The engine counts the evidence
items (section 14):

- A (agreeing) = items in the state's direction, plus 1 when relative volume is
  elevated;
- C (conflicting) = items against it.

| Confidence | Rule |
|---|---|
| HIGH | A >= 5 and C = 0 |
| MEDIUM | A >= 3 and C <= 1 |
| LOW | otherwise |

Every item has weight 1. There are no hidden weights. The snapshot's signal
exposes `agreeing` and `conflicting`. All thresholds live in `TechnicalConfig`
and are validated. For example, the RSI zones must satisfy
`oversold < weak <= strong < overbought`.

## 16. Replay

`TechnicalEngine.replay(bars)` validates the series and emits one
`TechnicalSnapshot` per bar, in order. For each bar it:

1. exposes only data up to that bar;
2. confirms pivots whose right side now exists;
3. rebuilds levels from the confirmed pivots;
4. judges breakouts against the previous bar's levels;
5. classifies the bar.

`analyze(bars)` is the last replay snapshot. Snapshots are frozen dataclasses
with `to_dict()` (JSON (JavaScript Object Notation) serializable).

Indicators are computed once per series, which is safe because they are causal.
Levels are rebuilt at each bar in O(pivots) time, so a replay is
O(bars x pivots). That is fine for sessions and fixtures; see section 20.

## 17. Look-ahead prevention

Look-ahead bias is treated as a correctness failure. `tests/test_technical_replay.py`
proves:

- **Equivalence:** `replay(bars)[i] == analyze(bars[:i + 1])` for every bar of
  every META scenario, and every fifth bar of every NVDA scenario. A truncated
  series cannot see the future, so equality proves the full replay did not
  either.
- **Future mutation:** replacing all bars from a cut point with a wildly
  different path leaves every earlier snapshot identical.
- **Pivots:**
  - every pivot's confirmation index is `index + window`;
  - with one bar fewer than required, the pivot is not even detectable;
  - the confirmed-pivot count at each bar equals the number of pivots confirmed
    by then;
  - the significant high/low in each snapshot is confirmed at or before its bar.
- **Levels:** no level's `last_seen` is later than `pivot_window` bars before the
  snapshot.
- **Breakouts:** on every truncated prefix before the breakout bar, no breakout
  appears. The broken level already existed at the previous bar.
- **Indicators:**
  - EMA20, RSI, ATR and VWAP in each snapshot equal the values computed from
    the prefix alone;
  - the indicator tests separately check that appending data never changes
    earlier outputs.
- **Gaps:** the last bar of day 1 does not know day 2's gap.

## 18. Limitations

- **No live data:** there is only the fixture provider (section 2). All examples
  are synthetic.
- **No exchange calendar:** there are no holidays or half-days; sessions are
  09:30-16:00 America/New_York by weekday time rules.
- **Bar timestamps are bar starts.** A vendor using bar-end stamps must be
  normalized in its adapter.
- **N-bar pivots lag** by `pivot_window` bars by construction; that lag is the
  price of no look-ahead.
- **Greedy clustering** is simple and deterministic, not optimal. Level strength
  is evidence, not a probability.
- **Replay cost** is O(bars x pivots). Long multi-year intraday replays need an
  incremental level builder.
- **Thresholds are defaults**, not tuned or backtested, because there is no
  historical data in Phase 4.
- **No fusion** with news or event intelligence.
- **No scheduler job:** there is nothing live to schedule, so the optional
  `TECHNICAL_SCHEDULE_ENABLED` hook was deliberately **deferred**. The scheduler
  is unchanged.
- **The formatter is terminal/testing only** and is never sent to Telegram.

## 19. META/NVDA examples

`tests/technical_fixtures.py` builds **synthetic** five-minute regular sessions:

- 78 bars per session;
- piecewise-linear percent paths from arbitrary bases (META 740, NVDA 182);
- 0.05% wicks;
- U-shaped volume, with volume multipliers on breakout bars.

The dates (21 and 22 September 2026) and prices are generated. **These are not
historical prices.** Each scenario runs for both symbols with identical states.

Final-bar results (the same for META and NVDA; A/C = agreeing/conflicting):

| Scenario | Structure | Final state | Confidence | A/C | Key intermediate check |
|---|---|---|---|---|---|
| bullish_trend | HH/HL bullish | bullish_setup | HIGH | 5/0 | bars 0-18 insufficient_data |
| bearish_trend | LH/LL bearish | bearish_setup | HIGH | 5/0 | |
| range | EH/EL range | range | LOW | 0/0 | no breakout/breakdown in the range |
| gap_up_continuation | HH/HL bullish | bullish_setup | HIGH | 6/0 | +2.00% gap on every day-2 bar |
| gap_up_failure | LH/LL bearish | bearish_setup | MEDIUM | 5/1 | the gap conflicts with the fade |
| vwap_reclaim | HH/HL bullish | bullish_setup | HIGH | 5/0 | below VWAP, one crossing_above, then above |
| vwap_rejection | LH/LL bearish | bearish_setup | HIGH | 5/0 | crossing_above, then crossing_below, then below |
| ema_crossover | HH/HL bullish | bullish_setup | HIGH | 5/0 | bearish alignment at day-1 close; EMA9/20 bullish cross on day 2 |
| breakout_high_volume | HH/HL bullish | bullish_setup | HIGH | 5/0 | bar 57 breakout_watch HIGH on 3x+ relative volume |
| false_breakout | LH/HL range | range | LOW | 0/0 | bar 57 breakout_watch; bars 59-60 failed_breakout; expired at 61 |

Final prices:

| Scenario | META | NVDA |
|---|---|---|
| bullish_trend | 768.86 | 189.10 |
| bearish_trend | 711.14 | 174.90 |
| gap_up_continuation | 796.83 | 195.98 |
| breakout_high_volume | 759.98 | 186.91 |

Formatter output for the synthetic NVDA breakout bar:

```
NVDA — 5m
Price: 185.28

Structure:
EH / EL
Trend: Range

EMA:
9 > 20 > 50

VWAP:
Price +1.2% above VWAP

RSI(14):
71.5

Volume:
4.4x average

Levels:
Support 183.91 (5 touches)
Breakout of 183.91

Signal:
BREAKOUT_WATCH (confidence HIGH)

Reasons:
- breakout of level 183.91
- EMA9 > EMA20 > EMA50
- price above EMA20
- above vwap (+1.18%)
- RSI 71.5 (overbought-like)
- relative volume 4.4x (elevated)
```

Usage:

```python
from technical.engine import TechnicalEngine
from technical.formatter import format_snapshot

snapshot = TechnicalEngine().analyze(bars)   # bars: validated MarketBar list, oldest first
print(format_snapshot(snapshot))
```

## 20. Recommended Phase 4B

1. **Licensed live market data:** pick a provider with explicit terms (for
   example, a keyed free tier), and implement `MarketDataProvider` behind
   environment variables with no secrets in logs. The adapter must:
   - normalize to bar-start timestamps;
   - keep `Decimal` prices;
   - handle provider gaps without filling them.
2. **Exchange calendar:** holidays and half-days for sessions, gaps and VWAP.
3. **Multi-timeframe context:** daily or 1h structure alongside 5m, reported side
   by side, not fused.
4. **Incremental engine:** keep indicator and pivot state per symbol, so a new
   bar costs O(1) to O(levels) instead of a full replay.
5. **Disabled-by-default scheduler job** (`TECHNICAL_SCHEDULE_ENABLED=false`):
   fetch, analyze and log snapshots only. No Telegram until reviewed.
6. **Snapshot persistence:** shadow-mode storage following the Phase 2 pattern,
   for later audit and backtest.
7. **Evaluation harness** on real history: measure the stability of states and
   the frequency of failed breakouts before tuning any threshold.

Fusion with news/event intelligence and options analytics remain later phases.
