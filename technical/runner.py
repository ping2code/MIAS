"""One-shot technical job: fetch completed bars, warm the incremental engine, log each timeframe's state.

    python -m technical.runner [--symbols META,NVDA] [--timeframes 1d,1h,5m] [--text]

- Settings come from the process environment (``market_data.config``,
  ``persistence.technical_settings``); ``.env`` is never read.
- Each run is stateless: it warms the engine from recent history and reports the
  latest **completed** bar per timeframe. It is not a daemon.
- Each source interval is fetched once per symbol. 1h and 1d are derived from 30m,
  so a symbol costs two fetches (plus any vendor pagination) for the default
  timeframes.
- Output is structured ``key=value`` log lines (and an optional text view). There is
  no Telegram and no alert decision, and timeframes are never combined.

**Warm-up windows (Phase 4C)** are anchored to trading sessions, not wall-clock
time. For a target timeframe the window starts at midnight exchange time,
``WARMUP_SESSIONS[target] - 1`` trading days before the *reference day*: the latest
trading day whose first bar of that timeframe has completed. So every run that
evaluates the same latest bar uses the same bars and produces the same snapshot,
and persisted snapshots stay idempotent across runs. The window only moves when a
new bar of that timeframe completes on a new trading day.

| Target | Sessions | Bars (regular) | Why |
|---|---|---|---|
| 5m | 10 | ~780 | about 3.9x EMA200, far beyond RSI/ATR(14), volume(20) and pivot/level history |
| 1h | 90 | ~630 | about 3x EMA200 |
| 1d | 450 | ~450 | 2.25x EMA200. About 21 months, inside a typical two-year history limit. The EMA200 seed still carries about 8% weight (see docs) |

**Shadow persistence (Phase 4C):** with
``TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED=true``, each timeframe's latest
snapshot is submitted after it has been computed and logged. A database failure
changes neither output nor exit code.

Exit codes: 0 success, 1 provider or data failure for any symbol, 2 configuration or usage error.
"""
import argparse
from datetime import datetime, timedelta
import logging
import os
import sys

from market_data.aggregation import derive_completed
from market_data.models import EXCHANGE_TZ, Interval, MarketDataError
from technical.formatter import format_multi_timeframe
from technical.incremental import IncrementalTechnicalEngine
from technical.multitimeframe import DEFAULT_TIMEFRAMES, from_engine

logger = logging.getLogger("technical.runner")
DEFAULT_SYMBOLS = ("META", "NVDA")
SOURCE = {Interval.M1: Interval.M1, Interval.M5: Interval.M5, Interval.M15: Interval.M15, Interval.M30: Interval.M30,
          Interval.H1: Interval.M30, Interval.D1: Interval.M30}
WARMUP_SESSIONS = {Interval.M1: 3, Interval.M5: 10, Interval.M15: 30, Interval.M30: 60, Interval.H1: 90,
                   Interval.D1: 450}


def reference_day(calendar, as_of, target):
    """Latest trading day whose first ``target`` bar has completed by ``as_of``."""
    local = as_of.astimezone(EXCHANGE_TZ)
    day = local.date()
    times = calendar.session_times(day)
    if times is not None:
        first_end = times.close if not target.intraday else min(times.open + target.delta, times.close)
        if as_of >= first_end:
            return day
    return calendar.previous_trading_day(day)


def warmup_start(calendar, as_of, target):
    day = reference_day(calendar, as_of, target)
    for _ in range(WARMUP_SESSIONS[target] - 1):
        day = calendar.previous_trading_day(day)
    return datetime.combine(day, datetime.min.time(), tzinfo=EXCHANGE_TZ)


def run_symbol(provider, symbol, timeframes):
    """(MultiTimeframeSnapshot, {label: dict(bars, warmup_start)}) for one symbol."""
    calendar = provider.calendar
    as_of = provider.as_of()
    engine = IncrementalTechnicalEngine(calendar=calendar)
    starts = {interval: warmup_start(calendar, as_of, interval) for interval in timeframes}
    fetched, context = {}, {}
    for source in sorted({SOURCE[t] for t in timeframes}, key=lambda i: i.seconds):
        start = min(starts[t] for t in timeframes if SOURCE[t] == source)
        fetched[source] = provider.get_bars(symbol, source, start, as_of + timedelta(seconds=1))
    for interval in timeframes:
        source_bars = [b for b in fetched[SOURCE[interval]] if b.timestamp >= starts[interval]]
        bars = source_bars if SOURCE[interval] == interval else derive_completed(source_bars, interval, calendar, as_of)
        context[interval.label] = dict(bars=len(bars), warmup_start=starts[interval])
        engine.warmup(bars)
    return from_engine(engine, symbol, [t.label for t in timeframes]), context


