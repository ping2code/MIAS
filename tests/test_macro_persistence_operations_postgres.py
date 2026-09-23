"""Live restart, outage, burst and spawned-process macro shadow validation."""
from copy import deepcopy
import json
import multiprocessing
import os
import re
import subprocess
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from persistence import macro_shadow as shadow
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine, transaction
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.repository import EventRepository
from tests import test_persistence_postgres as foundation
from tests import test_macro_persistence_operations as operations
from tests.test_macro_persistence_adapter import sample, NOW
from tests.test_macro_shadow_persistence import collect


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
        writer = shadow.ShadowWriter(engine_factory=factory)
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


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLOperationsTests(operations.ReconciliationTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema

    def writer(self, **kwargs):
        writer = shadow.ShadowWriter(engine_factory=lambda: self.engine, **kwargs)
        self.addCleanup(writer.shutdown)
        return writer

    def test_restart_duplicate_and_new_material_version(self):
        event = sample()
        first = self.writer()
        first.submit(event)
        self.assertTrue(first.shutdown()["stopped"])
        restarted = self.writer()
        restarted.submit(event)
        revised = deepcopy(event)
        revised["metrics"]["headline_cpi_sa"]["value"] += 1
        restarted.submit(revised)
        stats = restarted.shutdown()["stats"]
        self.assertEqual((stats["persisted"], stats["duplicate"], stats["failed"]), (2, 1, 0))
        self.assertEqual(self.reconcile(revised)["mismatches"], [])
        self.assertEqual(self.reconcile(event, expect_current=False)["mismatches"], [])
        with transaction(self.engine) as session:
            self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(event_versions)).scalar_one(), 2)

    def test_reconciliation_in_database_read_only_transaction(self):
        event = sample()
        shadow.persist_macro(self.engine, event, NOW)
        with transaction(self.engine) as session:
            session.execute(sa.text("SET TRANSACTION READ ONLY"))
            result = operations.reconcile_macro_event(event, EventRepository(session))
            self.assertEqual(result["mismatches"], [])

    def test_worker_exception_rollback_then_recreation(self):
        event = sample()
        writer = self.writer()
        with patch.object(EventRepository, "append_history", side_effect=RuntimeError("private")):
            writer.submit(event)
            operations.await_stats(writer, "failed", 1)
        self.assertFalse(self.reconcile(event)["event_found"])
        writer.submit(event)
        operations.await_stats(writer, "persisted", 1)
        self.assertTrue(writer.shutdown()["stopped"])
        recreated = self.writer()
        recreated.submit(event)
        stats = recreated.shutdown()["stats"]
        self.assertEqual((stats["persisted"], stats["duplicate"]), (1, 1))
        self.assertEqual(self.reconcile(event)["mismatches"], [])

    def test_bounded_burst_200_snapshots(self):
        writer = self.writer(capacity=256)
        event = sample()
        started = monotonic()
        for index in range(200):
            snapshot = deepcopy(event)
            snapshot["event_id"] = f"phase2c-burst-{index}"
            self.assertTrue(writer.submit(snapshot))
        result = writer.shutdown(timeout=30)
        elapsed = monotonic() - started
        stats = result["stats"]
        self.assertTrue(result["stopped"])
        self.assertEqual((stats["queued"], stats["persisted"], stats["failed"], stats["dropped_queue_full"]), (200, 200, 0, 0))
        self.assertEqual(stats["queue_depth"], 0)
        with transaction(self.engine) as session:
            self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(events)).scalar_one(), 200)
        print("PHASE2C_BURST " + json.dumps(dict(queued=stats["queued"], persisted=stats["persisted"],
              dropped=stats["dropped_queue_full"] + stats["dropped_shutdown"], elapsed_seconds=round(elapsed, 3))))

    def concurrent_writers(self, first, second, *, existing=False):
        """Hold a DB lock until both spawned writers are observed waiting on it."""
        context = multiprocessing.get_context("spawn")
        gate = context.Barrier(3)
        results = context.Queue()
        prefix = "mias_ops_" + uuid4().hex[:12]
        url = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"])).url.render_as_string(hide_password=False)
        children = [context.Process(target=child_writer, args=(url, self.schema, prefix + str(index), event, gate, results))
                    for index, event in enumerate((first, second))]
        try:
            with self.engine.begin() as connection:
                if existing:
                    connection.execute(sa.select(events).where(events.c.event_key == first["event_id"]).with_for_update()).one()
                else:
                    connection.execute(events.insert().values(id=str(uuid4()), source_family="macro", identity_version="macro-v1",
                        event_key=first["event_id"], first_seen_at=NOW, last_seen_at=NOW))
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
                self.assertTrue(result["accepted"])
                self.assertTrue(result["stopped"])
                self.assertEqual(result["stats"]["persisted"], 1)
                self.assertEqual(result["stats"]["failed"], 0)
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
        event = sample()
        outcomes = self.concurrent_writers(event, event)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 1)
        self.assertEqual(self.reconcile(event)["mismatches"], [])
        with transaction(self.engine) as session:
            for table, count in ((events, 1), (event_versions, 1), (event_provenance, 3), (event_history, 2)):
                self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one(), count)

    def test_two_processes_material_revision_and_old_observation(self):
        event = sample()
        shadow.persist_macro(self.engine, event, NOW)
        revised = deepcopy(event)
        revised["metrics"]["headline_cpi_sa"]["value"] += 1
        outcomes = self.concurrent_writers(event, revised, existing=True)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 1)
        self.assertEqual(self.reconcile(revised)["mismatches"], [])
        self.assertEqual(self.reconcile(event, expect_current=False)["mismatches"], [])

    def test_two_processes_append_identical_new_provenance(self):
        event = sample()
        shadow.persist_macro(self.engine, event, NOW)
        event["source_hash"] = "a" * 64
        outcomes = self.concurrent_writers(event, event, existing=True)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 1)
        with transaction(self.engine) as session:
            self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(event_provenance)).scalar_one(), 4)
        self.assertEqual(self.reconcile(event)["mismatches"], [])

    def test_two_processes_append_concurrent_score_decision_histories(self):
        event = sample()
        shadow.persist_macro(self.engine, event, NOW)
        first, second = deepcopy(event), deepcopy(event)
        first["impact_score"] = 80
        second.update(impact_score=60, alert_decision="DISPLAY_ONLY")
        outcomes = self.concurrent_writers(first, second, existing=True)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 0)
        with transaction(self.engine) as session:
            self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(event_versions)).scalar_one(), 1)
            self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(event_history)).scalar_one(), 6)
        self.assertEqual(self.reconcile(first)["mismatches"], [])
        self.assertEqual(self.reconcile(second)["mismatches"], [])

    @unittest.skipUnless(os.environ.get("MIAS_PHASE2C_TEST_CONTAINER"), "Explicit disposable container opt-in required for outage test")
    def test_real_container_down_start_midrun_and_recovery(self):
        name = os.environ["MIAS_PHASE2C_TEST_CONTAINER"]
        self.assertEqual(name, "mias-test-phase2c-postgres")
        info = json.loads(docker_command("inspect", name))[0]
        self.assertEqual(info["Config"]["Labels"].get("mias.disposable-test"), "phase2c")
        self.assertEqual(len(info["Mounts"]), 1)
        self.assertEqual(info["Mounts"][0]["Type"], "volume")
        self.assertEqual(info["Mounts"][0]["Destination"], "/var/lib/postgresql/data")
        self.assertIn("POSTGRES_DB=mias_test_phase2c", info["Config"]["Env"])
        self.assertIn("POSTGRES_USER=mias_test_user", info["Config"]["Env"])
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.assertEqual(settings.url.host, "127.0.0.1")
        self.assertEqual(settings.url.database, "mias_test_phase2c")
        self.assertEqual(info["NetworkSettings"]["Ports"]["5432/tcp"],
                         [{"HostIp": "127.0.0.1", "HostPort": str(settings.url.port)}])
        def start():
            docker_command("start", name)
            deadline = monotonic() + 10
            while monotonic() < deadline:
                result = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "mias_test_user", "-d", "mias_test_phase2c"],
                                        capture_output=True, timeout=5)
                if result.returncode == 0: return
                sleep(0.1)
            self.fail("Disposable PostgreSQL did not recover")
        event = sample()
        writer = self.writer()
        writer.submit(event)
        operations.await_stats(writer, "persisted", 1)
        baseline, _ = collect(False)
        try:
            docker_command("stop", "--time", "5", name)
            enabled, _ = collect(True, submit=writer.submit)
            self.assertEqual(enabled, baseline)
            operations.await_stats(writer, "failed", 1)
            new_writer = self.writer()
            down = deepcopy(event)
            down["event_id"] = "phase2c-only-during-outage"
            new_writer.submit(down)
            operations.await_stats(new_writer, "failed", 1)
            start()
            writer.submit(event)  # Same live writer reconnects and deduplicates.
            operations.await_stats(writer, "persisted", 2)
            self.assertEqual(writer.get_persistence_stats()["duplicate"], 1)
            recovered = deepcopy(event)
            recovered["event_id"] = "phase2c-after-recovery"
            new_writer.submit(recovered)
            operations.await_stats(new_writer, "persisted", 1)
            self.assertFalse(self.reconcile(down)["event_found"])  # Failed work is not replayed.
            self.assertEqual(self.reconcile(recovered)["mismatches"], [])
            self.assertEqual(self.reconcile(event)["mismatches"], [])
            self.assertEqual(writer.get_persistence_stats()["queue_depth"], 0)
        finally:
            start()  # Restore only our verified disposable instance before schema cleanup.
            writer.shutdown()
            if "new_writer" in locals(): new_writer.shutdown()
