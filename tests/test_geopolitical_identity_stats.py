"""Phase 2J: collector-side durable-identity counter visibility and staging rollout validation."""
from contextlib import ExitStack
import os
import runpy
import subprocess
import sys
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from persistence import geopolitical_durable_identity as durable_identity
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import PersistenceError, make_engine
from tests import geopolitical_readiness_corpus as corpus
from tests import staging_rollout
from tests.test_geopolitical_pipeline import MemoryRedis, geo

ROOT_A = "a" * 64
FIELDS = ("lookup_attempted", "lookup_hit", "lookup_miss", "lookup_timeout", "lookup_error", "lookup_conflict",
          "lookup_skipped_busy", "redis_hit_bypass", "registry_inserted", "registry_existing", "registry_conflict",
          "registry_error")


def collect(docs, redis, *, stats_log, lookup=None, interval=300):
    with ExitStack() as stack:
        stack.enter_context(patch.object(geo, "GEOPOLITICAL_IDENTITY_STATS_LOG_ENABLED", stats_log))
        stack.enter_context(patch.object(geo, "GEOPOLITICAL_IDENTITY_STATS_LOG_INTERVAL_SECONDS", interval))
        stack.enter_context(patch.object(geo, "GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED", lookup is not None))
        if lookup is not None:
            stack.enter_context(patch.object(durable_identity, "get_durable_lookup", lambda timeout_ms: lookup))
        return corpus.run_collector(docs, redis)


