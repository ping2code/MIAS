"""Replay technical states over historical bars and describe what followed (Phase 4B research harness).

This is **not** a profitability backtest: there are no orders, fills, transaction
costs, position sizing or options P&L (profit and loss). The states come from the
causal ``TechnicalEngine.replay``. Forward labels (``evaluation.metrics``) are
computed afterwards, for evaluation only. Thresholds are never tuned here.

CLI (needs a configured provider; prints a JSON report with no raw bars):

    python -m evaluation.technical_replay --symbol META --interval 5m \
        --start 2026-06-01 --end 2026-07-01 [--horizons 1,3,5,10]

Exit codes: 0 success, 1 provider or data failure, 2 configuration or usage error.
"""
import argparse
from datetime import datetime
import json
import logging
import os
import sys

from evaluation import metrics
from market_data.models import EXCHANGE_TZ
from market_data.validation import validate_series
from technical.engine import TechnicalEngine

DEFAULT_HORIZONS = (1, 3, 5, 10)
NOTE = "Descriptive research only: not a backtest of profitability; no orders, costs or P&L."


def evaluate(bars, *, config=None, calendar=None, horizons=DEFAULT_HORIZONS, session_bounded=True):
    bars = validate_series(bars)
    if not bars:
        return dict(bars=0, note=NOTE)
    snapshots = TechnicalEngine(config, calendar).replay(bars)
    states = [s.signal.state for s in snapshots]
    labels = metrics.forward_labels(bars, horizons, session_bounded=session_bounded)
    return dict(symbol=bars[0].symbol, interval=bars[0].interval.label, bars=len(bars),
                first_bar=bars[0].timestamp.isoformat(), last_bar=bars[-1].timestamp.isoformat(),
                horizons=list(horizons), session_bounded=session_bounded and bars[0].interval.intraday,
                final_state=dict(state=snapshots[-1].signal.state, confidence=snapshots[-1].signal.confidence,
                                 trend=snapshots[-1].trend),
                state_frequency=metrics.state_frequency(states), durations=metrics.durations(states),
                transitions=metrics.transitions(states),
                forward=metrics.forward_by_state(states, labels, horizons), note=NOTE)


def _date(value):
    return datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), datetime.min.time(), tzinfo=EXCHANGE_TZ)


def main(argv=None, environ=None, *, provider=None):
    from market_data.config import MarketDataConfigError, load_market_data_settings
    from market_data.http import ProviderError
    from market_data.models import MarketDataError
    from market_data.providers import build_provider
    parser = argparse.ArgumentParser(prog="python -m evaluation.technical_replay")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", required=True)
    parser.add_argument("--start", required=True, type=_date)
    parser.add_argument("--end", required=True, type=_date)
    parser.add_argument("--horizons", default=",".join(map(str, DEFAULT_HORIZONS)))
    try:
        args = parser.parse_args(argv)
        horizons = tuple(int(h) for h in args.horizons.split(","))
        if not horizons or min(horizons) < 1:
            raise ValueError
    except (SystemExit, ValueError):
        return 2
    try:
        if provider is None:
            provider = build_provider(load_market_data_settings(os.environ if environ is None else environ))
    except (MarketDataConfigError, MarketDataError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    try:
        bars = provider.get_bars(args.symbol.upper(), args.interval, args.start, args.end)
        report = evaluate(bars, calendar=getattr(provider, "calendar", None), horizons=horizons)
    except (ProviderError, MarketDataError) as error:
        print(f"provider or data error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(main())
