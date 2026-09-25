"""Bounded, read-only live validation of the configured market data provider (Phase 4C).

    python -m market_data.live_check [--symbols META,NVDA] [--intervals 5m,1h,1d] [--days 5] \
        [--max-requests 40] [--states]

- **Read-only:** only provider GET requests. No Telegram, no OpenAI, no database or
  Redis writes.
- **Bounded:** at most ``--days`` (1-10) recent trading sessions per check, at most 4
  symbols, and a hard request cap (``--max-requests``, default 40, at most 200)
  enforced in the HTTP client, retries included.
- **Safe output:** one JSON summary per (symbol, interval) on stdout, with
  metadata only. There are no raw payloads, no prices except the final state
  summary with ``--states``, and never the API key. Errors carry the error kind
  and a credential-free message.
- ``--states`` also runs the technical runner's warm-up fetch (larger windows) and
  reports the final technical state per timeframe.

Contract checks per result (``checks``):

- ``fields_and_types``: every result had t/o/h/l/c/v with the expected types
  (otherwise validation fails);
- ``timestamps_in_requested_range``: ``t`` interpreted as milliseconds landed inside
  the requested window;
- ``volume_valid``: every volume was a non-negative finite number. Fractional
  volume is valid and is counted in ``fractional_volumes``;
- ``session_grid``: every bar mapped to one session and sat on its grid;
- ``completed_only``: every returned bar ended at or before the data cut-off;
- ``adjusted_echo_matches``: the vendor echoed the requested ``adjusted`` flag.

Plus observations: pagination (pages), statuses (``OK``/``DELAYED``), rate-limit
headers and 429 counts, and the delay of the latest bar.

Exit codes: 0 all checks passed, 1 a provider/data failure or failed check, 2 configuration or usage error
(including no provider configured: live validation NOT EXECUTED).
"""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import sys

from market_data.models import EXCHANGE_TZ, Interval, MarketDataError

MAX_SYMBOLS, MAX_DAYS, MAX_REQUESTS = 4, 10, 200


def _window(calendar, as_of, days):
    day = as_of.astimezone(EXCHANGE_TZ).date()
    if not calendar.is_trading_day(day):
        day = calendar.previous_trading_day(day)
    first = day
    for _ in range(days - 1):
        first = calendar.previous_trading_day(first)
    return datetime.combine(first, datetime.min.time(), tzinfo=EXCHANGE_TZ), as_of + timedelta(seconds=1)


def check_one(provider, symbol, interval, days):
    from market_data.http import ProviderError
    from market_data.providers.polygon import DERIVED
    calendar, as_of = provider.calendar, provider.as_of()
    start, end = _window(calendar, as_of, days)
    before_diag, before_requests = len(provider.diagnostics), provider.http.requests_made
    before_429 = provider.http.status_counts.get(429, 0)
    summary = dict(provider=provider.settings.provider, symbol=symbol, requested_interval=interval.label,
                   source_interval=DERIVED.get(interval, interval).label, range_start=start.date().isoformat(),
                   range_end=as_of.astimezone(EXCHANGE_TZ).isoformat(timespec="minutes"),
                   data_cutoff_delay_seconds=provider.settings.delay_seconds, adjusted=provider.settings.adjusted)
    try:
        bars = provider.get_bars(symbol, interval, start, end)
    except (ProviderError, MarketDataError) as error:
        summary.update(validation="failed", error_kind=getattr(error, "kind", "data"), error=str(error)[:200],
                       requests=provider.http.requests_made - before_requests)
        return summary
    diagnostics = provider.diagnostics[before_diag:]
    raw = sum(d["raw_bars"] for d in diagnostics)
    ends = [calendar.bar_end(b) for b in bars]
    volume_types = sorted({t for d in diagnostics for t in d["volume_json_types"]})
    echo = sorted({a for d in diagnostics for a in d["adjusted_echo"]})
    checks = dict(
        fields_and_types=True, volume_valid=True, session_grid=True,  # Otherwise get_bars would have raised.
        timestamps_in_requested_range=all(start <= b.timestamp < end for b in bars) and (raw == 0 or bool(bars)),
        completed_only=all(e <= as_of for e in ends),
        adjusted_echo_matches=echo in ([], [str(provider.settings.adjusted)]))
    latest_delay = None if not ends else int((provider._clock() - max(ends)).total_seconds())
    summary.update(
        validation="ok" if all(checks.values()) else "check_failed", checks=checks, bar_count=len(bars),
        completed_bar_count=sum(e <= as_of for e in ends), source_bars_fetched=raw,
        first_timestamp=bars[0].timestamp.isoformat() if bars else None,
        last_timestamp=bars[-1].timestamp.isoformat() if bars else None, volume_json_types=volume_types,
        fractional_volumes=sum(d["fractional_volumes"] for d in diagnostics),
        max_fractional_volume_part=max((d["max_fractional_part"] for d in diagnostics if d["max_fractional_part"]),
                                       key=Decimal, default=None),
        pages=sum(d["pages"] for d in diagnostics), pagination_observed=any(d["pages"] > 1 for d in diagnostics),
        statuses=sorted({s for d in diagnostics for s in d["statuses"]}), adjusted_echo=echo,
        rate_limited_responses=provider.http.status_counts.get(429, 0) - before_429,
        min_request_interval_seconds=provider.settings.min_request_interval_seconds,
        rate_limit_headers=dict(provider.http.rate_limit_headers),
        latest_bar_age_seconds=latest_delay, requests=provider.http.requests_made - before_requests)
    return summary


