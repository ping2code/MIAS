"""Market Context runner (Phase 7B.1): fetch each unique symbol once, build contexts, print JSON. Read-only.

    python -m market_context.runner --symbols META,NVDA,MSFT [--benchmarks SPY,QQQ] [--out FILE]

**Provider:** the configured ``MarketDataProvider`` (``MARKET_DATA_PROVIDER`` and
friends, from the process environment only; never ``.env``). This is the only
place Market Context touches a provider. The engine is pure.

**Clock:** read once per run. ``as_of = now - MARKET_DATA_DELAY_SECONDS`` (the
configured delay; no vendor-specific assumption).

- Fetched bars are cut to those completed at that single ``as_of``.
- The fetch window starts at midnight exchange time, two trading sessions
  before the latest trading day. That covers the context session and its
  previous session even before today's first bar completes.

**Budget:** the union of symbols and benchmarks, each fetched **exactly once**
(5m; one vendor page per symbol for this window). They are cached in memory for
this run only: no Redis, no database, no evidence ledger, no persistence, no
scheduler.

**Failures:** a provider error makes only that series unavailable; its bars are
empty, so its contexts report ``no_data`` and comparisons report
``no_symbol_data`` or ``no_benchmark_data``. The error kind is listed under
``errors``.

Output contains facts only: no recommendations, labels or scores, and never the
API key.

Exit codes: 0 all fetched; 1 any provider or data failure (the partial context is
still printed); 2 configuration or usage error.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys

from market_context.engine import build_market_context
from market_context.models import CONTEXT_INTERVAL, SeriesInput
from market_data.models import EXCHANGE_TZ, SYMBOL, Interval, MarketDataError

DEFAULT_BENCHMARKS = ("SPY", "QQQ")
MAX_UNIQUE_SYMBOLS = 20


def parse_symbols(text):
    symbols = [s.strip().upper() for s in (text or "").split(",") if s.strip()]
    if any(not SYMBOL.fullmatch(s) for s in symbols):
        raise ValueError("invalid symbol")
    return list(dict.fromkeys(symbols))


def fetch_window(as_of, calendar):
    day = as_of.astimezone(EXCHANGE_TZ).date()
    last = day if calendar.is_trading_day(day) else calendar.previous_trading_day(day)
    first = calendar.previous_trading_day(calendar.previous_trading_day(last))
    return datetime.combine(first, datetime.min.time(), tzinfo=EXCHANGE_TZ), as_of + timedelta(seconds=1)


def fetch_all(provider, symbols, *, as_of):
    """({symbol: SeriesInput}, {symbol: error kind}); each symbol is requested exactly once."""
    from market_data.completion import completed_bars
    from market_data.http import ProviderError
    calendar = provider.calendar
    start, end = fetch_window(as_of, calendar)
    cache, errors = {}, {}
    settings = provider.settings
    for symbol in symbols:
        if symbol in cache:
            continue
        try:
            bars = tuple(completed_bars(provider.get_bars(symbol, Interval.M5, start, end), as_of, calendar))
        except (ProviderError, MarketDataError) as error:
            errors[symbol] = getattr(error, "kind", "data")
            bars = ()
        cache[symbol] = SeriesInput(symbol=symbol, interval=CONTEXT_INTERVAL, bars=bars,
                                    provider_id=settings.provider, delay_seconds=int(settings.delay_seconds))
    return cache, errors


def run(provider, symbols, benchmarks, *, now):
    as_of = now - timedelta(seconds=int(provider.settings.delay_seconds))
    union = list(dict.fromkeys([*symbols, *benchmarks]))
    cache, errors = fetch_all(provider, union, as_of=as_of)
    extra = dict(adjusted=bool(provider.settings.adjusted),
                 include_extended_hours=bool(provider.settings.include_extended_hours))
    contexts = [build_market_context(cache[s], [cache[b] for b in benchmarks], now=now, as_of=as_of,
                                     calendar=provider.calendar, provenance=extra) for s in symbols]
    return contexts, errors


def main(argv=None, environ=None, *, provider=None, clock=None, out=None):
    from market_data.config import MarketDataConfigError, load_market_data_settings
    from market_data.providers import build_provider
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m market_context.runner")
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--benchmarks", default=",".join(DEFAULT_BENCHMARKS))
    parser.add_argument("--out", help="also write the JSON result to this file")
    try:
        args = parser.parse_args(argv)
        symbols, benchmarks = parse_symbols(args.symbols), parse_symbols(args.benchmarks)
        if not symbols or len(set(symbols) | set(benchmarks)) > MAX_UNIQUE_SYMBOLS:
            raise ValueError("symbol count")
    except (SystemExit, ValueError):
        print(json.dumps(dict(error="usage error")), file=out)
        return 2
    try:
        if provider is None:
            provider = build_provider(load_market_data_settings(environ))
    except (MarketDataConfigError, MarketDataError) as error:
        print(json.dumps(dict(error=str(error))), file=out)
        return 2
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    contexts, errors = run(provider, symbols, benchmarks, now=now)
    requests = getattr(getattr(provider, "http", None), "requests_made", None)
    result = dict(context_format_version=contexts[0].context_format_version, benchmarks=benchmarks,
                  contexts=[c.to_dict() for c in contexts], errors=errors, provider_requests=requests,
                  note="facts only; no interpretation")
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(text + "\n")
    print(text, file=out)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
