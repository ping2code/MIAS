"""User-run, read-only live contract check of the Massive Stocks RANGE endpoint (Phase 7A).

    MARKET_DATA_PROVIDER=massive_stocks python -m market_data.massive_check [--symbol META] [--days 2] \
        [--pagination-limit 500] [--max-requests 8]

The key is read from ``MARKET_DATA_API_KEY`` in the process environment only
(never ``.env``) and sent only as the ``Authorization: Bearer`` header. It is never
printed.

**Read-only and bounded:**

- only GET requests to the configured Massive host;
- no database, ledger, Redis, Telegram or OpenAI access;
- a hard request cap, enforced in the HTTP client with retries included
  (``--max-requests``, default 8, at most 20);
- at most 5 sessions and one symbol; bounded HTTP timeouts.

It runs three checks through ``MassiveStocksProvider``, with a data delay of 0 so the
vendor's own delay is observable:

1. **5m range** over the last ``--days`` sessions;
2. **30m range** over the same window (1h and 1d are derived from it);
3. **pagination probe:** the latest session's 5m range with a small vendor ``limit``,
   to observe ``next_url`` and its host.

Output is one JSON document of metadata only: statuses, pages, ``next_url``
hosts, result and bar counts, ``vw``/``n`` presence counts, first and last bar
timestamps, observed freshness, HTTP status counts and rate-limit headers. It
contains no prices and no payloads.

Exit codes:

- 0: every check passed;
- 1: a provider or data failure, or a failed check;
- 2: configuration or usage error, including ``MARKET_DATA_PROVIDER`` not set to
  ``massive_stocks`` (NOT EXECUTED).
"""
import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import sys

from market_data.models import EXCHANGE_TZ, Interval, MarketDataError

MAX_DAYS, MAX_REQUESTS = 5, 20
EXPECTED_HOST = "api.massive.com"


def window(calendar, as_of, days):
    day = as_of.astimezone(EXCHANGE_TZ).date()
    if not calendar.is_trading_day(day):
        day = calendar.previous_trading_day(day)
    first = day
    for _ in range(days - 1):
        first = calendar.previous_trading_day(first)
    return datetime.combine(first, datetime.min.time(), tzinfo=EXCHANGE_TZ), day


def run(provider, *, symbol, days, pagination_limit, now):
    """(report, passed) — performs at most three range fetches (plus pagination) through the provider."""
    calendar = provider.calendar
    start, last_day = window(calendar, now, days)
    end = now + timedelta(seconds=1)
    fetches = {}
    for name, interval, begin, limit in (
            ("range_5m", Interval.M5, start, provider.page_limit),
            ("range_30m", Interval.M30, start, provider.page_limit),
            ("pagination_probe_5m", Interval.M5, datetime.combine(last_day, datetime.min.time(), tzinfo=EXCHANGE_TZ),
             pagination_limit)):
        provider.page_limit = limit
        before = len(provider.diagnostics)
        bars = provider.get_bars(symbol, interval, begin, end)
        diag = dict(provider.diagnostics[before]) if len(provider.diagnostics) > before else {}
        diag["returned_completed_bars"] = len(bars)
        if bars:
            diag["latest_bar_end"] = calendar.bar_end(bars[-1]).isoformat()
            diag["observed_age_seconds"] = int((now - calendar.bar_end(bars[-1])).total_seconds())
        fetches[name] = diag
    provider.page_limit = type(provider).page_limit
    statuses = sorted({s for d in fetches.values() for s in d.get("statuses", [])})
    hosts = sorted({h for d in fetches.values() for h in d.get("next_url_hosts", [])})
    status_counts = dict(sorted(provider.http.status_counts.items()))
    checks = dict(
        bearer_auth_accepted=status_counts.get(200, 0) > 0 and not (set(status_counts) & {401, 403}),
        status_ok_or_delayed=bool(statuses) and set(statuses) <= {"OK", "DELAYED"},
        results_nonempty=all(d.get("results", 0) > 0 for d in fetches.values()),
        timestamps_validated=all(d.get("kept_bars", 0) > 0 for d in fetches.values()),
        next_url_same_host=all(h == provider._host for h in hosts),
        adjusted_echo_matches=all(d.get("adjusted_echo") in ([], [str(provider.settings.adjusted)])
                                  for d in fetches.values()))
    report = dict(
        provider=provider.settings.provider, host=provider._host, expected_host=EXPECTED_HOST, symbol=symbol,
        sessions=days, window_start=start.isoformat(), checked_at=now.isoformat(timespec="seconds"),
        market_open_now=calendar.classify(now).value, statuses=statuses, next_url_hosts=hosts,
        pagination_observed=fetches["pagination_probe_5m"].get("pages", 0) > 1, fetches=fetches,
        optional_fields={name: dict(results=d.get("results", 0), with_vw=d.get("results_with_vw", 0),
                                    with_n=d.get("results_with_n", 0)) for name, d in fetches.items()},
        http_status_counts={str(k): v for k, v in status_counts.items()},
        rate_limit_headers=dict(provider.http.rate_limit_headers), requests=provider.http.requests_made,
        checks=checks, settings=provider.settings.safe_view(),
        note="freshness is meaningful only during market hours; data delay forced to 0 for this check")
    return report, all(checks.values())


def main(argv=None, environ=None, *, session=None, clock=None, sleep=None, out=None):
    from market_data.config import MarketDataConfigError, load_market_data_settings
    from market_data.http import ProviderError
    from market_data.providers.massive import MassiveStocksProvider
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m market_data.massive_check")
    parser.add_argument("--symbol", default="META")
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--pagination-limit", type=int, default=500)
    parser.add_argument("--max-requests", type=int, default=8)
    try:
        args = parser.parse_args(argv)
        symbol = args.symbol.strip().upper()
        if not (1 <= args.days <= MAX_DAYS and 1 <= args.max_requests <= MAX_REQUESTS
                and 50 <= args.pagination_limit <= 50_000 and symbol):
            raise ValueError
    except (SystemExit, ValueError):
        print(json.dumps(dict(live_validation="NOT EXECUTED", reason="usage error")), file=out)
        return 2
    try:
        settings = load_market_data_settings(environ)
    except MarketDataConfigError as error:
        print(json.dumps(dict(live_validation="NOT EXECUTED", reason=str(error))), file=out)
        return 2
    if settings.provider != "massive_stocks":
        print(json.dumps(dict(live_validation="NOT EXECUTED",
                              reason="set MARKET_DATA_PROVIDER=massive_stocks for this check")), file=out)
        return 2
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    try:
        provider = MassiveStocksProvider(replace(settings, delay_seconds=0), session=session, sleep=sleep,
                                         clock=lambda: now)
        provider.http.max_requests = args.max_requests
        report, passed = run(provider, symbol=symbol, days=args.days, pagination_limit=args.pagination_limit, now=now)
    except (ProviderError, MarketDataError) as error:
        print(json.dumps(dict(live_validation="FAILED", kind=getattr(error, "kind", "data"), error=str(error))), file=out)
        return 1
    report["live_validation"] = "PASSED" if passed else "FAILED"
    print(json.dumps(report, indent=2, sort_keys=True, default=str), file=out)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
