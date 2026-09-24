"""Phase 3 job runner: real isolated child processes, exit codes, timeouts, kill escalation, environment allowlist."""
import json
import os
import tempfile
import time
import unittest

from orchestrator.job_runner import ROOT, child_environment, run_job
from orchestrator.models import JobDefinition, JobStatus
from tests.scheduler_helpers import alive, fixture, markers, wait_dead

SECRET = "sk-runner-canary-secret-000000000000"


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.marker = os.path.join(self.tmp, "marker.jsonl")

    def test_success_result_is_structured_metadata_only(self):
        result = run_job(fixture("news", "--marker", self.marker))
        self.assertEqual((result.status, result.exit_code, result.error_summary), (JobStatus.SUCCESS, 0, None))
        data = result.to_dict()
        self.assertEqual(set(data), {"collector", "run_id", "status", "scheduled_at", "started_at", "finished_at",
                                     "duration_seconds", "exit_code", "error_summary"})
        self.assertEqual((data["collector"], data["status"]), ("news", "success"))
        self.assertGreaterEqual(data["duration_seconds"], 0)
        self.assertEqual([m["event"] for m in markers(self.marker)], ["start", "end"])
        self.assertNotEqual(markers(self.marker)[0]["pid"], os.getpid())  # A separate OS process.

    def test_nonzero_exit_is_failed(self):
        result = run_job(fixture("fed", "--exit", "3"))
        self.assertEqual((result.status, result.exit_code, result.error_summary), (JobStatus.FAILED, 3, "exit code 3"))

    def test_timeout_terminates_the_child(self):
        started = time.monotonic()
        result = run_job(fixture("sec", "--sleep", "30", "--marker", self.marker, timeout=0.5))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((result.status, result.error_summary), (JobStatus.TIMED_OUT, "exceeded timeout of 0.5s"))
        self.assertLess(result.exit_code, 0)  # Ended by SIGTERM.
        self.assertTrue(wait_dead([markers(self.marker)[0]["pid"]]))

    def test_timeout_escalates_to_kill_when_term_is_ignored(self):
        started = time.monotonic()
        result = run_job(fixture("macro", "--sleep", "30", "--ignore-term", "--marker", self.marker, timeout=0.5,
                                 kill_grace=0.5))
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual((result.status, result.exit_code), (JobStatus.TIMED_OUT, -9))  # SIGKILL after the grace period.

    def test_timeout_kills_the_whole_process_group(self):
        result = run_job(fixture("treasury", "--sleep", "30", "--grandchild", "--marker", self.marker, timeout=1.0))
        self.assertEqual(result.status, JobStatus.TIMED_OUT)
        grandchild = next(m["child"] for m in markers(self.marker) if m["event"] == "grandchild")
        self.assertTrue(wait_dead([grandchild]), "grandchild survived the timeout")

    def test_start_failure_is_recorded_not_raised(self):
        result = run_job(JobDefinition(name="geopolitical", argv=("/nonexistent/python-mias",), timeout_seconds=1))
        self.assertEqual((result.status, result.error_summary), (JobStatus.FAILED, "failed to start (FileNotFoundError)"))

    def test_environment_is_allowlisted(self):
        environ = {"PATH": os.environ["PATH"], "HOME": "/tmp", "MIAS_SCHEDULER_ENABLED": "true", "REDIS_PORT": "1",
                   "DATABASE_URL": "postgresql://u@127.0.0.1:1/mias_test_x", "TELEGRAM_BOT_TOKEN": "x",
                   "AWS_SECRET_ACCESS_KEY": SECRET, "SSH_AUTH_SOCK": "/tmp/agent", "RANDOM_VAR": "1", "PYTHONPATH": "/evil"}
        env_out = os.path.join(self.tmp, "env.json")
        result = run_job(fixture("news", "--env-out", env_out), environ=environ)
        self.assertEqual(result.status, JobStatus.SUCCESS)
        seen = json.load(open(env_out))
        self.assertTrue({"PATH", "HOME", "MIAS_SCHEDULER_ENABLED", "REDIS_PORT", "DATABASE_URL", "TELEGRAM_BOT_TOKEN"}
                        <= set(seen["keys"]))
        self.assertFalse({"AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "RANDOM_VAR"} & set(seen["keys"]))
        self.assertEqual(seen["pythonpath"], str(ROOT))  # Set by the runner, never inherited.
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", child_environment({"AWS_SECRET_ACCESS_KEY": SECRET}))
        self.assertNotIn(SECRET, json.dumps(result.to_dict()))

    def test_child_output_is_discarded_by_default(self):
        definition = JobDefinition(name="news", argv=(os.sys.executable, "-c", f"print('{SECRET}')"), timeout_seconds=10)
        result = run_job(definition)
        self.assertEqual(result.status, JobStatus.SUCCESS)
        self.assertNotIn(SECRET, json.dumps(result.to_dict()))


if __name__ == "__main__":
    unittest.main()
