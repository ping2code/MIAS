"""Test-only scheduler process: the CLI's signal wiring, but scheduling dummy fixture children.

    python -m tests.scheduler_fixture_app MARKER_DIR SLEEP [--ignore-term] [--grace S]
"""
import argparse
import logging
import signal
import sys

from orchestrator.scheduler import Scheduler
from tests.scheduler_helpers import fixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("marker_dir")
    parser.add_argument("sleep")
    parser.add_argument("--ignore-term", action="store_true")
    parser.add_argument("--grace", type=float, default=0.5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    extra = ["--ignore-term"] if args.ignore_term else []
    definitions = [fixture(name, "--sleep", args.sleep, "--grandchild", "--marker", f"{args.marker_dir}/{name}.jsonl", *extra,
                           interval=60, timeout=50, kill_grace=0.5) for name in ("news", "fed")]
    scheduler = Scheduler(definitions, shutdown_grace_seconds=args.grace, tick_seconds=0.05)
    for sig in (signal.SIGINT, signal.SIGTERM):  # Same handler as orchestrator.cli: only set the stop flag.
        signal.signal(sig, lambda *_: scheduler.request_stop())
    scheduler.run()
    statuses = {n: [r.status.value for r in scheduler.history[n]] for n in scheduler.history}
    print(f"statuses={statuses}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
