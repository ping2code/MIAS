"""Phase 3 scheduler loop: due/not-due, cadence without drift, offsets, overlap, concurrency, isolation, history, health."""
from datetime import datetime, timedelta, timezone
import json
import os
import sys
import tempfile
import time
import unittest

from orchestrator import cli
from orchestrator.models import JobDefinition, JobStatus
from orchestrator.scheduler import Scheduler
from tests.scheduler_helpers import fixture, markers, wait_for

SECRET = "sk-loop-canary-secret-000000000000000"


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.base = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def wall(self):
        return self.base + timedelta(seconds=self.now - 1000.0)


def dry(name, interval, offset=0.0, enabled=True):
    return JobDefinition(name=name, argv=("unused",), interval_seconds=interval, timeout_seconds=interval / 2,
                         start_offset_seconds=offset, enabled=enabled)


class CadenceTests(unittest.TestCase):
    """Deterministic: fake monotonic clock, dry-run (no processes)."""

    def scheduler(self, definitions, **kwargs):
        clock = FakeClock()
        return Scheduler(definitions, dry_run=True, clock=clock, wall=clock.wall, **kwargs), clock

    def fired(self, scheduler, name):
        return [r for r in scheduler.history[name] if r.status is JobStatus.DRY_RUN]

    def test_due_runs_not_due_does_not_and_disabled_never_runs(self):
        s, clock = self.scheduler([dry("news", 60), dry("sec", 60, offset=30), dry("fed", 60, enabled=False)])
        s.start()
        s.step()
        self.assertEqual((len(self.fired(s, "news")), len(self.fired(s, "sec"))), (1, 0))
        clock.now += 29.9
        s.step()
        self.assertEqual(len(self.fired(s, "sec")), 0)  # Not yet due.
        clock.now += 0.1
        s.step()
        self.assertEqual(len(self.fired(s, "sec")), 1)
        clock.now += 3600
        s.step()
        self.assertEqual([r.status for r in s.history["fed"]], [JobStatus.DISABLED])  # Recorded once, never run.
        self.assertNotIn("fed", s.next_due)

    def test_cadence_does_not_drift_with_step_timing(self):
        s, clock = self.scheduler([dry("news", 10)])
        s.start()
        fire_times = []
        for jitter in [0.0, 3.7, 6.4, 9.99, 10.02, 3.3, 7.1, 9.95, 0.3, 12.0] * 3:
            clock.now += jitter
            before = len(s.history["news"])
            s.step()
            if len(s.history["news"]) > before:
                fire_times.append(clock.now)
        dues = [1000.0 + 10 * k for k in range(len(fire_times))]
        self.assertTrue(all(0 <= fired - due < 12.01 for fired, due in zip(fire_times, dues)))
        self.assertEqual(s.next_due["news"], 1000.0 + 10 * len(fire_times))  # Anchored to the schedule, not to "now".

    def test_missed_slots_collapse_without_burst(self):
        s, clock = self.scheduler([dry("macro", 10)])
        s.start()
        s.step()
        clock.now += 55  # The loop stalled for five intervals.
        s.step()
        self.assertEqual(len(self.fired(s, "macro")), 2)  # One run, not a burst of five.
        self.assertEqual((s.missed_slots["macro"], s.next_due["macro"]), (4, 1060.0))

    def test_startup_offsets_order_first_runs(self):
        names = ["news", "fed", "sec", "macro", "treasury", "geopolitical"]
        s, clock = self.scheduler([dry(n, 600, offset=5 * i) for i, n in enumerate(names)])
        s.start()
        order = []
        for _ in range(30):
            s.step()
            order += [n for n in names if len(self.fired(s, n)) == 1 and n not in order]
            clock.now += 1
        self.assertEqual(order, names)

    def test_dry_run_executes_nothing(self):
        s, clock = self.scheduler([JobDefinition(name="news", argv=("/nonexistent/should-never-run",), interval_seconds=10,
                                                 timeout_seconds=5)])
        s.start()
        for _ in range(3):
            s.step()
            clock.now += 10
        self.assertEqual([r.status for r in s.history["news"]], [JobStatus.DRY_RUN] * 3)
        self.assertEqual(s.running, {})

    def test_bounded_history_and_health_snapshot(self):
        s, clock = self.scheduler([dry("news", 10), dry("sec", 10, enabled=False)], history_size=3)
        s.start()
        for _ in range(7):
            s.step()
            clock.now += 10
        self.assertEqual(len(s.history["news"]), 3)
        health = s.health()
        self.assertEqual((health["running"], health["dry_run"], health["active_runs"]), (True, True, 0))
        news = health["collectors"]["news"]
        self.assertEqual((news["enabled"], news["last_status"], news["currently_running"], news["history_size"]),
                         (True, "dry_run", False, 3))
        self.assertEqual(news["next_run_at"], clock.wall().isoformat(timespec="seconds"))
        self.assertEqual((health["collectors"]["sec"]["enabled"], health["collectors"]["sec"]["next_run_at"]), (False, None))
        json.dumps(health)  # Plain data.


