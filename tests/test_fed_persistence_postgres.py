"""Live Fed shadow validation: real transactions, processes, restart, outage and Redis parity."""
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

from persistence import fed_shadow as shadow
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine, transaction
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.repository import EventRepository
from tests import fed_readiness_corpus as corpus
from tests import test_persistence_postgres as foundation
from tests import test_fed_persistence_adapter as adapter
from tests.test_fed_persistence_adapter import sample, NOW
from tests.test_fed_shadow_persistence import replay
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
        writer = shadow.FedShadowWriter(engine_factory=factory)
        gate.wait(timeout=15)
        accepted = writer.submit(event)
        results.put(dict(accepted=accepted, **writer.shutdown(timeout=15)))
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
class PostgreSQLFedAdapterTests(adapter.FedAdapterTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


@live
class PostgreSQLFedOperationsTests(unittest.TestCase):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema
    count = adapter.FedAdapterTests.count
    reconcile = adapter.FedAdapterTests.reconcile

    def writer(self, **kwargs):
        writer = shadow.FedShadowWriter(engine_factory=lambda: self.engine, **kwargs)
        self.addCleanup(writer.shutdown)
        return writer

    def test_actual_worker_commits_full_collector_sequence(self):
        baseline, _ = replay(False)
        writer = self.writer()
        enabled, submitted = replay(True, submit=writer.submit)
        result = writer.shutdown(timeout=15)
        self.assertEqual(enabled, baseline)  # Including all Redis processed/delivered state.
        self.assertEqual((result["stats"]["failed"], result["stats"]["persisted"]), (0, len(submitted)))
        for _, event, _kwargs in submitted:
            self.assertEqual(self.reconcile(event)["mismatches"], [])
        self.assertEqual(self.count(events), 8)

    def test_repository_failure_rolls_back_without_collector_effect(self):
        baseline, _ = replay(False)
        with patch.object(EventRepository, "append_history", side_effect=RuntimeError("private")), \
             self.assertLogs("fed_shadow", level="WARNING"):
            writer = self.writer()
            enabled, submitted = replay(True, submit=writer.submit)
            self.assertTrue(writer.shutdown(timeout=10)["stopped"])
        self.assertEqual(enabled, baseline)
        # Every scored snapshot rolled back atomically; unscored skips (stale/missing date)
        # have no history to append and legitimately commit.
        unscored = {event["fed_fingerprint"] for _, event, _ in submitted if "impact_score" not in event}
        self.assertEqual((self.count(event_history), self.count(events)), (0, len(unscored)))
        with transaction(self.engine) as session:
            keys = set(session.execute(sa.select(events.c.event_key)).scalars())
        self.assertEqual(keys, unscored)

    def test_database_unavailable_at_start(self):
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        reserved = self.enterContext(socket.socket())
        reserved.bind(("127.0.0.1", 0))  # Reserved, never listening: cannot be a real database.
        missing = DatabaseSettings(url=settings.url.set(host="127.0.0.1", port=reserved.getsockname()[1]),
                                   connect_timeout_seconds=2)
        baseline, _ = replay(False)
        with self.assertLogs("fed_shadow", level="WARNING") as logs:
            writer = shadow.FedShadowWriter(engine_factory=lambda: make_engine(missing))
            enabled, submitted = replay(True, submit=writer.submit)
            result = writer.shutdown(timeout=20)
        self.assertEqual(enabled, baseline)
        self.assertEqual((result["stats"]["failed"], result["stats"]["persisted"]), (len(submitted), 0))
        self.assertNotIn("mias_test_user", str(logs.output))

    def test_restart_dedup_and_out_of_order_through_worker(self):
        first = self.writer()
        first.submit(sample("policy_action"))
        self.assertTrue(first.shutdown()["stopped"])
        restarted = self.writer()
        for label in ("policy_action", "synthetic_material_revision", "synthetic_older_observation"):
            restarted.submit(sample(label))
        stats = restarted.shutdown()["stats"]
        self.assertEqual((stats["persisted"], stats["duplicate"], stats["promotion_held"], stats["failed"]), (3, 1, 1, 0))
        self.assertEqual(self.reconcile(sample("synthetic_material_revision"))["mismatches"], [])
        self.assertEqual(self.reconcile(sample("synthetic_older_observation"), expect_current=False)["mismatches"], [])

    def test_worker_exception_rollback_then_recreation(self):
        event = sample("economic_projections")
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
        self.assertEqual(recreated.shutdown()["stats"]["duplicate"], 1)
        self.assertEqual(self.reconcile(event)["mismatches"], [])

    def concurrent_writers(self, first, second, *, existing=False):
        context = multiprocessing.get_context("spawn")
        gate, results = context.Barrier(3), context.Queue()
        prefix = "mias_fed_" + uuid4().hex[:12]
        url = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"])).url.render_as_string(hide_password=False)
        children = [context.Process(target=child_writer, args=(url, self.schema, prefix + str(i), event, gate, results))
                    for i, event in enumerate((first, second))]
        try:
            with self.engine.begin() as connection:
                if existing:
                    connection.execute(sa.select(events).where(events.c.event_key == first["fed_fingerprint"]).with_for_update()).one()
                else:
                    connection.execute(events.insert().values(id=str(uuid4()), source_family="fed", identity_version="fed-v1",
                                                              event_key=first["fed_fingerprint"], first_seen_at=NOW, last_seen_at=NOW))
                for child in children: child.start()
                gate.wait(timeout=15)
                deadline, blocked = monotonic() + 4, 0
                while monotonic() < deadline:
                    with self.admin.connect() as observer:
                        blocked = observer.execute(sa.text("SELECT count(*) FROM pg_stat_activity WHERE application_name "
                            "LIKE :name AND wait_event_type='Lock'"), {"name": prefix + "%"}).scalar_one()
                    if blocked == 2: break
                    sleep(0.01)
                self.assertEqual(blocked, 2, "Both independent processes must overlap on real PostgreSQL locks")
            outcomes = [results.get(timeout=15) for _ in children]
            for child in children:
                child.join(timeout=5)
                self.assertEqual(child.exitcode, 0)
            for outcome in outcomes:
                self.assertNotIn("error", outcome)
                self.assertEqual((outcome["stats"]["persisted"], outcome["stats"]["failed"]), (1, 0))
            return outcomes
        finally:
            for child in children:
                if child.pid is not None:
                    child.join(timeout=1)
                    if child.is_alive():
                        child.terminate()  # Only our own spawned test worker on failure.
                        child.join(timeout=5)
            results.close()
            results.join_thread()

    def test_two_processes_identical_write(self):
        event = sample()
        outcomes = self.concurrent_writers(event, event)
        self.assertEqual(sum(o["stats"]["duplicate"] for o in outcomes), 1)
        for table, count in ((events, 1), (event_versions, 1), (event_provenance, 1), (event_history, 3)):
            self.assertEqual(self.count(table), count)
        self.assertEqual(self.reconcile(event)["mismatches"], [])

    def test_two_processes_concurrent_material_revisions(self):
        shadow.persist_fed(self.engine, sample("policy_action"), NOW)
        newer, older = sample("synthetic_material_revision"), sample("synthetic_older_observation")
        outcomes = self.concurrent_writers(older, newer, existing=True)
        self.assertEqual(sum(o["stats"]["duplicate"] for o in outcomes), 0)
        self.assertEqual(self.count(event_versions), 3)
        self.assertEqual(self.reconcile(newer)["mismatches"], [])
        self.assertEqual(self.reconcile(older, expect_current=False)["mismatches"], [])

    def test_two_processes_provenance_contention(self):
        event = sample()
        shadow.persist_fed(self.engine, event, NOW)
        event["source_hash"] = "e" * 64
        outcomes = self.concurrent_writers(event, event, existing=True)
        self.assertEqual(sum(o["stats"]["duplicate"] for o in outcomes), 1)
        self.assertEqual(self.count(event_provenance), 2)
        self.assertEqual(self.reconcile(event)["mismatches"], [])

    @unittest.skipUnless(os.environ.get("MIAS_PHASE2C_TEST_CONTAINER"), "Explicit disposable container opt-in required for outage test")
    def test_real_container_down_midrun_and_recovery(self):
        name = os.environ["MIAS_PHASE2C_TEST_CONTAINER"]
        self.assertRegex(name, r"^mias-test-phase2[c-l]-postgres$")
        info = json.loads(docker_command("inspect", name))[0]
        self.assertRegex(info["Config"]["Labels"].get("mias.disposable-test", ""), r"^phase2[c-l]$")
        self.assertTrue(len(info["Mounts"]) == 1 and info["Mounts"][0]["Type"] == "volume")
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.assertIn("POSTGRES_DB=" + settings.url.database, info["Config"]["Env"])
        self.assertEqual(info["NetworkSettings"]["Ports"]["5432/tcp"],
                         [{"HostIp": "127.0.0.1", "HostPort": str(settings.url.port)}])
        def start():
            docker_command("start", name)
            deadline = monotonic() + 10
            while monotonic() < deadline:
                if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "mias_test_user", "-d", settings.url.database],
                                  capture_output=True, timeout=5).returncode == 0:
                    return
                sleep(0.1)
            self.fail("Disposable PostgreSQL did not recover")
        event = sample("minutes_ai_disabled")
        writer = self.writer()
        writer.submit(event)
        await_stats(writer, "persisted", 1)
        steps = corpus.STEPS[:6]
        baseline, _ = replay(False, steps=steps)
        try:
            docker_command("stop", "--time", "5", name)
            enabled, submitted = replay(True, submit=writer.submit, steps=steps)
            self.assertEqual(enabled, baseline)  # Redis, delivery, AI unchanged during the outage.
            await_stats(writer, "failed", len(submitted), timeout=30)
            start()
            writer.submit(event)  # Same writer reconnects lazily and deduplicates.
            await_stats(writer, "persisted", 2, timeout=15)
            self.assertEqual(writer.get_persistence_stats()["duplicate"], 1)
            recovered = sample("symbol_mention")
            writer.submit(recovered)
            await_stats(writer, "persisted", 3, timeout=15)
            self.assertFalse(self.reconcile(sample("policy_action"))["event_found"])  # No automatic replay.
            self.assertEqual(self.reconcile(recovered)["mismatches"], [])
        finally:
            start()
            writer.shutdown()
