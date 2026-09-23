"""Live geopolitical shadow validation: real transactions, processes, restart, outage, alias durability."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import multiprocessing
import os
import re
import socket
import subprocess
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from persistence import geopolitical_shadow as shadow
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine, transaction
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.repository import EventRepository
from tests import test_persistence_postgres as foundation
from tests import test_geopolitical_persistence_adapter as adapter
from tests import test_geopolitical_persistence_readiness as readiness
from tests.test_geopolitical_persistence_adapter import sample, NOW
from tests.test_geopolitical_shadow_persistence import replay_steps
from tests.test_treasury_shadow_persistence import await_stats


def child_writer(url, schema, name, event, gate, results):
    """Spawn entry point: all engines/connections/sessions belong to this process."""
    try:
        if not re.fullmatch(r"mias_phase2a_[0-9a-f]{32}", schema):
            raise ValueError("Invalid generated schema")
        settings = require_test_database(DatabaseSettings(url=url, application_name=name))
        def factory():
            engine = make_engine(settings)
            @sa.event.listens_for(engine, "connect")
            def path(connection, _):
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute(f'SET search_path TO "{schema}"')
                connection.autocommit = False
            return engine
        writer = shadow.GeopoliticalShadowWriter(engine_factory=factory)
        gate.wait(timeout=15)
        accepted = writer.submit(event)
        result = writer.shutdown(timeout=15)
        results.put(dict(accepted=accepted, **result))
    except Exception:
        results.put({"error": "child writer failed"})


def docker_command(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Disposable Docker operation failed")
    return result.stdout


def live(cls):
    return unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")(cls)


@live
class PostgreSQLGeopoliticalAdapterTests(adapter.GeopoliticalAdapterTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


@live
class PostgreSQLGeopoliticalReadinessTests(readiness.GeopoliticalReadinessTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


@live
class PostgreSQLGeopoliticalOperationsTests(unittest.TestCase):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema
    reconcile = readiness.GeopoliticalReadinessTests.reconcile
    count = adapter.GeopoliticalAdapterTests.count

    def writer(self, **kwargs):
        writer = shadow.GeopoliticalShadowWriter(engine_factory=lambda: self.engine, **kwargs)
        self.addCleanup(writer.shutdown)
        return writer

    def test_actual_worker_commits_full_collector_sequence(self):
        baseline, _ = replay_steps(False)
        writer = self.writer()
        enabled, submitted = replay_steps(True, submit=writer.submit)
        result = writer.shutdown(timeout=15)
        self.assertEqual(enabled, baseline)
        self.assertTrue(result["stopped"])
        self.assertEqual((result["stats"]["failed"], result["stats"]["persisted"]), (0, len(submitted)))
        not_current = {"trade", "stale_companion"}  # Earliest disclosure is current (Phase 2N).
        for label, event, _ in submitted:
            self.assertEqual(self.reconcile(event, expect_current=label not in not_current)["mismatches"], [], label)
        self.assertEqual(self.count(events), 14)

    def test_worker_repository_failure_rolls_back_without_collector_effect(self):
        steps = [s for s in readiness.corpus.STEPS if s[0] in {"bis_final", "fr_companion"}]
        baseline, _ = replay_steps(False, steps=steps)
        with patch.object(EventRepository, "append_history", side_effect=RuntimeError("private")), \
             self.assertLogs("geopolitical_shadow", level="WARNING"):
            writer = self.writer()
            enabled, _ = replay_steps(True, submit=writer.submit, steps=steps)
            self.assertTrue(writer.shutdown(timeout=5)["stopped"])
        self.assertEqual(enabled, baseline)
        for table in (events, event_versions, event_provenance, event_history):
            self.assertEqual(self.count(table), 0)

    def test_database_unavailable_at_start_does_not_affect_collector(self):
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        reserved = self.enterContext(socket.socket())
        reserved.bind(("127.0.0.1", 0))  # Reserved, never listening: cannot be any real database.
        missing = DatabaseSettings(url=settings.url.set(host="127.0.0.1", port=reserved.getsockname()[1]),
                                   connect_timeout_seconds=2)
        steps = readiness.corpus.STEPS[:6]
        baseline, _ = replay_steps(False, steps=steps)
        with self.assertLogs("geopolitical_shadow", level="WARNING") as logs:
            writer = shadow.GeopoliticalShadowWriter(engine_factory=lambda: make_engine(missing))
            enabled, submitted = replay_steps(True, submit=writer.submit, steps=steps)
            result = writer.shutdown(timeout=15)
        self.assertEqual(enabled, baseline)
        self.assertEqual((result["stats"]["failed"], result["stats"]["persisted"]), (len(submitted), 0))
        self.assertNotIn("mias_test_user", str(logs.output))

    def test_restart_deduplicates_and_keeps_companion_history(self):
        first = self.writer()
        first.submit(sample("bis_final"))
        first.submit(sample("fr_companion"))
        self.assertTrue(first.shutdown()["stopped"])
        restarted = self.writer()
        restarted.submit(sample("alias_expiry"))  # Post-expiry resolution lost the companion in Redis.
        restarted.submit(sample("fr_companion"))
        stats = restarted.shutdown()["stats"]
        self.assertEqual((stats["persisted"], stats["duplicate"], stats["failed"]), (2, 2, 0))
        self.assertEqual((self.count(events), self.count(event_versions), self.count(event_provenance)), (1, 1, 2))
        self.assertEqual(self.reconcile(sample("fr_companion"))["mismatches"], [])

    def test_out_of_order_through_worker_is_held(self):
        writer = self.writer()
        writer.submit(sample("trade"))
        writer.submit(sample("earlier_companion_disclosure"))
        writer.submit(sample("bis_final"))
        writer.submit(sample("stale_companion"), make_current=False)
        stats = writer.shutdown()["stats"]
        # earlier_disclosure promotes; only the stale caller-disabled observation is held (Phase 2N).
        self.assertEqual((stats["persisted"], stats["promotion_held"], stats["promotion_ambiguous"]), (4, 1, 0))
        self.assertEqual(self.reconcile(sample("earlier_companion_disclosure"))["mismatches"], [])
        self.assertEqual(self.reconcile(sample("trade"), expect_current=False)["mismatches"], [])
        self.assertEqual(self.reconcile(sample("stale_companion"), expect_current=False)["mismatches"], [])

    def test_worker_exception_rollback_then_recreation(self):
        event = sample("complaint")
        writer = self.writer()
        with patch.object(EventRepository, "append_history", side_effect=RuntimeError("private")):
            writer.submit(event)
            await_stats(writer, "failed", 1)
        self.assertFalse(self.reconcile(event)["event_found"])  # Failed shadow work is not replayed.
        writer.submit(event)
        await_stats(writer, "persisted", 1)
        self.assertTrue(writer.shutdown()["stopped"])
        recreated = self.writer()
        recreated.submit(event)
        stats = recreated.shutdown()["stats"]
        self.assertEqual((stats["persisted"], stats["duplicate"]), (1, 1))
        self.assertEqual(self.reconcile(event)["mismatches"], [])

    def concurrent_writers(self, first, second, *, existing=False):
        """Hold a DB lock until both spawned writers are observed waiting on it."""
        context = multiprocessing.get_context("spawn")
        gate = context.Barrier(3)
        results = context.Queue()
        prefix = "mias_geo_" + uuid4().hex[:12]
        url = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"])).url.render_as_string(hide_password=False)
        children = [context.Process(target=child_writer, args=(url, self.schema, prefix + str(index), event, gate, results))
                    for index, event in enumerate((first, second))]
        try:
            with self.engine.begin() as connection:
                if existing:
                    connection.execute(sa.select(events).where(events.c.event_key == first["event_id"]).with_for_update()).one()
                else:
                    connection.execute(events.insert().values(id=str(uuid4()), source_family="geopolitical",
                        identity_version="geopolitical-v1", event_key=first["event_id"], first_seen_at=NOW, last_seen_at=NOW))
                for child in children: child.start()
                gate.wait(timeout=15)
                deadline = monotonic() + 4
                blocked = 0
                while monotonic() < deadline:
                    with self.admin.connect() as observer:
                        blocked = observer.execute(sa.text("SELECT count(*) FROM pg_stat_activity "
                            "WHERE application_name LIKE :name AND wait_event_type='Lock'"), {"name": prefix + "%"}).scalar_one()
                    if blocked == 2: break
                    sleep(0.01)
                self.assertEqual(blocked, 2, "Both independent processes must overlap on real PostgreSQL locks")
            outcomes = [results.get(timeout=15) for _ in children]
            for child in children:
                child.join(timeout=5)
                self.assertEqual(child.exitcode, 0)
            for result in outcomes:
                self.assertNotIn("error", result)
                self.assertTrue(result["accepted"] and result["stopped"])
                self.assertEqual((result["stats"]["persisted"], result["stats"]["failed"]), (1, 0))
            return outcomes
        finally:
            for child in children:
                if child.pid is not None:
                    child.join(timeout=1)
                    if child.is_alive():
                        child.terminate()  # Only our own spawned test worker on test failure.
                        child.join(timeout=5)
            results.close()
            results.join_thread()

    def test_two_processes_same_event_version_and_provenance(self):
        event = sample("fr_companion")
        outcomes = self.concurrent_writers(event, event)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 1)
        self.assertEqual(self.reconcile(event)["mismatches"], [])
        for table, count in ((events, 1), (event_versions, 1), (event_provenance, 2), (event_history, 3)):
            self.assertEqual(self.count(table), count)

    def test_two_processes_companion_provenance_contention(self):
        shadow.persist_geopolitical(self.engine, sample("bis_final"), NOW)
        companion = sample("fr_companion")
        outcomes = self.concurrent_writers(companion, companion, existing=True)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 1)
        self.assertEqual(self.count(event_provenance), 2)  # Relationship row inserted exactly once.
        self.assertEqual(self.reconcile(companion)["mismatches"], [])

    def test_two_processes_revision_writes_resolve_by_source_order(self):
        base = sample("trade")
        shadow.persist_geopolitical(self.engine, base, NOW)
        newer = deepcopy(base)
        newer["summary"] += " Synthetic substantive revision."
        newer["published_at"] = (datetime.fromisoformat(base["published_at"]) + timedelta(hours=1)).isoformat()
        earlier = sample("earlier_companion_disclosure")
        outcomes = self.concurrent_writers(earlier, newer, existing=True)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 0)
        self.assertEqual(self.count(event_versions), 3)
        self.assertEqual(self.reconcile(newer)["mismatches"], [])
        self.assertEqual(self.reconcile(earlier, expect_current=False)["mismatches"], [])

    def test_two_processes_append_distinct_outcome_histories(self):
        event = sample("proposal")
        shadow.persist_geopolitical(self.engine, event, NOW)
        first, second = deepcopy(event), deepcopy(event)
        first.update(readiness.corpus.AI)
        second.update(alert_decision="IGNORE")
        outcomes = self.concurrent_writers(first, second, existing=True)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 0)
        self.assertEqual((self.count(event_versions), self.count(event_history)), (1, 4))
        self.assertEqual(self.reconcile(first)["mismatches"], [])
        self.assertEqual(self.reconcile(second)["mismatches"], [])

    @unittest.skipUnless(os.environ.get("MIAS_PHASE2C_TEST_CONTAINER"), "Explicit disposable container opt-in required for outage test")
    def test_real_container_down_midrun_and_recovery(self):
        # Same explicit opt-in and ownership proof as the Phase 2C-2E outage tests.
        name = os.environ["MIAS_PHASE2C_TEST_CONTAINER"]
        self.assertRegex(name, r"^mias-test-phase2[c-f]-postgres$")
        info = json.loads(docker_command("inspect", name))[0]
        self.assertRegex(info["Config"]["Labels"].get("mias.disposable-test", ""), r"^phase2[c-f]$")
        self.assertTrue(len(info["Mounts"]) == 1 and info["Mounts"][0]["Type"] == "volume"
                        and info["Mounts"][0]["Destination"] == "/var/lib/postgresql/data")
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.assertIn("POSTGRES_DB=" + settings.url.database, info["Config"]["Env"])
        self.assertIn("POSTGRES_USER=mias_test_user", info["Config"]["Env"])
        self.assertEqual(settings.url.host, "127.0.0.1")
        self.assertEqual(info["NetworkSettings"]["Ports"]["5432/tcp"],
                         [{"HostIp": "127.0.0.1", "HostPort": str(settings.url.port)}])
        def start():
            docker_command("start", name)
            deadline = monotonic() + 10
            while monotonic() < deadline:
                result = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "mias_test_user", "-d", settings.url.database],
                                        capture_output=True, timeout=5)
                if result.returncode == 0: return
                sleep(0.1)
            self.fail("Disposable PostgreSQL did not recover")
        event = sample("entity_list")
        writer = self.writer()
        writer.submit(event)
        await_stats(writer, "persisted", 1)
        steps = readiness.corpus.STEPS[:4]
        baseline, _ = replay_steps(False, steps=steps)
        try:
            docker_command("stop", "--time", "5", name)
            enabled, submitted = replay_steps(True, submit=writer.submit, steps=steps)
            self.assertEqual(enabled, baseline)  # Alerts, Redis, AI and Telegram unchanged during outage.
            await_stats(writer, "failed", len(submitted), timeout=30)
            fresh = self.writer()
            down = sample("sanctions")
            fresh.submit(down)
            await_stats(fresh, "failed", 1, timeout=15)
            start()
            writer.submit(event)  # Same live writer reconnects lazily and deduplicates.
            await_stats(writer, "persisted", 2, timeout=15)
            self.assertEqual(writer.get_persistence_stats()["duplicate"], 1)
            recovered = sample("moea_disruption")
            fresh.submit(recovered)
            await_stats(fresh, "persisted", 1, timeout=15)
            self.assertFalse(self.reconcile(down)["event_found"])  # No automatic replay/outbox.
            self.assertFalse(self.reconcile(sample("bis_final"))["event_found"])
            self.assertEqual(self.reconcile(recovered)["mismatches"], [])
            self.assertEqual(self.reconcile(event)["mismatches"], [])
            self.assertEqual(writer.get_persistence_stats()["queue_depth"], 0)
        finally:
            start()  # Restore only our verified disposable instance before schema cleanup.
            writer.shutdown()
            if "fresh" in locals(): fresh.shutdown()
