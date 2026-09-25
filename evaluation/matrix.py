"""Bounded Phase 4D evaluation matrix: symbols × periods × intervals, one fetch per source series.

    python -m evaluation.matrix --out /tmp/mias-4d [--symbols META,NVDA,MSFT,SPY] \\
        [--periods A=2025-01-02:2026-09-24,B=2024-10-01:2025-01-01] [--intervals 5m,1h,1d] \\
        [--pivot-windows 1,2,3,4] [--seed 42042] [--iterations 1000] [--max-requests 400] [--dry-run]

Per (symbol, period), the source bars are fetched **once**: 5m natively, and 30m
for both 1h and 1d (derived exactly as in production). Then these runs are
evaluated, with no further requests:

| Interval | Profile | Session policy | Horizons | Pivot windows |
|---|---|---|---|---|
| 5m | ``sb`` | session-bound | 1, 3, 5, 10 | 2 (production) |
| 1h | ``sb`` | session-bound | 1, 2, 3 | 2 |
| 1h | ``xs`` | no-session-bound (cross-session research) | 1, 3, 5, 10 | 2 |
| 1d | ``d`` | not applicable | 1, 3, 5, 10 | ``--pivot-windows`` (2 = production; others are research-only) |

**Outputs** (statistics only, never raw bars):

- one ``phase4d-v1`` JSON per run: ``{symbol}_{interval}_{period}_{profile}_pw{n}.json``;
- ``summary.json`` (cross-symbol, cross-period and pivot-window research);
- ``plan.json`` (the plan, the budget estimate, actual request counts and failures, and per-fetch
  diagnostics: pages, bar counts, ``excluded_overnight_bars`` and fractional-volume counts).

**Budget:** ``--dry-run`` prints the estimate and makes no requests.

- The estimate assumes the vendor's 50,000 limit counts base minute aggregates, as
  observed in Phase 4C: ``pages = ceil(sessions × 960 / 50000)`` per fetch.
- Runtime is estimated as requests × the configured request spacing.
- ``--max-requests`` is a hard cap enforced by the HTTP client, retries included.

Exit codes: 0 all runs succeeded, 1 any provider/data failure (other runs still
complete), 2 configuration or usage error.
"""
import argparse
from datetime import datetime, timedelta
import json
import math
import os
import sys

from evaluation import statistics
from evaluation.research import ResearchConfigError, research_config
from evaluation.summary import cross_period, cross_symbol, pivot_research
from evaluation.technical_replay import default_seed, evaluate
from market_data.models import EXCHANGE_TZ, Interval, MarketDataError

DEFAULT_SYMBOLS = ("META", "NVDA", "MSFT", "SPY")
DEFAULT_PERIODS = "A=2025-01-02:2026-09-24,B=2024-10-01:2025-01-01"
MINUTES_PER_SESSION = 960  # 04:00-20:00 base minute aggregates, including extended hours.
VENDOR_LIMIT = 50_000


def parse_periods(text):
    periods = []
    for item in text.split(","):
        label, _, span = item.partition("=")
        start, _, end = span.partition(":")
        start_d, end_d = datetime.strptime(start, "%Y-%m-%d").date(), datetime.strptime(end, "%Y-%m-%d").date()
        if not label or end_d <= start_d:
            raise ValueError("bad period")
        periods.append(dict(label=label.strip(), start=start_d, end=end_d))
    ordered = sorted(periods, key=lambda p: p["start"])
    for a, b in zip(ordered, ordered[1:]):
        if b["start"] < a["end"]:
            raise ValueError("periods overlap")
    if len({p["label"] for p in periods}) != len(periods):
        raise ValueError("duplicate period label")
    return periods


def runs_for(intervals, pivot_windows):
    runs = []
    if "5m" in intervals:
        runs.append(dict(interval="5m", source="5m", profile="sb", session_bound=True, horizons=(1, 3, 5, 10), pivot=2))
    if "1h" in intervals:
        runs.append(dict(interval="1h", source="30m", profile="sb", session_bound=True, horizons=(1, 2, 3), pivot=2))
        runs.append(dict(interval="1h", source="30m", profile="xs", session_bound=False, horizons=(1, 3, 5, 10),
                         pivot=2))
    if "1d" in intervals:
        for window in pivot_windows:
            runs.append(dict(interval="1d", source="30m", profile="d", session_bound=False, horizons=(1, 3, 5, 10),
                             pivot=window))
    return runs


def estimate(calendar, symbols, periods, runs, spacing):
    sources = sorted({r["source"] for r in runs})
    per_period = []
    for p in periods:
        sessions = len(calendar.trading_days(p["start"], p["end"] - timedelta(days=1)))
        pages = math.ceil(sessions * MINUTES_PER_SESSION / VENDOR_LIMIT) if sessions else 0
        per_period.append(dict(period=p["label"], sessions=sessions, pages_per_fetch=pages,
                               requests_per_symbol=pages * len(sources)))
    per_symbol = sum(p["requests_per_symbol"] for p in per_period)
    total = per_symbol * len(symbols)
    return dict(sources=sources, periods=per_period, requests_per_symbol=per_symbol, requests_total=total,
                runs_per_symbol_period=len(runs), request_spacing_seconds=spacing,
                estimated_request_minutes=round(total * spacing / 60, 1))


def _et(day):
    return datetime.combine(day, datetime.min.time(), tzinfo=EXCHANGE_TZ)