class RealProcessLoopTests(unittest.TestCase):
    """Real fixture child processes with short intervals (no collectors, no network)."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())

    def marker(self, name):
        return os.path.join(self.tmp, f"{name}.jsonl")

    def test_overlap_is_skipped_never_duplicated(self):
        s = Scheduler([fixture("sec", "--sleep", "1.2", "--marker", self.marker("sec"), interval=0.3, timeout=5)],
                      tick_seconds=0.05)
        with self.assertLogs("orchestrator", "INFO") as logs:
            s.run(max_seconds=2.0)
        statuses = [r.status for r in s.history["sec"]]
        self.assertIn(JobStatus.SKIPPED_OVERLAP, statuses)
        starts = [m for m in markers(self.marker("sec")) if m["event"] == "start"]
        ends = [m for m in markers(self.marker("sec")) if m["event"] == "end"]
        for first, second in zip(starts[1:], ends):  # A new run starts only after the previous one ended.
            self.assertGreaterEqual(first["t"], second["t"])
        self.assertTrue(any("event=job_skipped_overlap collector=sec" in line for line in logs.output))

    def test_different_collectors_run_concurrently(self):
        names = ["news", "fed", "sec"]
        s = Scheduler([fixture(n, "--sleep", "1.0", "--marker", self.marker(n), interval=30, timeout=5) for n in names],
                      tick_seconds=0.05)
        with self.assertLogs("orchestrator", "INFO"):
            s.run(max_seconds=0.5)  # Shutdown waits (grace) for the three in-flight runs.
        spans = {n: (markers(self.marker(n))[0]["t"], markers(self.marker(n))[-1]["t"]) for n in names}
        self.assertLess(max(start for start, _ in spans.values()), min(end for _, end in spans.values()))
        self.assertEqual({n: s.history[n][-1].status for n in names}, dict.fromkeys(names, JobStatus.SUCCESS))

    def test_failure_isolation_matrix(self):
        """Macro fails -> Treasury runs; Treasury hangs -> only Treasury times out; News fails -> Fed/SEC continue."""
        definitions = [fixture("macro", "--exit", "1", interval=0.4, timeout=5),
                       fixture("treasury", "--sleep", "30", "--marker", self.marker("treasury"), interval=30, timeout=0.8,
                               kill_grace=0.5),
                       fixture("news", "--exit", "2", interval=0.4, timeout=5),
                       fixture("fed", "--marker", self.marker("fed"), interval=0.4, timeout=5),
                       fixture("sec", "--marker", self.marker("sec"), interval=0.4, timeout=5)]
        s = Scheduler(definitions, tick_seconds=0.05)
        with self.assertLogs("orchestrator", "INFO") as logs:
            s.run(max_seconds=2.5)
        statuses = {n: [r.status for r in s.history[n]] for n in s.history}
        self.assertTrue(len(statuses["macro"]) >= 3 and set(statuses["macro"]) == {JobStatus.FAILED})
        self.assertEqual(statuses["treasury"][0], JobStatus.TIMED_OUT)
        self.assertTrue(set(statuses["news"]) == {JobStatus.FAILED} and len(statuses["news"]) >= 3)
        for name in ("fed", "sec"):  # Healthy collectors kept their cadence throughout.
            self.assertGreaterEqual(statuses[name].count(JobStatus.SUCCESS), 4)
        self.assertTrue(any("event=job_timeout collector=treasury" in line for line in logs.output))
        self.assertTrue(s.stopped)

    def test_logs_never_contain_secrets_environment_or_output(self):
        environ = dict(os.environ, OPENAI_API_KEY=SECRET, TELEGRAM_BOT_TOKEN=SECRET)
        definitions = [JobDefinition(name="news", argv=(sys.executable, "-c", f"import sys; print('{SECRET}'); "
                                                         "print('boom', file=sys.stderr); sys.exit(4)"),
                                     interval_seconds=30, timeout_seconds=5)]
        s = Scheduler(definitions, environ=environ, tick_seconds=0.05)
        with self.assertLogs("orchestrator", "INFO") as logs:
            s.run(max_seconds=0.5)
        text = "\n".join(logs.output) + json.dumps([r.to_dict() for r in s.history["news"]]) + json.dumps(s.health())
        self.assertNotIn(SECRET, text)
        self.assertNotIn("boom", text)
        for event in ("scheduler_started", "job_scheduled", "job_started", "job_failed", "scheduler_stopped"):
            self.assertTrue(any(f"event={event}" in line for line in logs.output), event)
        self.assertIn("exit_code=4", "\n".join(logs.output))

    def test_run_once_results_and_aggregate_policy(self):
        ok = Scheduler([fixture("news"), fixture("fed"), fixture("sec", enabled=False)])
        with self.assertLogs("orchestrator", "INFO"):
            results = ok.run_once()
        self.assertEqual([(r.collector, r.status) for r in results], [("news", JobStatus.SUCCESS), ("fed", JobStatus.SUCCESS)])
        self.assertEqual(cli.aggregate_exit_code(results), 0)
        self.assertEqual([r.status for r in ok.history["sec"]], [JobStatus.DISABLED])
        mixed = Scheduler([fixture("news", "--exit", "1"), fixture("fed", "--sleep", "30", timeout=0.5, kill_grace=0.5),
                           fixture("sec")])
        started = time.monotonic()
        with self.assertLogs("orchestrator", "INFO"):
            results = mixed.run_once()
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual({r.collector: r.status for r in results},
                         dict(news=JobStatus.FAILED, fed=JobStatus.TIMED_OUT, sec=JobStatus.SUCCESS))
        self.assertEqual(cli.aggregate_exit_code(results), 1)

    def test_run_once_dry_run_via_cli_executes_nothing(self):
        from io import StringIO
        out = StringIO()
        env = {"MIAS_SCHEDULER_ENABLED": "true", "MIAS_SCHEDULER_DRY_RUN": "true", "SEC_SCHEDULE_ENABLED": "false"}
        with self.assertLogs("orchestrator", "INFO"):
            code = cli.main(["run-once"], environ=env, out=out, err=StringIO())
        results = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual([(r["collector"], r["status"]) for r in results],
                         [(n, "dry_run") for n in ("news", "fed", "macro", "treasury", "geopolitical")])
        self.assertTrue(all(r["started_at"] is None and r["exit_code"] is None for r in results))


if __name__ == "__main__":
    unittest.main()
