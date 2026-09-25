"""Replay technical states over historical bars and describe what followed (evaluation format ``phase4d-v1``).

This is **not** a profitability backtest: there are no orders, fills, transaction
costs, position sizing or options P&L (profit and loss). The states come from the
causal ``TechnicalEngine.replay``. Forward labels (``evaluation.metrics``) are
computed afterwards, for evaluation only, and never feed back into states.
Thresholds are never tuned here.

**Session policy (forward-return labels only; state generation is unaffected):**

- ``--session-bound`` (the default for intraday, as in Phase 4B/4C): the horizon
  must end inside the same trading session.
- ``--no-session-bound``: the horizon counts the next ``h`` completed bars across
  sessions. Overnight, weekend and holiday gaps are skipped because no bars exist,
  so the return includes the gap. That answers a different question (holding across
  sessions).

For 1h, a regular session has 7 bars (4 on an early close), so session-bound
10-bar labels are structurally impossible. Use ``--horizons 1,2,3`` with
``--session-bound``, or ``1,3,5,10`` with ``--no-session-bound``.

CLI (needs a configured provider; prints JSON statistics only, never raw bars):

    python -m evaluation.technical_replay --symbol META --interval 1h --start 2025-01-02 --end 2026-09-24 \\
        [--horizons 1,2,3] [--session-bound | --no-session-bound] [--period-label A] \\
        [--research-pivot-window 3] [--seed 42042] [--iterations 1000] [--setup-events]

The seed defaults to ``EVALUATION_RANDOM_SEED`` (else 42042).

Exit codes: 0 success, 1 provider or data failure, 2 configuration or usage error.
"""
import argparse
from datetime import datetime
import json
import logging
import os
import sys

from evaluation import metrics, setup_lag, statistics
from evaluation.research import ResearchConfigError, differing_fields, is_production, research_config
from market_data.models import EXCHANGE_TZ
from market_data.validation import validate_series
from technical.engine import TechnicalEngine
from technical.signals import DIRECTION

FORMAT_VERSION = "phase4d-v1"
DEFAULT_HORIZONS = (1, 3, 5, 10)
NOTE = "Descriptive research only: not a backtest of profitability; no orders, costs or P&L."
WARNINGS = (
    "Multiple comparisons: many states, horizons, symbols, intervals and periods are examined; some apparently "
    "strong results will occur by chance. No statistical selection or tuning is performed.",
    "Forward-return observations overlap in time, so bootstrap intervals are optimistic (too narrow).",
    "MFE/MAE are excursions within the horizon, not achievable realized profit or loss.",
)


