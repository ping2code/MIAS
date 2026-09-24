"""Live Phase 2H validation: migration, index, real bounded lookup, outage, concurrent registration."""
from datetime import datetime
import json
import multiprocessing
import os
import re
import socket
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa
from alembic import command

from persistence import geopolitical_durable_identity as durable_identity
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine, transaction
from persistence.geopolitical_registry import (
    AnchorRegistryRepository, backfill_geopolitical_anchor_registry, KEY,
)
from persistence.geopolitical_shadow import persist_geopolitical
from persistence.models import events, event_versions, event_provenance, event_history, geopolitical_anchor_registry as registry
from tests import geopolitical_readiness_corpus as corpus
from tests import test_persistence as unit
from tests import test_persistence_postgres as foundation
from tests import test_geopolitical_anchor_registry as phase2h
from tests.test_geopolitical_pipeline import MemoryRedis

STAGE = phase2h.STAGE
ROOT_A, ROOT_B = phase2h.ROOT_A, phase2h.ROOT_B


def child_register(url, schema, name, root, gate, results):
    """Spawn entry point: a separate process registering one anchor under ``root``."""
    try:
        if not re.fullmatch(r"mias_phase2a_[0-9a-f]{32}", schema):
            raise ValueError("Invalid generated schema")
        engine = make_engine(require_test_database(DatabaseSettings(url=url, application_name=name)))
        @sa.event.listens_for(engine, "connect")
        def path(connection, _):
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
            connection.autocommit = False
        gate.wait(timeout=15)
        with transaction(engine) as session:
            counts = AnchorRegistryRepository(session).register(
                anchors=["fr:2026-99901"], stage=STAGE, policy_id=root, event_key="event-" + root[:4],
                observed_at=phase2h.OBSERVED)
        engine.dispose()
        results.put(counts)
    except Exception:
        results.put({"error": "child registration failed"})


def live(cls):
    return unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")(cls)


