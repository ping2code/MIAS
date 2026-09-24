"""Phase 3 shutdown: graceful drain, forced termination, kill escalation, real signals, no orphan processes."""
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from orchestrator.job_runner import ROOT
from orchestrator.models import JobStatus
from orchestrator.scheduler import Scheduler
from tests.scheduler_helpers import alive, fixture, markers, wait_dead, wait_for


def pids(path):
    events = markers(path)
    return [m["pid"] for m in events if m["event"] == "start"] + [m["child"] for m in events if m["event"] == "grandchild"]


class InProcessShutdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.marker = os.path.join(self.tmp, "m.jsonl")

    def run_and_stop(self, scheduler, *, after):
        threading.Timer(after, scheduler.request_stop).start()
        started = time.monotonic()
        with self.assertLogs("orchestrator", "INFO") as logs:
            scheduler.run(max_seconds=30)
        return time.monotonic() - started, logs.output

    def test_graceful_shutdown_lets_active_runs_finish_within_grace(self):
        s = Scheduler([fixture("news", "--sleep", "1.0", "--marker", self.marker, interval=60, timeout=10)],
                      shutdown_grace_seconds=5, tick_seconds=0.05)
        elapsed, logs = self.run_and_stop(s, after=0.3)
        self.assertLess(elapsed, 4)
        self.assertEqual([r.status for r in s.history["news"]], [JobStatus.SUCCESS])
        self.assertEqual([m["event"] for m in markers(self.marker)], ["start", "end"])
        self.assertTrue(any("event=scheduler_stopped" in line for line in logs))
        self.assertEqual((s.health()["running"], s.health()["stopping"]), (False, True))

    def test_forced_shutdown_cancels_and_leaves_no_orphans(self):
        s = Scheduler([fixture("sec", "--sleep", "30", "--grandchild", "--marker", self.marker, interval=60, timeout=50)],
                      shutdown_grace_seconds=0.3, tick_seconds=0.05)
        elapsed, _ = self.run_and_stop(s, after=0.5)
        self.assertLess(elapsed, 5)
        result = s.history["sec"][-1]
        self.assertEqual((result.status, result.error_summary), (JobStatus.CANCELLED, "terminated by scheduler shutdown"))
        self.assertTrue(wait_dead(pids(self.marker)), "child or grandchild survived shutdown")

    def test_forced_shutdown_escalates_to_kill(self):
        s = Scheduler([fixture("macro", "--sleep", "30", "--ignore-term", "--marker", self.marker, interval=60, timeout=50,
                               kill_grace=0.5)], shutdown_grace_seconds=0.2, tick_seconds=0.05)
        elapsed, _ = self.run_and_stop(s, after=0.5)
        self.assertLess(elapsed, 5)
        self.assertEqual((s.history["macro"][-1].status, s.history["macro"][-1].exit_code), (JobStatus.CANCELLED, -9))
        self.assertTrue(wait_dead(pids(self.marker)))

    def test_no_new_jobs_after_stop_and_shutdown_is_idempotent(self):
        s = Scheduler([fixture("fed", interval=0.2, timeout=0.1)], tick_seconds=0.05)
        self.run_and_stop(s, after=0.1)
        count = len(s.history["fed"])
        s.shutdown()
        time.sleep(0.5)
        self.assertEqual(len(s.history["fed"]), count)


class RealSignalTests(unittest.TestCase):
    """Separate scheduler OS processes receiving real signals."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())

    def start_app(self, *args):
        process = subprocess.Popen([sys.executable, "-m", "tests.scheduler_fixture_app", self.tmp, *args], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        ready = lambda: all(len(pids(os.path.join(self.tmp, f"{n}.jsonl"))) >= 2 for n in ("news", "fed"))
        self.assertTrue(wait_for(ready, timeout=20), "fixture children did not start")
        return process

    def children(self):
        return [p for n in ("news", "fed") for p in pids(os.path.join(self.tmp, f"{n}.jsonl"))]

    def test_sigterm_and_sigint_stop_cleanly_without_orphans(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                for path in os.listdir(self.tmp):
                    os.remove(os.path.join(self.tmp, path))
                process = self.start_app("30")
                process.send_signal(sig)
                out, err = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, err[-500:])
                self.assertIn("event=scheduler_stopped", err)
                self.assertIn("'news': ['cancelled']", out)
                self.assertTrue(wait_dead(self.children()), "orphan child processes remain")

    def test_sigterm_escalates_when_children_ignore_term(self):
        process = self.start_app("30", "--ignore-term")
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, err[-500:])
        self.assertTrue(wait_dead(self.children()))

    def test_killed_scheduler_does_not_leave_orphans(self):
        process = self.start_app("30")
        leaders = [m["pid"] for n in ("news", "fed") for m in markers(os.path.join(self.tmp, f"{n}.jsonl"))
                   if m["event"] == "start"]
        process.kill()  # SIGKILL: no handlers, no shutdown; the kernel delivers PR_SET_PDEATHSIG to each child.
        process.wait(timeout=10)
        self.assertTrue(wait_dead(leaders, timeout=10), "children survived a killed scheduler")

    def test_real_cli_dry_run_stops_on_sigterm(self):
        env = dict(PATH=os.environ["PATH"], HOME=os.environ.get("HOME", "/tmp"), MIAS_SCHEDULER_ENABLED="true",
                   MIAS_SCHEDULER_DRY_RUN="true", PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen([sys.executable, "-m", "orchestrator.cli", "run"], cwd=ROOT, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        time.sleep(1.5)  # The news collector (offset 0) is due immediately and only logged: dry-run.
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, err[-500:])
        self.assertIn("event=job_dry_run collector=news", err)
        self.assertIn("event=scheduler_stopped", err)
        self.assertNotIn("event=job_started", err)


if __name__ == "__main__":
    unittest.main()