def evaluate(bars, *, config=None, calendar=None, horizons=DEFAULT_HORIZONS, session_bounded=True, period=None,
             seed=statistics.DEFAULT_SEED, iterations=statistics.DEFAULT_ITERATIONS, provider=None,
             include_setup_events=False):
    """``phase4d-v1`` report. ``config`` may differ from production only in ``pivot_window`` (research)."""
    from persistence.technical_settings import DEFAULT_ENGINE_VERSION
    changed = [] if config is None else differing_fields(config)
    if set(changed) - {"pivot_window"}:
        raise ResearchConfigError(f"research configurations may vary only pivot_window (got {', '.join(changed)})")
    bars = validate_series(bars)
    if not bars:
        return dict(evaluation_format_version=FORMAT_VERSION, bars=0, note=NOTE)
    config = config or research_config(2)
    symbol, interval = bars[0].symbol, bars[0].interval.label
    bounded = bool(session_bounded) and bars[0].interval.intraday
    period = period or {}
    snapshots = TechnicalEngine(config, calendar).replay(bars)
    states = [s.signal.state for s in snapshots]
    labels = metrics.forward_labels(bars, horizons, session_bounded=bounded)
    metadata = dict(
        symbol=symbol, interval=interval, period=dict(label=period.get("label"), start=period.get("start"),
                                                      end=period.get("end")),
        first_bar=bars[0].timestamp.isoformat(), last_bar=bars[-1].timestamp.isoformat(), bar_count=len(bars),
        session_bound=bounded, horizons=list(horizons), pivot_window=config.pivot_window,
        research_config=not is_production(config), engine_version=DEFAULT_ENGINE_VERSION, provider=provider,
        seed=seed, bootstrap_iterations=iterations, random_iterations=iterations,
        min_sample=statistics.MIN_SAMPLE, confidence=statistics.CONFIDENCE)
    key = f"{symbol}:{interval}:{period.get('label')}:pw{config.pivot_window}:sb{int(bounded)}"
    baseline, states_out = {}, {}
    for h in horizons:
        pool = [labels[t][h]["forward_return"] for t in range(len(bars)) if labels[t][h] is not None]
        baseline[str(h)] = dict(statistics.basic(pool), sample_label=statistics.sample_label(len(pool)))
        for state in sorted(set(states)):
            rows = [labels[t][h] for t, s in enumerate(states) if s == state and labels[t][h] is not None]
            direction = DIRECTION.get(state)
            if direction == 1:
                excursions = dict(mfe=[r["max_up"] for r in rows], mae=[r["max_down"] for r in rows])
            elif direction == -1:
                excursions = dict(mfe=[-r["max_down"] for r in rows], mae=[-r["max_up"] for r in rows])
            else:
                excursions = dict(max_up=[r["max_up"] for r in rows], max_down=[r["max_down"] for r in rows])
            entry = states_out.setdefault(state, dict(direction={1: "bullish", -1: "bearish"}.get(direction, "none"),
                                                      horizons={}))
            entry["horizons"][str(h)] = statistics.state_horizon(
                [r["forward_return"] for r in rows], pool, baseline[str(h)], seed=seed, key=f"{key}:{state}:{h}",
                iterations=iterations, excursions=excursions)
    events = setup_lag.setup_events(bars, snapshots)
    report = dict(
        evaluation_format_version=FORMAT_VERSION, note=NOTE, warnings=list(WARNINGS), metadata=metadata,
        final_state=dict(state=snapshots[-1].signal.state, confidence=snapshots[-1].signal.confidence,
                         trend=snapshots[-1].trend),
        state_frequency=metrics.state_frequency(states), durations=metrics.durations(states),
        transitions=metrics.transitions(states), transition_probabilities=metrics.transition_probabilities(states),
        baseline=baseline, states=states_out, setup_lag=setup_lag.summarize(events))
    if include_setup_events:
        report["setup_events"] = events
    return report


def _date(value):
    return datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), datetime.min.time(), tzinfo=EXCHANGE_TZ)


def default_seed(environ):
    raw = (environ.get("EVALUATION_RANDOM_SEED") or "").strip()
    return int(raw) if raw else statistics.DEFAULT_SEED


def main(argv=None, environ=None, *, provider=None, out=None):
    from market_data.config import MarketDataConfigError, load_market_data_settings
    from market_data.http import ProviderError
    from market_data.models import MarketDataError
    from market_data.providers import build_provider
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m evaluation.technical_replay")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", required=True)
    parser.add_argument("--start", required=True, type=_date)
    parser.add_argument("--end", required=True, type=_date)
    parser.add_argument("--horizons", default=",".join(map(str, DEFAULT_HORIZONS)))
    bound = parser.add_mutually_exclusive_group()
    bound.add_argument("--session-bound", dest="session_bound", action="store_true", default=True)
    bound.add_argument("--no-session-bound", dest="session_bound", action="store_false")
    parser.add_argument("--period-label")
    parser.add_argument("--research-pivot-window", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--iterations", type=int, default=statistics.DEFAULT_ITERATIONS)
    parser.add_argument("--setup-events", action="store_true")
    try:
        args = parser.parse_args(argv)
        horizons = tuple(int(h) for h in args.horizons.split(","))
        if not horizons or min(horizons) < 1 or len(set(horizons)) != len(horizons) or not 100 <= args.iterations <= 10000:
            raise ValueError
        seed = args.seed if args.seed is not None else default_seed(environ)
        config = None if args.research_pivot_window is None else research_config(args.research_pivot_window)
    except (SystemExit, ValueError, ResearchConfigError):
        return 2
    try:
        if provider is None:
            provider = build_provider(load_market_data_settings(environ))
    except (MarketDataConfigError, MarketDataError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    try:
        bars = provider.get_bars(args.symbol.upper(), args.interval, args.start, args.end)
        settings = getattr(provider, "settings", None)
        report = evaluate(bars, config=config, calendar=getattr(provider, "calendar", None), horizons=horizons,
                          session_bounded=args.session_bound, seed=seed, iterations=args.iterations,
                          period=dict(label=args.period_label, start=args.start.date().isoformat(),
                                      end=args.end.date().isoformat()),
                          provider=getattr(settings, "provider", None), include_setup_events=args.setup_events)
    except (ProviderError, MarketDataError) as error:
        print(f"provider or data error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True), file=out)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(main())
