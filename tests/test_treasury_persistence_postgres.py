"""Live Treasury shadow validation: real transactions, processes, restart and outage."""
from copy import deepcopy
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

from persistence import treasury_shadow as shadow
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine, transaction
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.repository import EventRepository
from tests import test_persistence_postgres as foundation
from tests import test_treasury_persistence_adapter as adapter
from tests import test_treasury_persistence_readiness as readiness
from tests.test_treasury_persistence_adapter import sample, NOW
from tests.test_treasury_shadow_persistence import collect, mixed, await_stats


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
        writer = shadow.TreasuryShadowWriter(engine_factory=factory)
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
class PostgreSQLTreasuryAdapterTests(adapter.TreasuryAdapterTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


@live
class PostgreSQLTreasuryReadinessTests(readiness.TreasuryReadinessTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


@live
class PostgreSQLTreasuryOperationsTests(unittest.TestCase):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema
    reconcile = readiness.TreasuryReadinessTests.reconcile
    current = readiness.TreasuryReadinessTests.current

    def writer(self, **kwargs):
        writer = shadow.TreasuryShadowWriter(engine_factory=lambda: self.engine, **kwargs)
        self.addCleanup(writer.shutdown)
        return writer

    def count(self, table):
        with transaction(self.engine) as session:
            return session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()

    def test_actual_worker_commits_collector_results(self):
        baseline, _ = collect(False, **mixed())
        writer = self.writer()
        enabled, submitted = collect(True, submit=writer.submit, **mixed())
        result = writer.shutdown(timeout=10)
        self.assertEqual(enabled, baseline)
        self.assertTrue(result["stopped"])
        self.assertEqual((result["stats"]["failed"], result["stats"]["persisted"]), (0, len(submitted)))
        for value, kwargs in submitted:
            self.assertEqual(self.reconcile(value, expect_current=kwargs["make_current"])["mismatches"], [])
        processed = enabled[0][0][0]
        release = next(e for e in processed if e["treasury_category"] == "quarterly_refunding")
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            anchor = repo.find_event("treasury", "treasury-v1", release["event_id"])
            kinds = {row["kind"] for row in repo.history(anchor["current_version_id"])}
        self.assertEqual(kinds, {"score", "decision", "ai"})

    def test_worker_repository_failure_rolls_back_without_collector_effect(self):
        baseline, _ = collect(False)
        with patch.object(EventRepository, "append_history", side_effect=RuntimeError("private")), \
             self.assertLogs("treasury_shadow", level="WARNING"):
            writer = self.writer()
            enabled, _ = collect(True, submit=writer.submit)
            self.assertTrue(writer.shutdown(timeout=5)["stopped"])
        self.assertEqual(enabled, baseline)
        for table in (events, event_versions, event_provenance, event_history):
            self.assertEqual(self.count(table), 0)

    def test_database_unavailable_at_start_does_not_affect_collector(self):
        # Reserve a local port without listening: no service/non-test DB can occupy it.
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        reserved = self.enterContext(socket.socket())
        reserved.bind(("127.0.0.1", 0))
        missing = DatabaseSettings(url=settings.url.set(host="127.0.0.1", port=reserved.getsockname()[1]),
                                   connect_timeout_seconds=2)
        baseline, _ = collect(False, **mixed())
        with self.assertLogs("treasury_shadow", level="WARNING") as logs:
            writer = shadow.TreasuryShadowWriter(engine_factory=lambda: make_engine(missing))
            enabled, submitted = collect(True, submit=writer.submit, **mixed())
            result = writer.shutdown(timeout=10)
        self.assertEqual(enabled, baseline)
        self.assertEqual((result["stats"]["failed"], result["stats"]["persisted"]), (len(submitted), 0))
        self.assertNotIn("mias_test_user", str(logs.output))

    def test_restart_duplicate_and_new_material_version(self):
        event = sample()
        first = self.writer()
        first.submit(event)
        self.assertTrue(first.shutdown()["stopped"])
        restarted = self.writer()
        restarted.submit(event)
        restarted.submit(sample("refunding:material"))
        stats = restarted.shutdown()["stats"]
        self.assertEqual((stats["persisted"], stats["duplicate"], stats["failed"]), (2, 1, 0))
        self.assertEqual(self.reconcile(sample("refunding:material"))["mismatches"], [])
        self.assertEqual(self.reconcile(event, expect_current=False)["mismatches"], [])
        self.assertEqual(self.count(event_versions), 2)

    def test_out_of_order_through_worker_is_held(self):
        writer = self.writer()
        writer.submit(sample("refunding:material"))
        writer.submit(sample("refunding:older"))
        writer.submit(sample("yield:routine"))
        writer.submit(sample("yield:unverified_revision"))
        stats = writer.shutdown()["stats"]
        self.assertEqual((stats["persisted"], stats["promotion_held"], stats["promotion_ambiguous"]), (4, 2, 1))
        self.assertEqual(self.reconcile(sample("refunding:material"))["mismatches"], [])
        self.assertEqual(self.reconcile(sample("yield:routine"))["mismatches"], [])
        self.assertEqual(self.reconcile(sample("yield:unverified_revision"), expect_current=False)["mismatches"], [])

    def test_worker_exception_rollback_then_recreation(self):
        event = sample("bill_auction:result")
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
        prefix = "mias_tsy_" + uuid4().hex[:12]
        url = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"])).url.render_as_string(hide_password=False)
        children = [context.Process(target=child_writer, args=(url, self.schema, prefix + str(index), event, gate, results))
                    for index, event in enumerate((first, second))]
        try:
            with self.engine.begin() as connection:
                if existing:
                    connection.execute(sa.select(events).where(events.c.event_key == first["event_id"]).with_for_update()).one()
                else:
                    connection.execute(events.insert().values(id=str(uuid4()), source_family="treasury", identity_version="treasury-v1",
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
        event = sample("bill_auction:result")
        outcomes = self.concurrent_writers(event, event)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 1)
        self.assertEqual(self.reconcile(event)["mismatches"], [])
        for table, count in ((events, 1), (event_versions, 1), (event_provenance, 1), (event_history, 2)):
            self.assertEqual(self.count(table), count)

    def test_two_processes_material_revision_and_old_observation(self):
        event = sample()
        shadow.persist_treasury(self.engine, event, NOW)
        revised = sample("refunding:material")
        outcomes = self.concurrent_writers(event, revised, existing=True)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 1)
        self.assertEqual(self.reconcile(revised)["mismatches"], [])
        self.assertEqual(self.reconcile(event, expect_current=False)["mismatches"], [])

    def test_two_processes_conflicting_material_versions_resolve_by_source_order(self):
        shadow.persist_treasury(self.engine, sample(), NOW)
        newer, older = sample("refunding:material"), sample("refunding:older")
        outcomes = self.concurrent_writers(older, newer, existing=True)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 0)
        self.assertEqual(self.reconcile(newer)["mismatches"], [])
        self.assertEqual(self.reconcile(older, expect_current=False)["mismatches"], [])
        self.assertEqual(self.count(event_versions), 3)

    def test_two_processes_append_identical_new_provenance(self):
        event = sample("bill_auction:result")
        shadow.persist_treasury(self.engine, event, NOW)
        event["source_hash"] = "a" * 64
        outcomes = self.concurrent_writers(event, event, existing=True)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 1)
        self.assertEqual(self.count(event_provenance), 2)
        self.assertEqual(self.reconcile(event)["mismatches"], [])

    def test_two_processes_append_distinct_outcome_histories(self):
        event = sample("yield:threshold")
        shadow.persist_treasury(self.engine, event, NOW)
        first, second = deepcopy(event), deepcopy(event)
        first.update(corpus_ai())
        second.update(alert_decision="IGNORE")
        outcomes = self.concurrent_writers(first, second, existing=True)
        self.assertEqual(sum(r["stats"]["duplicate"] for r in outcomes), 0)
        self.assertEqual((self.count(event_versions), self.count(event_history)), (1, 4))
        self.assertEqual(self.reconcile(first)["mismatches"], [])
        self.assertEqual(self.reconcile(second)["mismatches"], [])

    @unittest.skipUnless(os.environ.get("MIAS_PHASE2C_TEST_CONTAINER"), "Explicit disposable container opt-in required for outage test")
    def test_real_container_down_midrun_and_recovery(self):
        # Same explicit opt-in and ownership proof as the Phase 2C/2D macro outage test.
        name = os.environ["MIAS_PHASE2C_TEST_CONTAINER"]
        self.assertRegex(name, r"^mias-test-phase2[c-e]-postgres$")
        info = json.loads(docker_command("inspect", name))[0]
        self.assertRegex(info["Config"]["Labels"].get("mias.disposable-test", ""), r"^phase2[c-e]$")
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
        event = sample("note_auction:result")
        writer = self.writer()
        writer.submit(event)
        await_stats(writer, "persisted", 1)
        baseline, _ = collect(False, **mixed())
        try:
            docker_command("stop", "--time", "5", name)
            enabled, submitted = collect(True, submit=writer.submit, **mixed())
            self.assertEqual(enabled, baseline)  # Alerts, Redis, AI and Telegram unchanged during outage.
            await_stats(writer, "failed", len(submitted), timeout=30)
            fresh = self.writer()
            down = sample("yield:threshold")
            fresh.submit(down)
            await_stats(fresh, "failed", 1, timeout=15)
            start()
            writer.submit(event)  # Same live writer reconnects lazily and deduplicates.
            await_stats(writer, "persisted", 2, timeout=15)
            self.assertEqual(writer.get_persistence_stats()["duplicate"], 1)
            recovered = sample("reopening:result")
            fresh.submit(recovered)
            await_stats(fresh, "persisted", 1, timeout=15)
            self.assertFalse(self.reconcile(down)["event_found"])  # No automatic replay/outbox.
            self.assertEqual(self.reconcile(recovered)["mismatches"], [])
            self.assertEqual(self.reconcile(event)["mismatches"], [])
            self.assertEqual(writer.get_persistence_stats()["queue_depth"], 0)
        finally:
            start()  # Restore only our verified disposable instance before schema cleanup.
            writer.shutdown()
            if "fresh" in locals(): fresh.shutdown()


def corpus_ai():
    from tests.treasury_readiness_corpus import AI
    return dict(AI)
