"""Pure Market Context engine (Phase 7B.1): completed 5m bars in, deterministic facts out.

It never calls a provider or reads a clock. ``now``, ``as_of`` and the
``ExchangeCalendar`` are always explicit.

**Input rules (``build_series_context``):**

- the interval must be 5m;
- bars must form a valid series for the declared symbol (``validate_series``);
- every bar must be **completed** at ``as_of`` (``completion.split_completed``);
  a forming bar raises ``MarketDataError``;
- ``now`` and ``as_of`` must be timezone-aware, with ``as_of <= now``.

**Regular session only:** a bar counts only if ``calendar.classify`` says
``regular``. Pre-market and after-hours bars are ignored for every metric.

**Context session:** the latest regular session containing at least one completed
regular bar.

- Session open, high, low, latest close and volume come from the existing
  ``market_data.aggregation.aggregate(bars, "1d", calendar)``, applied to that
  session's completed regular bars.
- ``session_range = high - low``.
- ``position_in_range = (latest_close - low) / (high - low)``; ``None`` with
  ``zero_range`` when ``high == low``.
- ``distance_from_high = (high - latest_close) / latest_close`` and
  ``distance_from_low = (latest_close - low) / latest_close``. Both are ``>= 0``
  and are fractions of the latest close.

**Previous close (existing MIAS semantic, as in ``technical.engine._gaps``):**

- the close of the last completed regular bar of
  ``calendar.previous_trading_day(session_date)``, so weekends and holidays are
  skipped by the calendar;
- if that session is absent from the bars: ``None`` with ``no_previous_session``;
- there is no vendor "official close".

**Returns** are simple returns in exact ``Decimal``:

- a 28-digit local context, quantized to 10 decimal places with ``ROUND_HALF_EVEN``;
- fractional units: ``0.0125`` = +1.25%;
- ``return_since_prev_close = latest_close / previous_close - 1``;
- ``return_since_open = latest_close / session_open - 1``.

**VWAP:** ``technical.indicators.vwap`` (unchanged) on the full input series, at the
latest regular bar. The formula is Σ(((H+L+C)/3) × volume) / Σvolume, reset at
each regular session. It is float, and it is **not** the vendor aggregate ``vw``.
``close_vs_vwap = (latest_close - vwap) / vwap``, also float.

**Comparisons (``compare``), per benchmark and basis:**

- ``cutoff = min(latest regular bar end of symbol, of benchmark)``;
- each series is evaluated at its last bar with ``bar_end <= cutoff``;
- ``relative_return = symbol_return - benchmark_return``, both quantized;
- if either evaluated bar ends before the cutoff (a missing bar): ``aligned=False``,
  ``misaligned``, and ``relative_return=None``;
- different context sessions give ``session_mismatch``.
"""
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from market_context import models as m
from market_context.freshness import freshness, grid_ends
from market_data.aggregation import aggregate
from market_data.completion import split_completed
from market_data.models import EXCHANGE_TZ, MarketDataError, Session
from market_data.validation import validate_series
from technical.indicators import vwap as mias_vwap

QUANTUM = Decimal("1E-10")


def _ratio_minus_one(numerator, denominator):
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        return (numerator / denominator - 1).quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def _fraction(numerator, denominator):
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        return (numerator / denominator).quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def _check_times(now, as_of):
    if now.utcoffset() is None or as_of.utcoffset() is None:
        raise MarketDataError("now and as_of must be timezone-aware")
    if as_of > now:
        raise MarketDataError("as_of must not be after now")


