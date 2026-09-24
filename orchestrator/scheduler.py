"""MIAS scheduler loop: cadence without drift, no self-overlap, bounded timeouts, clean shutdown.

    python -m orchestrator.scheduler            # same as: python -m orchestrator.cli run

Model (single-threaded, non-blocking):

- Each enabled collector has a monotonic ``next_due``. It starts at
  ``start + start_offset``; after every firing it becomes ``previous due + interval``
  (never "now + interval"), so job runtime cannot cause drift. If the loop fell
  behind by more than one interval, missed slots are collapsed to the next future
  slot (no burst) and counted.
- A due collector that is still running is recorded as ``skipped_overlap``.
  Different collectors run concurrently, each in its own child process.
- Each child is polled every tick; timeouts terminate only that child's process group.
- Shutdown stops scheduling, waits a bounded grace for active runs, then terminates
  and finally kills what remains. No child outlives the scheduler.
- Run history is in memory only, bounded per collector.
"""
from collections import deque
from datetime import datetime, timezone
import logging
from time import monotonic, sleep
from uuid import uuid4

from orchestrator import health
from orchestrator.job_runner import ChildRun, start_failure
from orchestrator.models import JobResult, JobStatus

logger = logging.getLogger("orchestrator")


def log_event(event, **fields):
    """Structured key=value line with an allowlisted field set (never environment, argv or child output)."""
    allowed = ("collector", "run_id", "status", "scheduled_at", "started_at", "finished_at", "duration_seconds",
               "exit_code", "error_summary", "pid", "enabled", "dry_run", "collectors", "active_runs", "missed_slots")
    parts = [f"event={event}"] + [f"{k}={fields[k]}" for k in allowed if fields.get(k) is not None]
    logger.info(" ".join(parts))


