"""Replay vs incremental engine performance sanity check on a long synthetic sequence (Phase 4B).

    python -m evaluation.performance [--sessions 60]

It reports total runtime, the mean per-bar incremental update time, and retained
memory growth (``tracemalloc``) for both engines. This is a sanity check, not a
service-level target. Prices are synthetic.
"""
import argparse
from datetime import date
import json
import time
import tracemalloc

from market_data.calendar import default_calendar
from technical.engine import TechnicalEngine
from technical.incremental import IncrementalTechnicalEngine


def synthetic_bars(sessions):
    from tests.market_data_fakes import calendar_bars
    calendar = default_calendar()
    days = calendar.trading_days(date(2025, 1, 2), date(2026, 12, 31))[:sessions]
    return calendar_bars("NVDA", days, 5, base=180.0, calendar=calendar)


def measure(sessions=60):
    bars = synthetic_bars(sessions)
    tracemalloc.start()
    started = time.perf_counter()
    snapshots = TechnicalEngine().replay(bars)
    replay_seconds = time.perf_counter() - started
    _, replay_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del snapshots
    engine = IncrementalTechnicalEngine()
    tracemalloc.start()
    started = time.perf_counter()
    half = len(bars) // 2
    for bar in bars[:half]:
        engine.update(bar)
    middle, _ = tracemalloc.get_traced_memory()
    for bar in bars[half:]:
        engine.update(bar)
    incremental_seconds = time.perf_counter() - started
    end, incremental_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return dict(bars=len(bars), replay_seconds=round(replay_seconds, 3),
                replay_peak_mib=round(replay_peak / 2 ** 20, 2), incremental_seconds=round(incremental_seconds, 3),
                incremental_per_bar_ms=round(incremental_seconds / len(bars) * 1000, 4),
                incremental_peak_mib=round(incremental_peak / 2 ** 20, 2),
                incremental_retained_growth_second_half_kib=round((end - middle) / 1024, 1),
                note="replay keeps every snapshot (by design); incremental keeps bounded state and the latest snapshot")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m evaluation.performance")
    parser.add_argument("--sessions", type=int, default=60)
    print(json.dumps(measure(parser.parse_args().sessions), indent=2))
