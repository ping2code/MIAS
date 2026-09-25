"""Bounded evaluation matrix (Phase 4D, extended in Phase 5): symbols × periods × intervals, one fetch per source.

    python -m evaluation.matrix --out /tmp/mias-5 [--symbols META,NVDA,MSFT,SPY,JPM,UNH,CAT,XOM] \\
        [--periods A=2025-01-02:2026-09-24,B=2024-10-01:2025-01-01] [--intervals 5m,1h,1d] \\
        [--pivot-windows 1,2,3,4] [--pseudo-split A=2025-11-03] [--seed 42042] [--iterations 1000] \\
        [--max-requests 400] [--resume] [--dry-run]

Per (symbol, period), the source bars are fetched **once**: 5m natively, and 30m
for both 1h and 1d (derived exactly as in production). Then these runs are
evaluated, with no further requests:

| Interval | Profile | Session policy | Horizons | Pivot windows |
|---|---|---|---|---|
| 5m | ``sb`` | session-bound | 1, 3, 5, 10 | 2 (production) |
| 1h | ``sb`` | session-bound | 1, 3 | ``--pivot-windows`` (2 = production; others research-only) |
| 1h | ``xs`` | no-session-bound (cross-session research) | 1, 3, 5, 10 | ``--pivot-windows`` |
| 1d | ``d`` | not applicable | 1, 3, 5, 10 | 2 |

``--pseudo-split A=YYYY-MM-DD`` also evaluates 1d (pivot window 2) separately on
the two halves of period A, from the same fetched bars (labels ``A1``/``A2``;
``pseudo_holdout: true``; no extra requests). This is a pre-registered,
descriptive-only secondary split, never validation.

**Progress:** one line per fetch and per report is written to stderr:
``[done/total] symbol=… period=… …``, with elapsed seconds and an ETA. There is
never a key, bar or payload.

**Resume (``--resume``):** each existing report is re-read and validated (JSON,
format version, and metadata matching the run: symbol, interval, period, pivot
window, session policy, horizons, seed and iterations). Valid reports are
skipped; missing or invalid ones are recomputed, fetching only symbol-periods
that still need work. The summary is always regenerated deterministically from
every report.

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
import time

import numpy as np

from evaluation import statistics
from evaluation.research import ResearchConfigError, research_config
from evaluation.summary import cross_period, cross_symbol, pivot_research
from evaluation.technical_replay import default_seed, evaluate
from market_data.models import EXCHANGE_TZ, Interval, MarketDataError

DEFAULT_SYMBOLS = ("META", "NVDA", "MSFT", "SPY", "JPM", "UNH", "CAT", "XOM")
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
        for window in pivot_windows:
            runs.append(dict(interval="1h", source="30m", profile="sb", session_bound=True, horizons=(1, 3),
                             pivot=window))
            runs.append(dict(interval="1h", source="30m", profile="xs", session_bound=False, horizons=(1, 3, 5, 10),
                             pivot=window))
    if "1d" in intervals:
        runs.append(dict(interval="1d", source="30m", profile="d", session_bound=False, horizons=(1, 3, 5, 10), pivot=2))
    return runs


def parse_split(text, periods):
    if not text:
        return None
    label, _, day = text.partition("=")
    split = datetime.strptime(day, "%Y-%m-%d").date()
    period = next((p for p in periods if p["label"] == label), None)
    if period is None or not period["start"] < split < period["end"]:
        raise ValueError("pseudo split must fall inside a configured period")
    return dict(label=label, date=split)


def run_name(symbol, run, period_label):
    return f"{symbol}_{run['interval']}_{period_label}_{run['profile']}_pw{run['pivot']}.json"


def valid_report(path, symbol, run, period_label, seed, iterations):
    """True only for a readable phase5-v1 report whose metadata matches this exact run."""
    from evaluation.technical_replay import FORMAT_VERSION
    try:
        with open(path) as handle:
            report = json.load(handle)
        m = report["metadata"]
        return (report["evaluation_format_version"] == FORMAT_VERSION and m["symbol"] == symbol
                and m["interval"] == run["interval"] and m["period"]["label"] == period_label
                and m["pivot_window"] == run["pivot"] and m["session_bound"] == (run["session_bound"] and
                                                                                   run["interval"] != "1d")
                and m["horizons"] == list(run["horizons"]) and m["seed"] == seed
                and m["bootstrap_iterations"] == iterations and m["bar_count"] > 0), report
    except (OSError, ValueError, KeyError, TypeError):
        return False, None


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


class Progress:
    """Safe progress lines on stderr: counts, labels, pages, timings. Never keys, bars or payloads."""

    def __init__(self, total, stream, clock=time.monotonic):
        self.total, self.done, self.stream, self.clock = total, 0, stream, clock
        self.started = clock()

    def line(self, **fields):
        elapsed = self.clock() - self.started
        eta = "unknown" if not self.done else f"{elapsed / self.done * (self.total - self.done):.0f}"
        text = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[{self.done}/{self.total}] {text} elapsed_seconds={elapsed:.0f} estimated_remaining_seconds={eta}",
              file=self.stream, flush=True)


def run_matrix(provider, *, symbols, periods, runs, out_dir, seed, iterations, split=None, resume=False,
               progress_stream=None):
    from market_data.aggregation import derive_completed
    from market_data.http import ProviderError
    calendar, as_of = provider.calendar, provider.as_of()
    reports, failures, written, fetches, skipped = [], [], [], [], []
    plan_runs = []
    for symbol in symbols:
        for period in periods:
            for run in runs:
                plan_runs.append((symbol, period, run, period["label"], None))
            if split and split["label"] == period["label"] and any(r["interval"] == "1d" for r in runs):
                daily = next(r for r in runs if r["interval"] == "1d")
                plan_runs.append((symbol, period, daily, f"{period['label']}1", "first"))
                plan_runs.append((symbol, period, daily, f"{period['label']}2", "second"))
    progress = Progress(len(plan_runs), progress_stream or sys.stderr)
    for symbol in symbols:
        for period in periods:
            todo = []
            for item in [p for p in plan_runs if p[0] == symbol and p[1] is period]:
                path = os.path.join(out_dir, run_name(symbol, item[2], item[3]))
                ok, report = valid_report(path, symbol, item[2], item[3], seed, iterations) if resume else (False, None)
                if ok:
                    reports.append(report)
                    skipped.append(os.path.basename(path))
                    progress.done += 1
                    progress.line(symbol=symbol, period=item[3], interval=item[2]["interval"],
                                  mode=item[2]["profile"], pivot_window=item[2]["pivot"], status="resumed_valid")
                else:
                    if resume and os.path.exists(path):
                        progress.line(symbol=symbol, period=item[3], interval=item[2]["interval"],
                                      mode=item[2]["profile"], pivot_window=item[2]["pivot"], status="rerun_invalid")
                    todo.append(item)
            if not todo:
                continue
            fetched = {}
            try:
                for source in sorted({item[2]["source"] for item in todo}):
                    before = len(provider.diagnostics)
                    fetched[source] = provider.get_bars(symbol, source, _et(period["start"]), _et(period["end"]))
                    for d in provider.diagnostics[before:]:
                        fetches.append(dict(symbol=symbol, period=period["label"], source_interval=d["source_interval"],
                                            pages=d["pages"], statuses=d["statuses"], raw_bars=d["raw_bars"],
                                            kept_bars=d["kept_bars"],
                                            excluded_overnight_bars=d.get("excluded_overnight_bars", 0),
                                            fractional_volumes=d["fractional_volumes"]))
                        progress.line(symbol=symbol, period=period["label"], step="fetch",
                                      source=d["source_interval"], fetch_pages=d["pages"], bars=d["kept_bars"],
                                      excluded_overnight=d.get("excluded_overnight_bars", 0))
            except (ProviderError, MarketDataError) as error:
                failures.append(dict(symbol=symbol, period=period["label"], kind=getattr(error, "kind", "data"),
                                     error=str(error)[:200]))
                progress.done += len(todo)
                progress.line(symbol=symbol, period=period["label"], status="failed",
                              kind=getattr(error, "kind", "data"))
                continue
            for _, _, run, label, half in todo:
                source_bars = fetched[run["source"]]
                if half is not None:
                    cut = _et(split["date"])
                    source_bars = [b for b in source_bars if (b.timestamp < cut) == (half == "first")]
                bars = source_bars if run["source"] == run["interval"] else derive_completed(
                    source_bars, run["interval"], calendar, as_of)
                progress.done += 1
                if not bars:
                    failures.append(dict(symbol=symbol, period=label, interval=run["interval"], kind="empty",
                                         error="no completed bars in period"))
                    continue
                config = None if run["pivot"] == 2 else research_config(run["pivot"])
                span = (dict(label=label, start=period["start"].isoformat(), end=period["end"].isoformat())
                        if half is None else dict(label=label, start=(split["date"] if half == "second"
                                                                      else period["start"]).isoformat(),
                                                  end=(period["end"] if half == "second" else split["date"]).isoformat(),
                                                  pseudo_holdout=True))
                report = evaluate(bars, config=config, calendar=calendar, horizons=run["horizons"],
                                  session_bounded=run["session_bound"], seed=seed, iterations=iterations,
                                  provider=provider.settings.provider, period=span)
                name = run_name(symbol, run, label)
                with open(os.path.join(out_dir, name), "w") as handle:
                    json.dump(report, handle, indent=2, sort_keys=True)
                reports.append(report)
                written.append(name)
                progress.line(symbol=symbol, period=label, interval=run["interval"], mode=run["profile"],
                              pivot_window=run["pivot"], reports_written=len(written))
    write_summary(reports, out_dir)
    return reports, failures, written, fetches, skipped


def correlations(reports):
    """Pairwise Pearson correlation of daily close-to-close returns per period (dependence description only)."""
    by_period = {}
    for r in reports:
        m = r["metadata"]
        if m["interval"] == "1d" and not m["research_config"] and not m["period"].get("pseudo_holdout") \
                and r.get("daily_returns"):
            by_period.setdefault(m["period"]["label"], {})[m["symbol"]] = dict(r["daily_returns"])
    out = {}
    for label, series in sorted(by_period.items()):
        symbols = sorted(series)
        pairs, values = {}, []
        for i, a in enumerate(symbols):
            for b in symbols[i + 1:]:
                days = sorted(set(series[a]) & set(series[b]))
                if len(days) < 30:
                    continue
                corr = float(np.corrcoef([series[a][d] for d in days], [series[b][d] for d in days])[0, 1])
                pairs[f"{a}-{b}"] = round(corr, 4)
                values.append(corr)
        n = len(symbols)
        mean_corr = sum(values) / len(values) if values else None
        out[label] = dict(symbols=symbols, pairs=pairs, mean_pairwise=None if mean_corr is None else round(mean_corr, 4),
                          effective_independent_symbols=None if mean_corr is None else
                          round(n / (1 + (n - 1) * max(mean_corr, 0.0)), 2))
    return out


def write_summary(reports, out_dir):
    reports = sorted(reports, key=lambda r: (r["metadata"]["symbol"], str(r["metadata"]["period"]["label"]),
                                             r["metadata"]["interval"], r["metadata"]["pivot_window"],
                                             r["metadata"]["session_bound"]))
    production = [r for r in reports if not r["metadata"]["research_config"]
                  and not r["metadata"]["period"].get("pseudo_holdout")]
    summary = dict(evaluation_format_version=reports[0]["evaluation_format_version"] if reports else None,
                   cross_symbol=cross_symbol(production), cross_period=cross_period(production),
                   pivot_research=pivot_research([r for r in reports if r["metadata"]["interval"] == "1h"
                                                  and r["metadata"]["session_bound"]]),
                   correlations=correlations(reports))
    with open(os.path.join(out_dir, "summary.json"), "w") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
    return summary


def main(argv=None, environ=None, *, provider=None, out=None, err=None):
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
    parser.add_argument("--pseudo-split", default="A=2025-11-03")
    parser.add_argument("--resume", action="store_true")
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
        split = parse_split(args.pseudo_split, periods)
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
    from evaluation.hypotheses import REGISTERED_HASHES
    per_symbol_period = len(runs)
    extra = 2 * len(symbols) if split and any(r["interval"] == "1d" for r in runs) else 0
    plan = dict(symbols=symbols, periods=[dict(p, start=p["start"].isoformat(), end=p["end"].isoformat())
                                          for p in periods],
                runs=[dict(r, horizons=list(r["horizons"])) for r in runs], seed=seed, iterations=args.iterations,
                pseudo_split=None if not split else dict(label=split["label"], date=split["date"].isoformat()),
                research_configs=sorted({r["pivot"] for r in runs if r["pivot"] != 2}),
                expected_reports=per_symbol_period * len(symbols) * len(periods) + extra,
                hypothesis_hashes=REGISTERED_HASHES,
                budget=estimate(calendar, symbols, periods, runs, spacing), max_requests=args.max_requests)
    if args.dry_run:
        print(json.dumps(dict(plan, dry_run=True), indent=2), file=out)
        return 0
    if plan["budget"]["requests_total"] > args.max_requests:
        print(json.dumps(dict(error="estimated requests exceed --max-requests", budget=plan["budget"])), file=out)
        return 2
    os.makedirs(args.out, exist_ok=True)
    provider.http.max_requests = args.max_requests
    reports, failures, written, fetches, skipped = run_matrix(
        provider, symbols=symbols, periods=periods, runs=runs, out_dir=args.out, seed=seed,
        iterations=args.iterations, split=split, resume=args.resume, progress_stream=err)
    plan.update(requests_made=provider.http.requests_made, rate_limited_responses=provider.http.status_counts.get(429, 0),
                files=written, resumed_valid=skipped, failures=failures, fetch_diagnostics=fetches,
                excluded_overnight_bars=sum(f["excluded_overnight_bars"] for f in fetches))
    with open(os.path.join(args.out, "plan.json"), "w") as handle:
        json.dump(plan, handle, indent=2)
    print(json.dumps(dict(result="ok" if not failures else "partial", reports=len(written), resumed=len(skipped),
                          failures=len(failures), requests_made=plan["requests_made"], out=args.out)), file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