def _utc():
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(self, definitions, *, dry_run=False, history_size=50, shutdown_grace_seconds=30.0, child_output="discard",
                 environ=None, clock=monotonic, wall=_utc, tick_seconds=0.25):
        names = [d.name for d in definitions]
        if len(set(names)) != len(names):
            raise ValueError("Collector names must be unique")
        self.definitions = list(definitions)
        self.dry_run, self.shutdown_grace_seconds, self.child_output = dry_run, shutdown_grace_seconds, child_output
        self.environ, self.clock, self.wall, self.tick_seconds = environ, clock, wall, tick_seconds
        self.history = {d.name: deque(maxlen=history_size) for d in self.definitions}
        self.running, self.next_due = {}, {}
        self.started_at, self.stopped, self.stopping = None, False, False
        self.missed_slots = {d.name: 0 for d in self.definitions}

    # -- state helpers
    def _record(self, result):
        self.history[result.collector].append(result)
        event = {JobStatus.SUCCESS: "job_succeeded", JobStatus.FAILED: "job_failed", JobStatus.TIMED_OUT: "job_timeout",
                 JobStatus.SKIPPED_OVERLAP: "job_skipped_overlap", JobStatus.DRY_RUN: "job_dry_run",
                 JobStatus.CANCELLED: "job_cancelled", JobStatus.DISABLED: "job_disabled"}[result.status]
        log_event(event, **result.to_dict(), pid=result.pid)

    def health(self):
        return health.snapshot(self)

    def request_stop(self):
        """Signal-safe: only sets a flag; the loop performs the bounded shutdown."""
        self.stopping = True

    # -- execution
    def _fire(self, definition, scheduled_mono):
        scheduled_at = self.wall()
        run_id = uuid4().hex
        log_event("job_scheduled", collector=definition.name, run_id=run_id, scheduled_at=scheduled_at.isoformat())
        if definition.name in self.running:
            self._record(JobResult(collector=definition.name, run_id=run_id, status=JobStatus.SKIPPED_OVERLAP,
                                   scheduled_at=scheduled_at.isoformat(timespec="milliseconds"),
                                   error_summary="previous run still active"))
            return
        if self.dry_run:
            self._record(JobResult(collector=definition.name, run_id=run_id, status=JobStatus.DRY_RUN,
                                   scheduled_at=scheduled_at.isoformat(timespec="milliseconds")))
            return
        try:
            run = ChildRun(definition, run_id=run_id, scheduled_at=scheduled_at, environ=self.environ,
                           output=self.child_output, clock=self.clock, wall=self.wall)
        except OSError as error:
            self._record(start_failure(definition, error, run_id=run_id, scheduled_at=scheduled_at, wall=self.wall))
            return
        self.running[definition.name] = run
        log_event("job_started", collector=definition.name, run_id=run_id, started_at=run.started_at.isoformat(),
                  pid=run.pid)

    def _reap(self):
        for name, run in list(self.running.items()):
            result = run.poll()
            if result is not None:
                del self.running[name]
                self._record(result)

    def _advance(self, definition, now):
        due = self.next_due[definition.name] + definition.interval_seconds
        if due <= now:  # The loop fell behind: collapse missed slots to the next future slot (no burst).
            missed = int((now - due) // definition.interval_seconds) + 1
            due += missed * definition.interval_seconds
            self.missed_slots[definition.name] += missed
            log_event("job_missed_slots", collector=definition.name, missed_slots=missed)
        self.next_due[definition.name] = due

    def start(self):
        self.started_at, start = self.wall(), self.clock()
        for definition in self.definitions:
            if definition.enabled:
                self.next_due[definition.name] = start + definition.start_offset_seconds
            else:
                self._record(JobResult(collector=definition.name, run_id=uuid4().hex, status=JobStatus.DISABLED,
                                       scheduled_at=self.started_at.isoformat(timespec="milliseconds")))
        log_event("scheduler_started", dry_run=self.dry_run,
                  collectors=",".join(d.name for d in self.definitions if d.enabled))

    def step(self):
        """One loop iteration: reap children, fire due collectors. Returns seconds until the next event."""
        self._reap()
        now = self.clock()
        if not self.stopping:
            for definition in self.definitions:
                if definition.enabled and now >= self.next_due[definition.name]:
                    self._fire(definition, self.next_due[definition.name])
                    self._advance(definition, now)
        upcoming = [due - self.clock() for name, due in self.next_due.items()] if not self.stopping else []
        return max(0.0, min([self.tick_seconds] + upcoming))

    def run(self, *, max_seconds=None):
        """Scheduling loop until ``request_stop`` (e.g. SIGINT/SIGTERM) or ``max_seconds``; then bounded shutdown."""
        self.start()
        deadline = None if max_seconds is None else self.clock() + max_seconds
        try:
            while not self.stopping:
                if deadline is not None and self.clock() >= deadline:
                    break
                sleep(self.step())
        finally:
            self.shutdown()
        return self.health()

    def shutdown(self):
        """Stop scheduling; wait a bounded grace for active runs; then terminate, then kill; no orphans."""
        self.stopping = True
        grace_until = self.clock() + self.shutdown_grace_seconds
        while self.running and self.clock() < grace_until:
            self._reap()
            sleep(min(self.tick_seconds, 0.1))
        for run in self.running.values():
            run.terminate(cancelled=True)
        while self.running:
            self._reap()
            sleep(0.05)
        if not self.stopped:
            self.stopped = True
            log_event("scheduler_stopped", active_runs=0)

    def run_once(self):
        """Start every enabled collector once (concurrently, no offsets), wait for all; return their results."""
        self.started_at = self.wall()
        log_event("scheduler_started", dry_run=self.dry_run,
                  collectors=",".join(d.name for d in self.definitions if d.enabled))
        results = []
        for definition in self.definitions:
            if not definition.enabled:
                self._record(JobResult(collector=definition.name, run_id=uuid4().hex, status=JobStatus.DISABLED,
                                       scheduled_at=self.started_at.isoformat(timespec="milliseconds")))
                continue
            self._fire(definition, self.clock())
        try:
            while self.running and not self.stopping:
                self._reap()
                sleep(0.05)
        finally:
            self.shutdown()
        for definition in self.definitions:
            if definition.enabled and self.history[definition.name]:
                results.append(self.history[definition.name][-1])
        return results


def main(argv=None):
    from orchestrator.cli import main as cli_main
    return cli_main(["run"] if argv is None else argv)


if __name__ == "__main__":
    import sys
    sys.exit(main())