@live
class PostgreSQLAnchorRegistryTests(phase2h.AnchorRegistryTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


@live
class PostgreSQLDurableIdentityLiveTests(unittest.TestCase):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema
    count = phase2h.AnchorRegistryTests.count
    dump = phase2h.AnchorRegistryTests.dump
    audit = phase2h.AnchorRegistryTests.audit
    persist = phase2h.AnchorRegistryTests.persist

    def real_lookup(self, **kwargs):
        lookup = durable_identity.DurableIdentityLookup(**kwargs)
        self.addCleanup(lookup.close)
        return lookup

    def persist_corpus(self):
        for row in corpus.load_corpus()[1]:
            persist_geopolitical(self.engine, row["event"], datetime.fromisoformat(row["observed_at"]),
                                 make_current=row["make_current"])

    def test_migration_schema_backfill_downgrade_upgrade_idempotent(self):
        with self.engine.connect() as connection:
            inspector = sa.inspect(connection)
            self.assertEqual({c["name"] for c in inspector.get_columns("geopolitical_anchor_registry")},
                             {c.name for c in registry.c})
            [unique] = inspector.get_unique_constraints("geopolitical_anchor_registry")
            self.assertEqual((unique["name"], unique["column_names"]), ("uq_geopolitical_anchor_registry_key", KEY))
            self.assertEqual({i["name"] for i in inspector.get_indexes("geopolitical_anchor_registry")},
                             {"ix_geopolitical_anchor_registry_policy", "uq_geopolitical_anchor_registry_key"})
            self.assertEqual({c["name"] for c in inspector.get_check_constraints("geopolitical_anchor_registry")},
                             {"ck_geopolitical_anchor_registry_anchor_type", "ck_geopolitical_anchor_registry_status",
                              "ck_geopolitical_anchor_registry_identity_shape",
                              "ck_geopolitical_anchor_registry_observation_order"})
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                             "0004_technical_snapshots")  # Migration head (Phase 4C added 0004).
        self.persist_corpus()
        with transaction(self.engine) as session:
            session.execute(registry.delete())
        history = self.dump()
        with transaction(self.engine) as session:
            first = backfill_geopolitical_anchor_registry(session)
        with self.engine.begin() as connection:
            command.downgrade(unit.migration_config(connection), "0002_macro_shadow_history")
        with self.engine.connect() as connection:
            tables = set(sa.inspect(connection).get_table_names())
        self.assertNotIn("geopolitical_anchor_registry", tables)
        self.assertEqual(tables, {"events", "event_versions", "event_provenance", "event_history", "alembic_version"})
        self.assertEqual(self.dump(), history)  # Downgrade removes only the registry.
        with self.engine.begin() as connection:
            command.upgrade(unit.migration_config(connection), "head")
        with transaction(self.engine) as session:
            again = backfill_geopolitical_anchor_registry(session)
        with transaction(self.engine) as session:
            rerun = backfill_geopolitical_anchor_registry(session)
        self.assertEqual(again, first)
        self.assertEqual((rerun["inserted"], rerun["conflicts"], rerun["already_present"]), (0, 0, first["anchors_seen"]))
        self.assertEqual(self.dump(), history)
        print("PHASE2H_MIGRATION_BACKFILL " + json.dumps(first, sort_keys=True))

    def test_lookup_uses_unique_index_on_populated_registry(self):
        with transaction(self.engine) as session:
            repo = AnchorRegistryRepository(session)
            for index in range(2000):
                repo.register(anchors=[f"fr:2026-{10000 + index}", f"eo:{index + 1}"], stage=STAGE,
                              policy_id=f"{index:064x}", event_key=f"event-{index}", observed_at=phase2h.OBSERVED)
        normalized = [("eo", "77"), ("fr", "2026-11234")]
        query = AnchorRegistryRepository._lookup_query(normalized, STAGE)
        sql = str(query.compile(dialect=self.engine.dialect, compile_kwargs={"literal_binds": True}))
        with transaction(self.engine) as session:
            session.execute(sa.text("ANALYZE geopolitical_anchor_registry"))
            plan = "\n".join(r[0] for r in session.execute(sa.text("EXPLAIN " + sql)))
        self.assertIn("uq_geopolitical_anchor_registry_key", plan)
        self.assertNotIn("Seq Scan", plan)
        started = monotonic()
        with transaction(self.engine) as session:
            result = AnchorRegistryRepository(session).lookup(anchors=["fr:2026-11234", "eo:1235"], stage=STAGE)
        self.assertLess(monotonic() - started, 1)
        self.assertEqual((result["status"], result["policy_id"]), ("hit", f"{1234:064x}"))

    def test_real_bounded_lookup_prevents_divergence_end_to_end(self):
        redis = MemoryRedis()
        _, joined = phase2h.collect([corpus.DOCS["bis_final"], phase2h.FR_WITH_EO], redis)
        self.persist(joined)
        corpus.expire(redis, "expire_all")
        lookup = self.real_lookup(timeout_ms=2000, engine_factory=lambda: self.engine)
        before = durable_identity.get_durable_identity_stats()
        _, alone = phase2h.collect([phase2h.FR_WITH_EO], redis, lookup)
        after = durable_identity.get_durable_identity_stats()
        self.assertEqual(alone[0][0]["event_id"], joined[0][0]["event_id"])
        self.assertEqual(after["lookup_hit"] - before["lookup_hit"], 1)
        self.persist(alone)
        self.assertEqual(self.count(events), 1)
        report = self.audit()
        self.assertEqual(report["summary"]["exact_authoritative_anchor"], 0)
        self.assertEqual(report, self.audit())  # Deterministic, read-only.

    def test_real_lookup_database_unavailable_falls_back_fast(self):
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        reserved = self.enterContext(socket.socket())
        reserved.bind(("127.0.0.1", 0))  # Reserved, never listening: cannot be any real database.
        missing = DatabaseSettings(url=settings.url.set(host="127.0.0.1", port=reserved.getsockname()[1]),
                                   connect_timeout_seconds=2)
        redis = MemoryRedis()
        _, joined = phase2h.collect([corpus.DOCS["bis_final"], phase2h.FR_WITH_EO], redis)
        corpus.expire(redis, "expire_all")
        baseline, _ = phase2h.collect([phase2h.FR_WITH_EO], MemoryRedis())
        lookup = self.real_lookup(timeout_ms=250, engine_factory=lambda: make_engine(missing))
        started = monotonic()
        with patch.dict(durable_identity._last_log, clear=True), \
             self.assertLogs("geopolitical_durable_identity", level="WARNING") as logs:
            outputs, alone = phase2h.collect([phase2h.FR_WITH_EO], redis, lookup)
        self.assertLess(monotonic() - started, 1.5)
        self.assertEqual(outputs[1], baseline[1])  # Same processing stats as today's resolver.
        self.assertEqual(len(outputs[3]), len(baseline[3]))  # Same delivery behavior.
        self.assertNotIn("mias_test_user", str(logs.output))

    def concurrent_register(self, roots):
        context = multiprocessing.get_context("spawn")
        gate, results = context.Barrier(3), context.Queue()
        prefix = "mias_reg_" + uuid4().hex[:12]
        url = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"])).url.render_as_string(hide_password=False)
        children = [context.Process(target=child_register, args=(url, self.schema, prefix + str(i), root, gate, results))
                    for i, root in enumerate(roots)]
        try:
            with self.engine.connect() as connection:
                # Hold an uncommitted row on the same key so both children block on the unique index.
                transaction_ = connection.begin()
                connection.execute(registry.insert().values(
                    id=str(uuid4()), anchor_type="fr", anchor_value="2026-99901", event_type=STAGE[0],
                    policy_stage=STAGE[1], revision_id=STAGE[2], policy_id="c" * 64, event_key="placeholder",
                    status="active", first_seen_at=phase2h.OBSERVED, last_seen_at=phase2h.OBSERVED,
                    created_at=phase2h.OBSERVED, updated_at=phase2h.OBSERVED, attributes={}))
                for child in children: child.start()
                gate.wait(timeout=15)
                deadline, blocked = monotonic() + 5, 0
                while monotonic() < deadline:
                    with self.admin.connect() as observer:
                        blocked = observer.execute(sa.text("SELECT count(*) FROM pg_stat_activity WHERE application_name "
                            "LIKE :name AND wait_event_type='Lock'"), {"name": prefix + "%"}).scalar_one()
                    if blocked == 2: break
                    sleep(0.01)
                self.assertEqual(blocked, 2, "Both registration processes must contend on real PostgreSQL locks")
                transaction_.rollback()  # Release: the two children now race each other.
            outcomes = [results.get(timeout=15) for _ in children]
            for child in children:
                child.join(timeout=5)
                self.assertEqual(child.exitcode, 0)
            for outcome in outcomes:
                self.assertNotIn("error", outcome)
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

    def test_two_processes_same_anchor_same_root(self):
        outcomes = self.concurrent_register([ROOT_A, ROOT_A])
        self.assertEqual(sorted((o["inserted"], o["already_present"], o["conflicts"]) for o in outcomes), [(0, 1, 0), (1, 0, 0)])
        with transaction(self.engine) as session:
            rows = session.execute(sa.select(registry)).mappings().all()
        self.assertEqual([(r["policy_id"], r["status"]) for r in rows], [(ROOT_A, "active")])

    def test_two_processes_conflicting_roots_no_silent_overwrite(self):
        outcomes = self.concurrent_register([ROOT_A, ROOT_B])
        self.assertEqual(sorted((o["inserted"], o["conflicts"]) for o in outcomes), [(0, 1), (1, 0)])
        with transaction(self.engine) as session:
            [row] = session.execute(sa.select(registry)).mappings().all()
            lookup = AnchorRegistryRepository(session).lookup(anchors=["fr:2026-99901"], stage=STAGE)
        self.assertEqual(row["status"], "conflicted")
        self.assertEqual(sorted([row["policy_id"], *row["attributes"]["conflicting_policy_ids"]]), [ROOT_A, ROOT_B])
        self.assertEqual(lookup["status"], "conflict")