def _prepared(series, now, as_of, calendar):
    """(all bars, {session_date: [regular bars]}) after contract validation."""
    if series.interval != m.CONTEXT_INTERVAL:
        raise MarketDataError(f"market context interval must be {m.CONTEXT_INTERVAL}")
    _check_times(now, as_of)
    bars = validate_series(series.bars)
    if bars and bars[0].symbol != series.symbol:
        raise MarketDataError("bars do not match the series symbol")
    if bars and bars[0].interval.label != m.CONTEXT_INTERVAL:
        raise MarketDataError("bars do not match the context interval")
    _, forming = split_completed(bars, as_of, calendar)
    if forming:
        raise MarketDataError(f"bar {forming[0].timestamp.isoformat()} is not completed at as_of")
    sessions = {}
    for bar in bars:
        if calendar.classify(bar.timestamp) is Session.REGULAR:
            sessions.setdefault(bar.timestamp.astimezone(EXCHANGE_TZ).date(), []).append(bar)
    return bars, sessions


def _expected_so_far(day, as_of, calendar):
    return sum(1 for end in grid_ends(day, m.CONTEXT_INTERVAL, calendar) if end <= as_of)


def build_series_context(series, *, now, as_of, calendar):
    bars, sessions = _prepared(series, now, as_of, calendar)
    if not sessions:
        return m.SeriesContext(symbol=series.symbol, interval=series.interval, unavailable=(m.NO_DATA,),
                               freshness=freshness(latest_bar_end=None, now=now, as_of=as_of,
                                                   delay_seconds=series.delay_seconds, interval=series.interval,
                                                   calendar=calendar))
    day = max(sessions)
    session_bars = sessions[day]
    daily = aggregate(session_bars, "1d", calendar)[0]
    latest = session_bars[-1]
    latest_end = calendar.bar_end(latest)
    reasons = []
    previous_day = calendar.previous_trading_day(day)
    previous_close = sessions[previous_day][-1].close if previous_day in sessions else None
    if previous_close is None:
        reasons.append(m.NO_PREVIOUS_SESSION)
    session_range = daily.high - daily.low
    position = None
    if session_range > 0:
        position = _fraction(daily.close - daily.low, session_range)
    else:
        reasons.append(m.ZERO_RANGE)
    vwap_values = mias_vwap(list(bars))
    vwap_value = vwap_values[bars.index(latest)]
    close_vs_vwap = None if vwap_value is None else (float(daily.close) - vwap_value) / vwap_value
    if vwap_value is None:
        reasons.append(m.NO_VWAP)
    return m.SeriesContext(
        symbol=series.symbol, interval=series.interval, session_date=day, bars_completed=len(session_bars),
        bars_expected_so_far=_expected_so_far(day, as_of, calendar), session_open=daily.open,
        session_high=daily.high, session_low=daily.low, latest_close=daily.close, latest_bar_start=latest.timestamp,
        latest_bar_end=latest_end, previous_close=previous_close,
        previous_close_session=previous_day if previous_close is not None else None,
        return_since_prev_close=None if previous_close is None else _ratio_minus_one(daily.close, previous_close),
        return_since_open=_ratio_minus_one(daily.close, daily.open), session_range=session_range,
        position_in_range=position, distance_from_high=_fraction(daily.high - daily.close, daily.close),
        distance_from_low=_fraction(daily.close - daily.low, daily.close), session_volume=daily.volume,
        vwap=vwap_value, close_vs_vwap=close_vs_vwap,
        freshness=freshness(latest_bar_end=latest_end, now=now, as_of=as_of, delay_seconds=series.delay_seconds,
                            interval=series.interval, calendar=calendar),
        unavailable=tuple(reasons))


def _evaluated(series, now, as_of, calendar, cutoff):
    """(bar_end, return_since_prev_close, return_since_open) at the last session bar ending <= cutoff."""
    _, sessions = _prepared(series, now, as_of, calendar)
    day = max(sessions)
    candidates = [b for b in sessions[day] if calendar.bar_end(b) <= cutoff]
    bar = candidates[-1]
    previous_day = calendar.previous_trading_day(day)
    previous_close = sessions[previous_day][-1].close if previous_day in sessions else None
    return (calendar.bar_end(bar),
            None if previous_close is None else _ratio_minus_one(bar.close, previous_close),
            _ratio_minus_one(bar.close, sessions[day][0].open))


