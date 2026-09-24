"""Phase 4C scheduler registration of the technical runner: disabled by default, configurable, isolated, safe env."""
import json
import os
import sys
import tempfile
import unittest

from orchestrator import registry
from orchestrator.config import SchedulerConfigError, load_settings
from orchestrator.job_runner import child_environment
from orchestrator.models import JobDefinition, JobStatus, OverlapPolicy
from orchestrator.scheduler import Scheduler
from tests.scheduler_helpers import fixture, markers
from tests.market_data_fakes import TEST_KEY

SECRET = "aws-canary-secret-not-for-children-0000"


def technical_definition(settings_env=None, **overrides):
    definitions = {d.name: d for d in registry.build_definitions(load_settings(settings_env or {}))}
    base = definitions["technical"]
    values = dict(name="technical", argv=(sys.executable, "-m", "tests.technical_runner_shim", *base.argv[3:]),
                  enabled=base.enabled, interval_seconds=base.interval_seconds, timeout_seconds=base.timeout_seconds,
                  start_offset_seconds=base.start_offset_seconds, kill_grace_seconds=base.kill_grace_seconds)
    values.update(overrides)
    return JobDefinition(**values)


class RegistrationTests(unittest.TestCase):
    def test_registered_and_disabled_by_default(self):
        definition = {d.name: d for d in registry.build_definitions(load_settings({}), python="/py")}["technical"]
        self.assertEqual(definition.argv, ("/py", "-m", "technical.runner"))
        self.assertFalse(definition.enabled)
        self.assertIs(definition.overlap_policy, OverlapPolicy.SKIP)
        self.assertEqual((definition.interval_seconds, definition.timeout_seconds, definition.start_offset_seconds),
                         (1800, 900, 30))

    def test_enabled_and_configurable(self):
        settings = load_settings(dict(TECHNICAL_SCHEDULE_ENABLED="true", TECHNICAL_INTERVAL_SECONDS="3600",
                                      TECHNICAL_TIMEOUT_SECONDS="1200", TECHNICAL_START_OFFSET_SECONDS="45"))
        definition = {d.name: d for d in registry.build_definitions(settings)}["technical"]
        self.assertEqual((definition.enabled, definition.interval_seconds, definition.timeout_seconds,
                          definition.start_offset_seconds), (True, 3600, 1200, 45))
        for env in (dict(TECHNICAL_SCHEDULE_ENABLED="yes"), dict(TECHNICAL_SCHEDULE_SEND_ALERTS="true"),
                    dict(TECHNICAL_INTERVAL_SECONDS="600", TECHNICAL_TIMEOUT_SECONDS="600")):
            with self.subTest(env=env), self.assertRaises(SchedulerConfigError):
                load_settings(env)

    def test_other_collectors_unchanged(self):
        before = {d.name: d for d in registry.build_definitions(load_settings({}))}
        after = {d.name: d for d in registry.build_definitions(load_settings(dict(TECHNICAL_SCHEDULE_ENABLED="true")))}
        self.assertEqual({n: d for n, d in before.items() if n != "technical"},
                         {n: d for n, d in after.items() if n != "technical"})

    def test_child_environment_propagation(self):
        environ = dict(PATH="/bin", MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY,
                       TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="true", DATABASE_URL="postgresql://x/y",
                       AWS_SECRET_ACCESS_KEY=SECRET, SSH_AUTH_SOCK="/tmp/agent")
        child = child_environment(environ)
        self.assertTrue({"MARKET_DATA_PROVIDER", "MARKET_DATA_API_KEY", "TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED",
                         "DATABASE_URL"} <= set(child))
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", child)
        self.assertNotIn("SSH_AUTH_SOCK", child)


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())

    def marker(self, name):
        return os.path.join(self.tmp, f"{name}.jsonl")

    def test_disabled_technical_never_runs(self):
        s = Scheduler([fixture("technical", "--marker", self.marker("technical"), enabled=False),
                       fixture("news", "--marker", self.marker("news"))])
        with self.assertLogs("orchestrator", "INFO"):
            results = s.run_once()
        self.assertEqual([r.collector for r in results], ["news"])
        self.assertEqual(markers(self.marker("technical")), [])
        self.assertEqual([r.status for r in s.history["technical"]], [JobStatus.DISABLED])

    def test_overlap_skipped(self):
        s = Scheduler([fixture("technical", "--sleep", "1.2", "--marker", self.marker("technical"), interval=0.3,
                               timeout=5)], tick_seconds=0.05)
        with self.assertLogs("orchestrator", "INFO") as logs:
            s.run(max_seconds=2.0)
        self.assertIn(JobStatus.SKIPPED_OVERLAP, [r.status for r in s.history["technical"]])
        self.assertTrue(any("event=job_skipped_overlap collector=technical" in line for line in logs.output))

    def test_timeout_isolated_from_other_collectors(self):
        s = Scheduler([fixture("technical", "--sleep", "30", interval=30, timeout=0.8, kill_grace=0.5),
                       fixture("news", "--marker", self.marker("news"), interval=0.4, timeout=5),
                       fixture("fed", "--marker", self.marker("fed"), interval=0.4, timeout=5)], tick_seconds=0.05)
        with self.assertLogs("orchestrator", "INFO"):
            s.run(max_seconds=2.5)
        self.assertEqual(s.history["technical"][0].status, JobStatus.TIMED_OUT)
        for name in ("news", "fed"):
            self.assertGreaterEqual([r.status for r in s.history[name]].count(JobStatus.SUCCESS), 4)

    def test_real_runner_job_success_failure_and_no_key_in_logs(self):
        reports = self.enterContext(tempfile.TemporaryDirectory())
        base = dict(PATH=os.environ["PATH"], HOME=os.environ.get("HOME", "/tmp"), PYTHONDONTWRITEBYTECODE="1",
                    MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY, MARKET_DATA_DELAY_SECONDS="0",
                    MIAS_TEST_TECHNICAL_NOW="2026-09-23T20:00:00-04:00", MIAS_TEST_SHIM_REPORT=reports,
                    AWS_SECRET_ACCESS_KEY=SECRET)
        definition = technical_definition(dict(TECHNICAL_SCHEDULE_ENABLED="true"), timeout_seconds=120)
        ok = Scheduler([definition, fixture("news", "--marker", self.marker("news"))], environ=base,
                       shutdown_grace_seconds=60)
        with self.assertLogs("orchestrator", "INFO") as logs:
            results = {r.collector: r for r in ok.run_once()}
        self.assertEqual((results["technical"].status, results["technical"].exit_code), (JobStatus.SUCCESS, 0))
        self.assertEqual(results["news"].status, JobStatus.SUCCESS)
        with open(os.path.join(reports, "technical.runner.json")) as handle:
            report = json.load(handle)
        self.assertEqual((report["telegram_attempts"], report["openai_attempts"], report["network_attempts"]), (0, 0, 0))
        failing = Scheduler([definition], environ=dict(base, MIAS_TEST_TECHNICAL_FAIL="auth"), shutdown_grace_seconds=60)
        with self.assertLogs("orchestrator", "INFO") as failed_logs:
            result = failing.run_once()[0]
        self.assertEqual((result.status, result.exit_code), (JobStatus.FAILED, 1))
        text = "\n".join(logs.output + failed_logs.output) + json.dumps([r.to_dict() for r in failing.history["technical"]])
        self.assertNotIn(TEST_KEY, text)
        self.assertNotIn(SECRET, text)


if __name__ == "__main__":
    unittest.main()
