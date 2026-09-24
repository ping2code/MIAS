"""Phase 3 controlled MIAS integration: the scheduler reaches all six real collector entry points.

Each registry command is launched exactly as the scheduler would, prefixed by
``tests.scheduler_collector_shim``. The shim blocks every network connection
(feeds, Redis, PostgreSQL, Telegram, OpenAI), disables ``.env`` loading, stubs
Telegram and OpenAI, and reports which collection function actually ran. This
proves the wiring, not live sources (Phase 2S covers those).

Entry-point exit contracts with the network unavailable are unchanged. News, Fed,
SEC and geopolitical exit 0 after a cycle. Macro and Treasury exit 1 whenever a
fetch failed, so the scheduler records them as failed.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from orchestrator import registry
from orchestrator.config import load_settings
from orchestrator.models import JobDefinition, JobStatus
from orchestrator.scheduler import Scheduler
from tests.test_real_staging import closed_port

MODULES = dict(news="collector.multi_source_collector", fed="collector.fed_collector", sec="collector.sec_collector",
               macro="collector.macro_collector", treasury="collector.treasury_collector",
               geopolitical="orchestrator.entrypoints")
ENTRY = dict(news={"collect_all_sources", "read_feed"}, fed={"collect_fed_events"},
             sec={"collect_sec_filings", "process_sec_filings"}, macro={"collect_macro_events"},
             treasury={"collect_treasury_events"}, geopolitical={"collect_geopolitical_events"})
EXPECTED_STATUS = dict(news=JobStatus.SUCCESS, fed=JobStatus.SUCCESS, sec=JobStatus.SUCCESS, macro=JobStatus.FAILED,
                       treasury=JobStatus.FAILED, geopolitical=JobStatus.SUCCESS)


def shimmed(definition, **overrides):
    """The registry command, launched through the network-blocking test shim (same module and arguments)."""
    argv = (sys.executable, "-m", "tests.scheduler_collector_shim", *definition.argv[1:])
    values = dict(name=definition.name, argv=argv, enabled=definition.enabled, interval_seconds=definition.interval_seconds,
                  timeout_seconds=definition.timeout_seconds, start_offset_seconds=definition.start_offset_seconds,
                  kill_grace_seconds=definition.kill_grace_seconds)
    values.update(overrides)
    return JobDefinition(**values)


class SchedulerCollectorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.reports = self.enterContext(tempfile.TemporaryDirectory())
        holder, port = closed_port()
        self.addCleanup(holder.close)
        self.environ = dict(PATH=os.environ["PATH"], HOME=os.environ.get("HOME", "/tmp"), PYTHONDONTWRITEBYTECODE="1",
                            REDIS_HOST="127.0.0.1", REDIS_PORT=str(port), MIAS_TEST_SHIM_REPORT=self.reports,
                            TELEGRAM_BOT_TOKEN="123456789:MIASCANARYschedulerTESTONLY0000000000",
                            OPENAI_API_KEY="sk-mias-canary-scheduler-test-only-00000000")
        self.definitions = [shimmed(d, timeout_seconds=120) for d in registry.build_definitions(load_settings({}))]

    def report(self, name):
        with open(os.path.join(self.reports, f"{MODULES[name]}.json")) as handle:
            return json.load(handle)

    def check_reports(self):
        for name in MODULES:
            with self.subTest(collector=name):
                report = self.report(name)
                self.assertTrue(ENTRY[name] <= set(report["entry_functions"]), report["entry_functions"])
                self.assertEqual((report["telegram_attempts"], report["openai_attempts"]), (0, 0))
                self.assertGreater(report["network_attempts"], 0)  # Collectors really tried; every connection was blocked.
                self.assertEqual(report["argv"][:2], ["-m", MODULES[name]])

    def test_run_once_reaches_all_six_collectors(self):
        scheduler = Scheduler(self.definitions, environ=self.environ, shutdown_grace_seconds=60)
        with self.assertLogs("orchestrator", "INFO") as logs:
            results = scheduler.run_once()
        self.assertEqual({r.collector: r.status for r in results}, EXPECTED_STATUS)
        self.assertEqual({r.collector: r.exit_code for r in results},
                         {n: (1 if n in ("macro", "treasury") else 0) for n in MODULES})
        self.check_reports()
        text = "\n".join(logs.output)
        self.assertNotIn(self.environ["TELEGRAM_BOT_TOKEN"], text)
        self.assertNotIn(self.environ["OPENAI_API_KEY"], text)
        self.assertEqual(text.count("event=job_started"), 6)

    def test_scheduled_loop_starts_each_collector_once_with_offsets(self):
        definitions = [shimmed(d, interval_seconds=600, timeout_seconds=120, start_offset_seconds=0.2 * i)
                       for i, d in enumerate(self.definitions)]
        scheduler = Scheduler(definitions, environ=self.environ, shutdown_grace_seconds=120, tick_seconds=0.05)
        with self.assertLogs("orchestrator", "INFO"):
            scheduler.run(max_seconds=1.5)  # All six fire (offsets 0..1.0 s); shutdown waits for them to finish.
        self.assertEqual({n: [r.status for r in scheduler.history[n]] for n in MODULES},
                         {n: [s] for n, s in EXPECTED_STATUS.items()})
        starts = [scheduler.history[n][0].started_at for n in MODULES]
        self.assertEqual(starts, sorted(starts))  # Deterministic start-up order from the offsets.
        self.check_reports()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL") and os.environ.get("MIAS_PHASE2J_REDIS_URL"),
                     "Disposable TEST_DATABASE_URL and loopback Redis opt-in required")
class SchedulerIsolatedServicesIntegrationTests(unittest.TestCase):
    """The same six commands with isolated Redis/PostgreSQL reachable, persistence on and outside network still blocked.

    Only loopback connections to the disposable services are allowed; the shim blocks everything else.
    """

    def test_run_once_with_isolated_services(self):
        from urllib.parse import urlsplit
        import redis
        from tests import staging_rollout
        redis_url = os.environ["MIAS_PHASE2J_REDIS_URL"]
        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
        self.addCleanup(client.close)
        staging_rollout.verify_redis(client, redis_url)
        self.addCleanup(lambda: [client.delete(k) for k in client.scan_iter(match="mias:*")])
        reports = self.enterContext(tempfile.TemporaryDirectory())
        environ = dict(PATH=os.environ["PATH"], HOME=os.environ.get("HOME", "/tmp"), PYTHONDONTWRITEBYTECODE="1",
                       REDIS_HOST="127.0.0.1", REDIS_PORT=str(urlsplit(redis_url).port), MIAS_TEST_SHIM_REPORT=reports,
                       DATABASE_URL=os.environ["TEST_DATABASE_URL"], MIAS_TEST_SHIM_ALLOW_LOOPBACK="true",
                       **{f"{p}_PERSISTENCE_SHADOW_ENABLED": "true" for p in ("FED", "MACRO", "TREASURY", "GEOPOLITICAL",
                                                                              "SEC", "NEWS")})
        definitions = [shimmed(d, timeout_seconds=120) for d in registry.build_definitions(load_settings({}))]
        scheduler = Scheduler(definitions, environ=environ, shutdown_grace_seconds=60)
        with self.assertLogs("orchestrator", "INFO"):
            results = scheduler.run_once()
        self.assertEqual({r.collector: r.status for r in results}, EXPECTED_STATUS)
        for name, module in MODULES.items():
            with open(os.path.join(reports, f"{module}.json")) as handle:
                report = json.load(handle)
            self.assertTrue(ENTRY[name] <= set(report["entry_functions"]), name)
            self.assertEqual((report["telegram_attempts"], report["openai_attempts"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