def _log_snapshot(symbol, label, snapshot, context):
    if snapshot is None:
        logger.info("event=technical_snapshot symbol=%s interval=%s bars=%d state=unavailable", symbol, label,
                    context["bars"])
        return
    logger.info("event=technical_snapshot symbol=%s interval=%s bars=%d warmup_start=%s last_bar=%s state=%s "
                "confidence=%s trend=%s high_type=%s low_type=%s rsi=%s vwap=%s", symbol, label, context["bars"],
                context["warmup_start"].date().isoformat(), snapshot.timestamp.isoformat(), snapshot.signal.state,
                snapshot.signal.confidence, snapshot.trend, snapshot.last_high_type, snapshot.last_low_type,
                "n/a" if snapshot.rsi is None else f"{snapshot.rsi:.1f}", snapshot.vwap_state["position"])


def _submit_snapshots(multi, context, provider, persistence):
    """Build immutable rows and submit them (non-blocking); returns the number of rows that could not be built."""
    from persistence import technical_shadow
    from persistence.technical_snapshot_repository import SnapshotRowError, snapshot_row
    invalid = 0
    for label, snapshot in multi.timeframes.items():
        if snapshot is None:
            continue
        intraday = Interval.parse(label).intraday
        try:
            row = snapshot_row(snapshot, provider=provider.settings.provider, engine_version=persistence.engine_version,
                               provider_delay_seconds=provider.settings.delay_seconds,
                               warmup_start=context[label]["warmup_start"], warmup_bars=context[label]["bars"],
                               session_type=provider.calendar.classify(snapshot.timestamp).value if intraday else None)
        except SnapshotRowError:
            invalid += 1
            continue
        technical_shadow.submit_snapshot(row)
    return invalid


def main(argv=None, environ=None, *, provider=None, out=None):
    from market_data.config import MarketDataConfigError, load_market_data_settings
    from market_data.http import ProviderError
    from market_data.providers import build_provider
    from persistence.technical_settings import TechnicalPersistenceConfigError, load_technical_persistence_settings
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m technical.runner")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES))
    parser.add_argument("--text", action="store_true", help="also print the human-readable multi-timeframe view")
    try:
        args = parser.parse_args(argv)
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        timeframes = [Interval.parse(t.strip()) for t in args.timeframes.split(",") if t.strip()]
        if not symbols or not timeframes:
            raise ValueError
    except (SystemExit, ValueError, MarketDataError):
        logger.error("event=technical_run_failed reason=usage")
        return 2
    try:
        persistence = load_technical_persistence_settings(environ)
        if provider is None:
            provider = build_provider(load_market_data_settings(environ))
    except (MarketDataConfigError, MarketDataError, TechnicalPersistenceConfigError) as error:
        logger.error("event=technical_run_failed reason=config detail=%s", str(error).replace(" ", "_"))
        return 2
    failures = invalid = 0
    for symbol in symbols:
        try:
            multi, context = run_symbol(provider, symbol, timeframes)
        except (ProviderError, MarketDataError) as error:
            failures += 1
            logger.error("event=technical_symbol_failed symbol=%s kind=%s detail=%s", symbol,
                         getattr(error, "kind", "data"), str(error).replace(" ", "_"))
            continue
        for label, snapshot in multi.timeframes.items():
            _log_snapshot(symbol, label, snapshot, context[label])
        if args.text:
            print(format_multi_timeframe(multi), file=out)
        if persistence.enabled:
            invalid += _submit_snapshots(multi, context, provider, persistence)
    if persistence.enabled:
        _finish_persistence(persistence, invalid)
    logger.info("event=technical_run_finished symbols=%d failed=%d", len(symbols), failures)
    return 1 if failures else 0


def _finish_persistence(persistence, invalid):
    """Drain the shadow writer within the configured bound and log its accounting (never raises)."""
    try:
        from persistence import technical_shadow
        result = technical_shadow.shutdown(drain=True, timeout=persistence.drain_timeout_seconds)
        s = result["stats"]
        logger.info("event=technical_persistence engine_version=%s queued=%d persisted=%d duplicate=%d conflict=%d "
                    "failed=%d dropped_queue_full=%d dropped_shutdown=%d dropped_initializing=%d "
                    "failed_initializing=%d invalid_row=%d drained=%s", persistence.engine_version, s["queued"],
                    s["persisted"], s["duplicate"], s.get("conflict", 0), s["failed"], s["dropped_queue_full"],
                    s["dropped_shutdown"], s.get("dropped_initializing", 0), s.get("failed_initializing", 0),
                    invalid + s.get("invalid_row", 0), str(result["stopped"] and not result["timed_out"]).lower())
    except Exception:
        logger.warning("event=technical_persistence status=unavailable")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(main())
