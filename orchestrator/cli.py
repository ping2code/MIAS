"""MIAS scheduler command line (settings from the process environment only; never ``.env``).

    python -m orchestrator.cli status-config   # validate and print safe settings + registry; exit 0 / 2
    python -m orchestrator.cli run             # scheduling loop (requires MIAS_SCHEDULER_ENABLED=true)
    python -m orchestrator.cli run-once        # each enabled collector once (requires MIAS_SCHEDULER_ENABLED=true)

``MIAS_SCHEDULER_DRY_RUN=true`` makes ``run``/``run-once`` compute and log due jobs without executing collectors.

Exit codes:

- 0: ``run`` stopped cleanly; ``run-once``: every enabled collector succeeded (or dry-run).
- 1: ``run-once`` had at least one failed, timed-out or cancelled collector.
- 2: invalid configuration or usage.
- 3: the scheduler is disabled (``MIAS_SCHEDULER_ENABLED`` is not true).

Collector output goes to the scheduler's stdout/stderr (``MIAS_SCHEDULER_CHILD_OUTPUT=inherit``,
the default) or is discarded; it is never stored or parsed. SIGINT/SIGTERM trigger the bounded
shutdown.
"""
import argparse
import json
import logging
import os
import signal
import sys

from orchestrator.config import SchedulerConfigError, load_settings
from orchestrator.models import JobStatus
from orchestrator.registry import build_definitions
from orchestrator.scheduler import Scheduler


def _configure_logging():
    root = logging.getLogger("orchestrator")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)


def build_scheduler(settings, environ):
    return Scheduler(build_definitions(settings), dry_run=settings.dry_run, history_size=settings.history_size,
                     shutdown_grace_seconds=settings.shutdown_grace_seconds, child_output=settings.child_output,
                     environ=environ)


def aggregate_exit_code(results):
    """run-once policy: 0 when every enabled collector succeeded (or dry-run); 1 otherwise."""
    ok = {JobStatus.SUCCESS, JobStatus.DRY_RUN}
    return 0 if all(result.status in ok for result in results) else 1


def main(argv=None, *, environ=None, out=None, err=None):
    environ = os.environ if environ is None else environ
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m orchestrator.cli", description="MIAS scheduler.")
    parser.add_argument("command", choices=("run", "run-once", "status-config"))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return 2 if exit_.code else 0
    try:
        settings = load_settings(environ)
    except SchedulerConfigError as error:
        print(f"invalid scheduler configuration: {error}", file=err)
        return 2
    if args.command == "status-config":
        definitions = build_definitions(settings)
        view = settings.safe_view()
        view["registry"] = {d.name: dict(module=d.argv[2], args=list(d.argv[3:])) for d in definitions}
        print(json.dumps(view, indent=2, sort_keys=True), file=out)
        return 0
    if not settings.enabled:
        print("scheduler disabled: set MIAS_SCHEDULER_ENABLED=true to run collectors", file=err)
        return 3
    _configure_logging()
    scheduler = build_scheduler(settings, environ)
    previous = {sig: signal.signal(sig, lambda *_: scheduler.request_stop()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if args.command == "run":
            scheduler.run()
            return 0
        results = scheduler.run_once()
        print(json.dumps([r.to_dict() for r in results], indent=2), file=out)
        return aggregate_exit_code(results)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