class IdentityStatsVisibilityTests(unittest.TestCase):
    def setUp(self):
        durable_identity._close()
        durable_identity._reset()  # Process-local counters, as after a restart.
        self.addCleanup(durable_identity._reset)

    def service(self, result=None, side_effect=None):
        lookup = durable_identity.DurableIdentityLookup(timeout_ms=50)
        self.addCleanup(lookup.close)
        self.enterContext(patch.object(lookup, "_query", return_value=result, side_effect=side_effect))
        return lookup

    def test_logging_disabled_by_default_emits_nothing(self):
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, {}, clear=True):
            config = runpy.run_path("shared/config.py")
        self.assertEqual((config["GEOPOLITICAL_IDENTITY_STATS_LOG_ENABLED"],
                          config["GEOPOLITICAL_IDENTITY_STATS_LOG_INTERVAL_SECONDS"]), (False, 300))
        with patch.object(durable_identity, "maybe_log_stats", side_effect=AssertionError("must not log")), \
             self.assertNoLogs("geopolitical_identity_stats"):
            collect([corpus.DOCS["bis_final"]], MemoryRedis(), stats_log=False)

    def test_config_validation(self):
        for value, expected in (("true", True), ("TRUE", True), ("yes", False)):
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, {"GEOPOLITICAL_IDENTITY_STATS_LOG_ENABLED": value}, clear=True):
                self.assertIs(runpy.run_path("shared/config.py")["GEOPOLITICAL_IDENTITY_STATS_LOG_ENABLED"], expected)
        for bad in ("9", "86401", "x"):
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, {"GEOPOLITICAL_IDENTITY_STATS_LOG_INTERVAL_SECONDS": bad},
                                                         clear=True), self.assertRaises(ValueError):
                runpy.run_path("shared/config.py")

    def test_enabled_logging_is_rate_limited_per_interval(self):
        with self.assertLogs("geopolitical_identity_stats", level="INFO") as logs:
            for _ in range(3):  # Three cycles inside one interval: one line.
                collect([corpus.DOCS["bis_final"]], MemoryRedis(), stats_log=True)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("event=geopolitical_identity_stats", logs.output[0])
        durable_identity._reset()  # Explicit monotonic instants from here on.
        with self.assertLogs("geopolitical_identity_stats", level="INFO") as logs:
            first = durable_identity.maybe_log_stats(interval_seconds=300, lookup_enabled=False, now=10_000)
            self.assertIsNone(durable_identity.maybe_log_stats(interval_seconds=300, lookup_enabled=False, now=10_299))
            again = durable_identity.maybe_log_stats(interval_seconds=300, lookup_enabled=False, now=10_300)
        self.assertEqual((first, again, len(logs.output)), (durable_identity.format_stats_line(lookup_enabled=False),) * 2 + (2,))

    def test_deterministic_fields_and_order(self):
        stats = dict.fromkeys(FIELDS, 0) | dict(lookup_hit=3, redis_hit_bypass=7, last_error_at="2026", extra=9)
        line = durable_identity.format_stats_line(stats, lookup_enabled=True)
        self.assertEqual(line, "event=geopolitical_identity_stats scope=process lookup_enabled=true "
                               "lookup_attempted=0 lookup_hit=3 lookup_miss=0 lookup_timeout=0 lookup_error=0 "
                               "lookup_conflict=0 lookup_skipped_busy=0 redis_hit_bypass=7 registry_inserted=0 "
                               "registry_existing=0 registry_conflict=0 registry_error=0")
        self.assertEqual(tuple(durable_identity.STATS_LOG_FIELDS), FIELDS)

    def test_logging_does_not_change_collector_behavior(self):
        def replay(stats_log):
            redis, outputs = MemoryRedis(), []
            for label, name, clock, change in corpus.STEPS:
                redis.now = clock
                if change:
                    corpus.expire(redis, change)
                outputs.append(collect([corpus.DOCS[name]], redis, stats_log=stats_log)[0])
            return outputs
        quiet = replay(False)
        durable_identity._reset()
        self.assertEqual(replay(True), quiet)

    def test_runtime_counters_are_visible_in_the_line(self):
        redis = MemoryRedis()
        expected = {}
        scenarios = (
            ("lookup_hit", dict(result=dict(status="hit", policy_id=ROOT_A, event_key="e", anchors=["fr:1"])), True),
            ("lookup_miss", dict(result=dict(status="miss", policy_id=None, event_key=None, anchors=[])), True),
            ("lookup_conflict", dict(result=dict(status="conflict", policy_id=None, event_key=None, anchors=["fr:1"])), True),
            ("lookup_error", dict(side_effect=PersistenceError("password=secret")), True),
        )
        with self.assertLogs("geopolitical_durable_identity", level="WARNING") as warnings, \
             patch.dict(durable_identity._last_log, clear=True):
            for counter, kwargs, expire in scenarios:
                corpus.expire(redis, "expire_all")
                collect([corpus.DOCS["bis_final"]], redis, stats_log=False, lookup=self.service(**kwargs))
                expected[counter] = expected.get(counter, 0) + 1
            release = Event()
            self.addCleanup(release.set)
            corpus.expire(redis, "expire_all")
            collect([corpus.DOCS["bis_final"]], redis, stats_log=False, lookup=self.service(side_effect=lambda *a: release.wait(5)))
            release.set()
            expected["lookup_timeout"] = 1
            collect([corpus.DOCS["fr_companion"]], redis, stats_log=False, lookup=self.service())  # Redis hit.
            expected["redis_hit_bypass"] = 1
        line = durable_identity.format_stats_line(lookup_enabled=True)
        for counter, value in expected.items():
            self.assertIn(f"{counter}={value}", line)
        self.assertIn("lookup_attempted=5", line)
        self.assertNotIn("secret", " ".join(warnings.output) + line)
        self.assertEqual(len({w.split(":", 2)[2] for w in warnings.output}), len(warnings.output))  # Bounded, one per kind.

    def test_counters_are_process_local_and_reset(self):
        durable_identity._count("lookup_hit", 4)
        self.assertEqual(durable_identity.get_durable_identity_stats()["lookup_hit"], 4)
        durable_identity._reset()
        self.assertEqual(durable_identity.get_durable_identity_stats()["lookup_hit"], 0)
        code = "import persistence.geopolitical_durable_identity as d; print(sum(d.get_durable_identity_stats()[k] for k in d.COUNTERS))"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(result.stdout.strip(), "0", result.stderr)

    def test_staging_safety_refusals(self):
        with self.assertRaises(Exception):
            staging_rollout.verify_database_url("postgresql://u@127.0.0.1:5432/production")
        with self.assertRaises(staging_rollout.StagingStop):
            staging_rollout.verify_database_url("postgresql://u@db.example.com:5432/mias_test_x")
        with self.assertRaises(staging_rollout.StagingStop):
            staging_rollout.verify_redis(MemoryRedis(), "redis://10.0.0.5:6379/0")
        busy = MemoryRedis()
        busy.set("unrelated", "1", ex=60)
        with self.assertRaises(staging_rollout.StagingStop):
            staging_rollout.verify_redis(busy)
        with self.assertRaises(staging_rollout.StagingStop):
            staging_rollout.render_report(dict(redis="postgresql://user:secret@host/db", postgresql_version="16",
                                               migration_revision="x", acceptance="PASS", pre_registry_history={},
                                               backfill={}, backfill_rerun={}, conflicts=[], baseline_audit={},
                                               rollout={}, post_rollout_audit={}, rollback={}, failure_injection={},
                                               final_audit={}))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLStagingRolloutTests(unittest.TestCase):
    """The full Phase 2I runbook on a fresh, dedicated, test-only database."""

    def setUp(self):
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.name = "mias_test_phase2j_" + uuid4().hex[:12]
        self.server = make_engine(settings)
        with self.server.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{self.name}"'))
        self.url = settings.url.set(database=self.name).render_as_string(hide_password=False)
        self.addCleanup(self.drop)

    def drop(self):
        staging_rollout.restart_collector()
        with self.server.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))
        self.server.dispose()

    def check(self, evidence):
        self.assertEqual(evidence["acceptance"], "PASS")
        self.assertEqual(evidence["migration_revision"], "0007_technical_evidence_ledger")  # Migration head.
        self.assertTrue(evidence["postgresql_version"].startswith("16."))
        pre = evidence["pre_registry_history"]
        self.assertEqual((pre["writer"]["failed"], pre["counters"]["registry_error"] > 0), (0, True))  # Registry absent at 0002.
        self.assertGreater(evidence["backfill"]["inserted"], 0)
        self.assertGreaterEqual(evidence["backfill"]["conflicts"], 1)
        self.assertEqual(evidence["backfill_rerun"]["inserted"], 0)
        self.assertEqual([c["anchor"] for c in evidence["conflicts"]], ["fr:2026-99920"])
        self.assertEqual(evidence["baseline_audit"]["exact_authoritative_anchor"], 1)
        rollout = evidence["rollout"]
        self.assertEqual({k: rollout["counters"][k] for k in ("lookup_attempted", "lookup_hit", "lookup_miss",
                          "lookup_timeout", "lookup_error", "lookup_conflict", "redis_hit_bypass")},
                         dict(lookup_attempted=2, lookup_hit=1, lookup_miss=1, lookup_timeout=0, lookup_error=0,
                              lookup_conflict=0, redis_hit_bypass=1))
        self.assertEqual(rollout["warnings"], [])
        self.assertEqual(len(rollout["stats_log_lines"]), 1)  # Rate limited within the interval.
        self.assertEqual((evidence["post_rollout_audit"]["new_exact_authoritative_anchor"],
                          evidence["post_rollout_audit"]["historical_groups"]), (0, 1))
        rollback = evidence["rollback"]
        self.assertEqual((rollback["lookup_instances_created"], rollback["durable_resolver_calls"],
                          rollback["lookup_attempted"], rollback["downgrade_required"]), (False, 0, 0, False))
        self.assertEqual(rollback["registry_rows_before"], rollback["registry_rows_after"])
        failures = evidence["failure_injection"]
        for name, counter in (("db_unavailable_redis_miss", "lookup_error"), ("lookup_timeout", "lookup_timeout"),
                              ("conflicted_anchor", "lookup_conflict")):
            self.assertEqual(failures[name]["counters"][counter], 1, name)
            self.assertTrue(failures[name]["processed_matches_control"], name)  # No alert suppression.
            self.assertTrue(failures[name]["warnings"], name)
        hit = failures["redis_hit_db_unavailable"]
        self.assertEqual((hit["counters"]["redis_hit_bypass"], hit["counters"]["lookup_attempted"]), (1, 0))
        self.assertTrue(hit["processed_matches_control"])
        self.assertEqual(evidence["final_audit"]["new_exact_authoritative_anchor"], 0)
        report = staging_rollout.render_report(evidence)
        self.assertEqual(report, staging_rollout.render_report(evidence))
        for token in ("postgresql://", "redis://", "password", self.name):
            self.assertNotIn(token, report)

    def test_full_runbook_with_in_memory_redis(self):
        self.check(staging_rollout.run_staging(self.url, MemoryRedis()))

    @unittest.skipUnless(os.environ.get("MIAS_PHASE2J_REDIS_URL"), "Disposable loopback Redis opt-in required")
    def test_full_runbook_with_disposable_redis_server(self):
        import redis
        client = redis.Redis.from_url(os.environ["MIAS_PHASE2J_REDIS_URL"], decode_responses=True,
                                      socket_connect_timeout=3, socket_timeout=3)
        self.addCleanup(client.close)
        staging_rollout.verify_redis(client, os.environ["MIAS_PHASE2J_REDIS_URL"])  # Refuses unknown state.
        self.addCleanup(lambda: [client.delete(k) for k in client.scan_iter(match="mias:geopolitical:*")])
        self.check(staging_rollout.run_staging(self.url, client, redis_url=os.environ["MIAS_PHASE2J_REDIS_URL"]))
