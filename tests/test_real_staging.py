"""Phase 2M staging harness: safety refusals, credential scanning and hermetic worker processes.

No network, no containers: worker processes run labelled controlled fixtures against a
reserved, never-listening Redis port and with persistence switched off.
"""
import json
import os
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import real_staging as staging
from tests import real_staging_worker as worker
from tests.test_fed_pipeline import deduplicator

ROOT = staging.ROOT


def closed_port():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    return holder, holder.getsockname()[1]


def inspect(label="phase2m", port=55432, ip="127.0.0.1", mounts=None, tmpfs=None):
    return json.dumps([{"Config": {"Labels": {"mias.disposable-test": label}},
                        "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": ip, "HostPort": str(port)}]}},
                        "Mounts": mounts if mounts is not None else [{"Type": "volume"}],
                        "HostConfig": {"Tmpfs": tmpfs}, "Image": "sha256:" + "0" * 64}])


class HarnessSafetyTests(unittest.TestCase):
    def test_container_verification_refuses_unsafe_services(self):
        with patch.object(staging, "docker", return_value=inspect()):
            staging.verify_container("x", 55432, volume=True)
        for bad in (inspect(label="prod"), inspect(ip="0.0.0.0"), inspect(port=5432), inspect(mounts=[])):
            with self.subTest(bad=bad), patch.object(staging, "docker", return_value=bad), \
                 self.assertRaises(staging.StagingStop):
                staging.verify_container("x", 55432, volume=True)
        with patch.object(staging, "docker", return_value=inspect(mounts=[], tmpfs={"/data": ""})):
            staging.verify_container("x", 55432, volume=False)
        with patch.object(staging, "docker", return_value=inspect(mounts=[], tmpfs=None)), \
             self.assertRaises(staging.StagingStop):
            staging.verify_container("x", 55432, volume=False)

    def test_database_targets_are_test_only(self):
        self.assertTrue(staging.database_url("mias_test_phase2m_staging").endswith("/mias_test_phase2m_staging"))
        for name in ("production", "mias", "mias_test_other"):
            with self.subTest(name=name), self.assertRaises(Exception):
                staging.database_url(name)

    def test_credential_scan(self):
        self.assertEqual(staging.clean("healthy report"), "healthy report")
        for text in ("postgresql://u@h/d", "redis://h", "PASSWORD=x", "a secret", "api_key", "token=abc"):
            with self.subTest(text=text), self.assertRaises(staging.StagingStop):
                staging.clean(text)

    def test_worker_environment_is_explicit_only(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "y", "DATABASE_URL": "prod"}):
            env = staging.base_env(db_url="postgresql://u@127.0.0.1:1/mias_test_phase2m_staging", redis_port=1,
                                   FED_PERSISTENCE_SHADOW_ENABLED=True)
        self.assertEqual(set(env), {"PATH", "HOME", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "DATABASE_URL",
                                    "DB_CONNECT_TIMEOUT_SECONDS", "REDIS_HOST", "REDIS_PORT", "FED_PERSISTENCE_SHADOW_ENABLED"})
        self.assertEqual((env["FED_PERSISTENCE_SHADOW_ENABLED"], env["REDIS_HOST"]), ("true", "127.0.0.1"))
        self.assertTrue(env["DATABASE_URL"].endswith("mias_test_phase2m_staging"))

    def test_fed_fixture_identities_cannot_collide_with_real_releases(self):
        from tests.fed_readiness_corpus import ENTRIES
        from collector.fed_normalizer import normalize_fed_entry
        original = ENTRIES["policy_statement"]
        rewritten = worker._synthetic_fed(dict(original))
        self.assertIn("/mias-staging-fixture-", rewritten["link"])
        self.assertNotEqual(deduplicator.create_fingerprint(normalize_fed_entry(rewritten)),
                            deduplicator.create_fingerprint(normalize_fed_entry(original)))

    def test_endpoint_buckets_hide_document_identifiers(self):
        self.assertEqual(worker._bucket("/api/v1/documents/2026-19417.json"), "/api/v1/documents/*")
        self.assertEqual(worker._bucket("/news-events/news/press-releases/2026/09/x"), "/news-events/*")
        self.assertEqual(worker._bucket("/api/v1/documents.json"), "/api/v1/documents.json")


class HermeticWorkerProcessTests(unittest.TestCase):
    """Real separate processes speaking the line protocol, with no network or services."""

    def run_worker(self, collector, commands, **switches):
        holder, port = closed_port()
        self.addCleanup(holder.close)
        env = staging.base_env(db_url="postgresql://mias_test_user@127.0.0.1:1/mias_test_phase2m_staging",
                               redis_port=port, **switches)
        result = subprocess.run([sys.executable, "-m", "tests.real_staging_worker", "--collector", collector], cwd=ROOT,
                                env=env, input="".join(json.dumps(c) + "\n" for c in commands),
                                capture_output=True, text=True, timeout=120)
        return result, [json.loads(line) for line in result.stdout.splitlines()]

    def test_fed_controlled_cycle_graceful_and_hard_exit(self):
        for exit_op in ("exit", "hard_exit"):
            with self.subTest(exit_op=exit_op):
                result, lines = self.run_worker("fed", [
                    {"op": "controlled", "docs": ["policy_statement"], "clock": staging.FED_CLOCK, "label": "fixture"},
                    {"op": exit_op}])
                self.assertEqual(result.returncode, 0, result.stderr[-500:])
                cycle, finish = lines
                # Redis unreachable: the Fed collector fails open and still processes the fixture.
                self.assertEqual((cycle["stats"]["processed"], cycle["submitted"]), (1, 0))  # Shadow off.
                self.assertEqual((finish["telegram_calls"], finish["openai_calls"]), (0, 0))
                self.assertEqual(finish["shadow_stats"]["worker_started"], 0)  # No writer when disabled.
                self.assertEqual(cycle["pid"], finish["pid"])
                staging.clean(result.stdout + result.stderr)

    def test_geo_controlled_cycle_fails_closed_without_redis(self):
        result, lines = self.run_worker("geo", [
            {"op": "controlled", "docs": ["bis_final"], "clock": staging.GEO_CLOCK, "label": "fixture"},
            {"op": "exit"}])
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        cycle, finish = lines
        self.assertEqual((cycle["stats"]["state_errors"], cycle["events"]), (1, []))  # Existing fail-closed semantics.
        self.assertEqual(finish["durable_counters"]["lookup_attempted"], 0)  # Lookup switch off.
        self.assertEqual((finish["telegram_calls"], finish["openai_calls"]), (0, 0))

    def test_worker_never_reads_dotenv(self):
        code = ("import sys\nimport dotenv\ncalls=[]\ndotenv.load_dotenv=lambda *a,**k: calls.append(1)\n"
                "import tests.real_staging_worker\nprint(len(calls))")
        env = staging.base_env(db_url="postgresql://u@127.0.0.1:1/mias_test_phase2m_staging", redis_port=1)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "0", result.stderr[-500:])