def run_matrix(provider, *, symbols, periods, runs, out_dir, seed, iterations):
    from market_data.aggregation import derive_completed
    from market_data.http import ProviderError
    calendar, as_of = provider.calendar, provider.as_of()
    reports, failures, written, fetches = [], [], [], []
    for symbol in symbols:
        for period in periods:
            fetched = {}
            try:
                for source in sorted({r["source"] for r in runs}):
                    before = len(provider.diagnostics)
                    fetched[source] = provider.get_bars(symbol, source, _et(period["start"]), _et(period["end"]))
                    for d in provider.diagnostics[before:]:
                        fetches.append(dict(symbol=symbol, period=period["label"], source_interval=d["source_interval"],
                                            pages=d["pages"], statuses=d["statuses"], raw_bars=d["raw_bars"],
                                            kept_bars=d["kept_bars"],
                                            excluded_overnight_bars=d.get("excluded_overnight_bars", 0),
                                            fractional_volumes=d["fractional_volumes"]))
            except (ProviderError, MarketDataError) as error:
                failures.append(dict(symbol=symbol, period=period["label"], kind=getattr(error, "kind", "data"),
                                     error=str(error)[:200]))
                continue
            for run in runs:
                source_bars = fetched[run["source"]]
                bars = source_bars if run["source"] == run["interval"] else derive_completed(
                    source_bars, run["interval"], calendar, as_of)
                if not bars:
                    failures.append(dict(symbol=symbol, period=period["label"], interval=run["interval"],
                                         kind="empty", error="no completed bars in period"))
                    continue
                config = None if run["pivot"] == 2 else research_config(run["pivot"])
                report = evaluate(bars, config=config, calendar=calendar, horizons=run["horizons"],
                                  session_bounded=run["session_bound"], seed=seed, iterations=iterations,
                                  provider=provider.settings.provider,
                                  period=dict(label=period["label"], start=period["start"].isoformat(),
                                              end=period["end"].isoformat()))
                name = f"{symbol}_{run['interval']}_{period['label']}_{run['profile']}_pw{run['pivot']}.json"
                with open(os.path.join(out_dir, name), "w") as handle:
                    json.dump(report, handle, indent=2, sort_keys=True)
                reports.append(report)
                written.append(name)
    production = [r for r in reports if not r["metadata"]["research_config"]]
    summary = dict(evaluation_format_version="phase4d-v1",
                   cross_symbol=cross_symbol(production), cross_period=cross_period(production),
                   pivot_research=pivot_research([r for r in reports if r["metadata"]["interval"] == "1d"]))
    with open(os.path.join(out_dir, "summary.json"), "w") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
    return reports, failures, written, fetches


def main(argv=None, environ=None, *, provider=None, out=None):
    from market_data.config import MarketDataConfigError, load_market_data_settings
    from market_data.providers import build_provider
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m evaluation.matrix")
    parser.add_argument("--out", required=True)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--periods", default=DEFAULT_PERIODS)
    parser.add_argument("--intervals", default="5m,1h,1d")
    parser.add_argument("--pivot-windows", default="1,2,3,4")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--iterations", type=int, default=statistics.DEFAULT_ITERATIONS)
    parser.add_argument("--max-requests", type=int, default=400)
    parser.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        periods = parse_periods(args.periods)
        intervals = [Interval.parse(i.strip()).label for i in args.intervals.split(",") if i.strip()]
        if set(intervals) - {"5m", "1h", "1d"}:
            raise ValueError
        windows = [int(w) for w in args.pivot_windows.split(",")]
        for w in windows:
            research_config(w)
        if not symbols or len(symbols) > 8 or not 100 <= args.iterations <= 10000 or not 1 <= args.max_requests <= 2000:
            raise ValueError
        seed = args.seed if args.seed is not None else default_seed(environ)
    except (SystemExit, ValueError, MarketDataError, ResearchConfigError):
        print(json.dumps(dict(error="usage")), file=out)
        return 2
    runs = runs_for(intervals, windows)
    try:
        if provider is None:
            settings = load_market_data_settings(environ)
            if settings.provider == "none":
                if not args.dry_run:
                    raise MarketDataConfigError("MARKET_DATA_PROVIDER is not configured")
                from market_data.calendar import default_calendar
                calendar, spacing = default_calendar(), settings.min_request_interval_seconds
            else:
                provider = build_provider(settings)
        if provider is not None:
            calendar, spacing = provider.calendar, provider.settings.min_request_interval_seconds
    except (MarketDataConfigError, MarketDataError) as error:
        print(json.dumps(dict(error=str(error)[:200])), file=out)
        return 2
    plan = dict(symbols=symbols, periods=[dict(p, start=p["start"].isoformat(), end=p["end"].isoformat())
                                          for p in periods],
                runs=[dict(r, horizons=list(r["horizons"])) for r in runs], seed=seed, iterations=args.iterations,
                budget=estimate(calendar, symbols, periods, runs, spacing), max_requests=args.max_requests)
    if args.dry_run:
        print(json.dumps(dict(plan, dry_run=True), indent=2), file=out)
        return 0
    if plan["budget"]["requests_total"] > args.max_requests:
        print(json.dumps(dict(error="estimated requests exceed --max-requests", budget=plan["budget"])), file=out)
        return 2
    os.makedirs(args.out, exist_ok=True)
    provider.http.max_requests = args.max_requests
    reports, failures, written, fetches = run_matrix(provider, symbols=symbols, periods=periods, runs=runs,
                                                     out_dir=args.out, seed=seed, iterations=args.iterations)
    plan.update(requests_made=provider.http.requests_made, rate_limited_responses=provider.http.status_counts.get(429, 0),
                files=written, failures=failures, fetch_diagnostics=fetches,
                excluded_overnight_bars=sum(f["excluded_overnight_bars"] for f in fetches))
    with open(os.path.join(args.out, "plan.json"), "w") as handle:
        json.dump(plan, handle, indent=2)
    print(json.dumps(dict(result="ok" if not failures else "partial", reports=len(written), failures=len(failures),
                          requests_made=plan["requests_made"], out=args.out)), file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