def final_states(provider, symbol, intervals):
    from technical.runner import run_symbol
    multi, context = run_symbol(provider, symbol, intervals)
    return {label: None if s is None else dict(state=s.signal.state, confidence=s.signal.confidence, trend=s.trend,
                                               last_bar=s.timestamp.isoformat(), bars=context[label]["bars"],
                                               warmup_start=context[label]["warmup_start"].date().isoformat())
            for label, s in multi.timeframes.items()}


def main(argv=None, environ=None, *, provider=None, out=None):
    from market_data.config import MarketDataConfigError, load_market_data_settings
    from market_data.http import ProviderError
    from market_data.providers import build_provider
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m market_data.live_check")
    parser.add_argument("--symbols", default="META,NVDA")
    parser.add_argument("--intervals", default="5m,1h,1d")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--states", action="store_true")
    try:
        args = parser.parse_args(argv)
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        intervals = [Interval.parse(i.strip()) for i in args.intervals.split(",") if i.strip()]
        if not (symbols and intervals and len(symbols) <= MAX_SYMBOLS and 1 <= args.days <= MAX_DAYS
                and 1 <= args.max_requests <= MAX_REQUESTS):
            raise ValueError
    except (SystemExit, ValueError, MarketDataError):
        print(json.dumps(dict(live_validation="NOT EXECUTED", reason="usage error")), file=out)
        return 2
    try:
        if provider is None:
            settings = load_market_data_settings(os.environ if environ is None else environ)
            if settings.provider == "none":
                print(json.dumps(dict(live_validation="NOT EXECUTED",
                                      reason="MARKET_DATA_PROVIDER is not configured in the process environment")),
                      file=out)
                return 2
            provider = build_provider(settings)
    except (MarketDataConfigError, MarketDataError) as error:
        print(json.dumps(dict(live_validation="NOT EXECUTED", reason=str(error)[:200])), file=out)
        return 2
    provider.http.max_requests = args.max_requests
    ok = True
    for symbol in symbols:
        for interval in intervals:
            summary = check_one(provider, symbol, interval, args.days)
            ok = ok and summary["validation"] == "ok"
            print(json.dumps(summary, sort_keys=True), file=out)
        if args.states:
            try:
                states = final_states(provider, symbol, intervals)
            except (ProviderError, MarketDataError) as error:
                ok = False
                states = dict(error_kind=getattr(error, "kind", "data"), error=str(error)[:200])
            print(json.dumps(dict(symbol=symbol, final_states=states), sort_keys=True), file=out)
    print(json.dumps(dict(live_validation="EXECUTED", result="ok" if ok else "failed",
                          requests=provider.http.requests_made,
                          checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds")), sort_keys=True),
          file=out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
