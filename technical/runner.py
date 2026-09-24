"""One-shot technical job: fetch completed bars, warm the incremental engine, log each timeframe's state (Phase 4B).

    python -m technical.runner [--symbols META,NVDA] [--timeframes 1d,1h,5m] [--text]

- Settings come from the process environment (``market_data.config``; ``.env`` is never read).
- Each run is stateless: it warms the engine from recent history and reports the
  latest **completed** bar per timeframe. It is not a daemon.
- Each source interval is fetched once per symbol. 1h and 1d are derived from 30m,
  so a symbol costs two requests for the default timeframes.
- Output is structured ``key=value`` log lines (and an optional text view). There is
  no Telegram, no alert decision and no persistence; timeframes are never combined.

Exit codes: 0 success, 1 provider or data failure for any symbol, 2 configuration or usage error.
"""
import argparse
from datetime import timedelta
import logging
import os
import sys

from market_data.aggregation import derive_completed
from market_data.models import Interval, MarketDataError
from technical.formatter import format_multi_timeframe
from technical.incremental import IncrementalTechnicalEngine
from technical.multitimeframe import DEFAULT_TIMEFRAMES, from_engine

logger = logging.getLogger("technical.runner")
DEFAULT_SYMBOLS = ("META", "NVDA")
SOURCE = {Interval.M1: Interval.M1, Interval.M5: Interval.M5, Interval.M15: Interval.M15, Interval.M30: Interval.M30,
          Interval.H1: Interval.M30, Interval.D1: Interval.M30}
# Calendar-day lookback per source so that EMA200 can warm up on every derived timeframe.
LOOKBACK = {Interval.M1: timedelta(days=5), Interval.M5: timedelta(days=14), Interval.M15: timedelta(days=45),
            Interval.M30: timedelta(days=400)}


def run_symbol(provider, symbol, timeframes):
    """MultiTimeframeSnapshot for one symbol plus per-timeframe bar counts."""
    calendar = provider.calendar
    as_of = provider.as_of()
    engine = IncrementalTechnicalEngine(calendar=calendar)
    fetched, counts = {}, {}
    for interval in timeframes:
        source = SOURCE[interval]
        if source not in fetched:
            fetched[source] = provider.get_bars(symbol, source, as_of - LOOKBACK[source], as_of + timedelta(seconds=1))
        bars = fetched[source] if source == interval else derive_completed(fetched[source], interval, calendar, as_of)
        counts[interval.label] = len(bars)
        engine.warmup(bars)
    return from_engine(engine, symbol, [t.label for t in timeframes]), counts


def _log_snapshot(symbol, label, snapshot, bars):
    if snapshot is None:
        logger.info("event=technical_snapshot symbol=%s interval=%s bars=%d state=unavailable", symbol, label, bars)
        return
    logger.info("event=technical_snapshot symbol=%s interval=%s bars=%d last_bar=%s state=%s confidence=%s trend=%s "
                "high_type=%s low_type=%s rsi=%s vwap=%s", symbol, label, bars, snapshot.timestamp.isoformat(),
                snapshot.signal.state, snapshot.signal.confidence, snapshot.trend, snapshot.last_high_type,
                snapshot.last_low_type, "n/a" if snapshot.rsi is None else f"{snapshot.rsi:.1f}",
                snapshot.vwap_state["position"])


def main(argv=None, environ=None, *, provider=None, out=None):
    from market_data.config import MarketDataConfigError, load_market_data_settings
    from market_data.http import ProviderError
    from market_data.providers import build_provider
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
        if provider is None:
            provider = build_provider(load_market_data_settings(os.environ if environ is None else environ))
    except (MarketDataConfigError, MarketDataError) as error:
        logger.error("event=technical_run_failed reason=config detail=%s", str(error).replace(" ", "_"))
        return 2
    failures = 0
    for symbol in symbols:
        try:
            multi, counts = run_symbol(provider, symbol, timeframes)
        except (ProviderError, MarketDataError) as error:
            failures += 1
            logger.error("event=technical_symbol_failed symbol=%s kind=%s detail=%s", symbol,
                         getattr(error, "kind", "data"), str(error).replace(" ", "_"))
            continue
        for label, snapshot in multi.timeframes.items():
            _log_snapshot(symbol, label, snapshot, counts[label])
        if args.text:
            print(format_multi_timeframe(multi), file=out)
    logger.info("event=technical_run_finished symbols=%d failed=%d", len(symbols), failures)
    return 1 if failures else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(main())
