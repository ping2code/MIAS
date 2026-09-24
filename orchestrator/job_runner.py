"""Isolated child-process execution for one collector run (no shell, bounded timeout, no leaked children).

Each run is a separate OS process started in its own session/process group, so
timeouts and shutdown can terminate the whole group. On Linux the child also asks
the kernel to send it SIGTERM if the scheduler dies (``PR_SET_PDEATHSIG``), so a
killed scheduler does not leave orphans. Children receive an allowlisted
environment; nothing about the environment, command-line secrets or child output
is logged or stored.
"""
import ctypes
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
from time import monotonic, sleep
from uuid import uuid4

from orchestrator.models import JobResult, JobStatus

ROOT = Path(__file__).resolve().parents[1]
ENV_EXACT = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
             "DATABASE_URL"}
# Configuration families the collectors read today (shared/config.py, persistence config, Telegram, OpenAI).
ENV_PREFIXES = ("MIAS_", "ALERT_", "DISPLAY_", "REDIS_", "DB_", "FED_", "MACRO_", "TREASURY_", "GEOPOLITICAL_", "SEC_",
                "NEWS_", "TELEGRAM_", "OPENAI_", "NEAR_DUPLICATE_", "HEADLINE_", "DEDUP_", "RSS_")


def child_environment(environ):
    """Only variables the collectors need; everything else (cloud credentials, agent sockets...) is dropped."""
    env = {k: v for k, v in environ.items() if k in ENV_EXACT or k.startswith(ENV_PREFIXES)}
    env["PYTHONPATH"] = str(ROOT)
    return env


def _die_with_parent():
    """Linux only: SIGTERM this child if the scheduler process dies. Best effort; never fails the start."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG = 1
    except Exception:
        pass


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="milliseconds") if value else None


class ChildRun:
    """One running collector process with its deadlines; ``poll`` advances timeout/kill escalation."""

    def __init__(self, definition, *, run_id=None, scheduled_at=None, environ=None, output="discard",
                 clock=monotonic, wall=_now):
        self.definition, self.clock, self.wall = definition, clock, wall
        self.run_id = run_id or uuid4().hex
        self.scheduled_at = scheduled_at or wall()
        self.started_at, self.started_mono = wall(), clock()
        self.timeout_at = self.started_mono + definition.timeout_seconds
        self.kill_at = None
        self.timed_out = self.cancelled = False
        stream = None if output == "inherit" else subprocess.DEVNULL
        self.process = subprocess.Popen(list(definition.argv), cwd=ROOT, env=child_environment(os.environ if environ is None else environ),
                                        stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True,
                                        preexec_fn=_die_with_parent if os.name == "posix" else None)
        self.pid = self.process.pid

    def _signal_group(self, sig):
        try:
            os.killpg(self.process.pid, sig)  # The child leads its own process group (start_new_session).
        except ProcessLookupError:
            pass

    def terminate(self, *, cancelled=False):
        """SIGTERM the process group now; SIGKILL after the grace period if it is still alive."""
        if self.kill_at is None:
            self.cancelled = self.cancelled or cancelled
            self._signal_group(signal.SIGTERM)
            self.kill_at = self.clock() + self.definition.kill_grace_seconds

    def poll(self):
        """Advance deadlines; return a JobResult once the process has exited, else None."""
        code = self.process.poll()
        if code is None:
            now = self.clock()
            if not self.timed_out and not self.cancelled and now >= self.timeout_at:
                self.timed_out = True
                self.terminate()
            elif self.kill_at is not None and now >= self.kill_at:
                self._signal_group(signal.SIGKILL)
                self.kill_at = float("inf")  # SIGKILL sent once; wait for the kernel to reap.
            return None
        # Group signals are only ever sent while the leader is alive (timeout/shutdown); after it has been reaped
        # its id could name an unrelated group, so a normally exiting run is never signalled.
        finished = self.wall()
        if self.cancelled:
            status, summary = JobStatus.CANCELLED, "terminated by scheduler shutdown"
        elif self.timed_out:
            status, summary = JobStatus.TIMED_OUT, f"exceeded timeout of {self.definition.timeout_seconds:g}s"
        elif code == 0:
            status, summary = JobStatus.SUCCESS, None
        else:
            status = JobStatus.FAILED
            summary = f"terminated by signal {-code}" if code < 0 else f"exit code {code}"
        return JobResult(collector=self.definition.name, run_id=self.run_id, status=status,
                         scheduled_at=_iso(self.scheduled_at), started_at=_iso(self.started_at), finished_at=_iso(finished),
                         duration_seconds=round(self.clock() - self.started_mono, 3), exit_code=code,
                         error_summary=summary, pid=self.pid)


def start_failure(definition, error, *, run_id=None, scheduled_at=None, wall=_now):
    """A run that could not start (e.g. missing interpreter): recorded as failed, scheduler unaffected."""
    now = wall()
    return JobResult(collector=definition.name, run_id=run_id or uuid4().hex, status=JobStatus.FAILED,
                     scheduled_at=_iso(scheduled_at or now), started_at=None, finished_at=_iso(now), duration_seconds=0.0,
                     exit_code=None, error_summary=f"failed to start ({type(error).__name__})")


def run_job(definition, *, environ=None, output="discard", poll_interval=0.05):
    """Run one collector to completion (bounded by its timeout and kill grace) and return its JobResult."""
    try:
        run = ChildRun(definition, environ=environ, output=output)
    except OSError as error:
        return start_failure(definition, error)
    while True:
        result = run.poll()
        if result is not None:
            return result
        sleep(poll_interval)