def compare(symbol_series, symbol_context, benchmark_series, benchmark_context, *, now, as_of, calendar):
    """BenchmarkComparison objects for both bases (``prev_close`` then ``open``)."""
    name = benchmark_series.symbol
    if name == symbol_series.symbol:
        return tuple(m.BenchmarkComparison(benchmark=name, basis=basis, unavailable=(m.SELF,)) for basis in m.BASES)
    missing = ((m.NO_SYMBOL_DATA,) if symbol_context.session_date is None else ()) + \
        ((m.NO_BENCHMARK_DATA,) if benchmark_context.session_date is None else ())
    if missing:
        return tuple(m.BenchmarkComparison(benchmark=name, basis=basis, unavailable=missing) for basis in m.BASES)
    if symbol_context.session_date != benchmark_context.session_date:
        return tuple(m.BenchmarkComparison(benchmark=name, basis=basis, unavailable=(m.SESSION_MISMATCH,))
                     for basis in m.BASES)
    cutoff = min(symbol_context.latest_bar_end, benchmark_context.latest_bar_end)
    s_end, s_prev, s_open = _evaluated(symbol_series, now, as_of, calendar, cutoff)
    b_end, b_prev, b_open = _evaluated(benchmark_series, now, as_of, calendar, cutoff)
    aligned = s_end == cutoff and b_end == cutoff
    gap = int((cutoff - min(s_end, b_end)).total_seconds())
    out = []
    for basis, s_ret, b_ret in (("prev_close", s_prev, b_prev), ("open", s_open, b_open)):
        reasons = []
        if not aligned:
            reasons.append(m.MISALIGNED)
        if s_ret is None or b_ret is None:
            reasons.append(m.NO_PREVIOUS_SESSION)
        relative = s_ret - b_ret if not reasons else None
        out.append(m.BenchmarkComparison(benchmark=name, basis=basis, cutoff=cutoff, symbol_return=s_ret,
                                         benchmark_return=b_ret, relative_return=relative, aligned=aligned,
                                         symbol_bar_end=s_end, benchmark_bar_end=b_end, alignment_gap_seconds=gap,
                                         unavailable=tuple(reasons)))
    return tuple(out)


def build_market_context(symbol_series, benchmark_series=(), *, now, as_of, calendar, provenance=None):
    """One MarketContext: the symbol's facts, each benchmark's facts, and comparisons (facts only)."""
    _check_times(now, as_of)
    symbol_context = build_series_context(symbol_series, now=now, as_of=as_of, calendar=calendar)
    benchmarks, contexts, comparisons, seen = [], [], [], set()
    for series in benchmark_series:
        if series.symbol in seen:
            continue
        seen.add(series.symbol)
        benchmarks.append(series)
    for series in benchmarks:
        context = symbol_context if series.symbol == symbol_series.symbol else \
            build_series_context(series, now=now, as_of=as_of, calendar=calendar)
        if series.symbol != symbol_series.symbol:
            contexts.append(context)
        comparisons.extend(compare(symbol_series, symbol_context, series, context, now=now, as_of=as_of,
                                   calendar=calendar))
    inputs = {s.symbol: dict(provider=s.provider_id, delay_seconds=int(s.delay_seconds), bars_supplied=len(s.bars))
              for s in (symbol_series, *benchmarks)}
    return m.MarketContext(
        context_format_version=m.CONTEXT_FORMAT_VERSION, symbol=symbol_series.symbol, now=now, as_of=as_of,
        calendar_state=calendar.classify(now).value, session_date=symbol_context.session_date,
        symbol_context=symbol_context, benchmark_contexts=tuple(contexts), comparisons=tuple(comparisons),
        provenance=dict(interval=m.CONTEXT_INTERVAL, inputs=inputs, **(provenance or {})))
