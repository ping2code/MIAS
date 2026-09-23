"""Phase 2I: operational CLI, paged audit, conflict listing, status, acceptance snapshots, rollout."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic import command

from persistence import geopolitical_durable_identity as durable_identity
from persistence import geopolitical_tools as tools
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.geopolitical_audit import (
    audit_geopolitical_identity_divergence, capture_identity_audit_snapshot, compare_identity_audits,
)
from persistence.geopolitical_registry import AnchorRegistryRepository
from persistence.geopolitical_shadow import persist_geopolitical
from persistence.models import events, event_versions, event_provenance, event_history, geopolitical_anchor_registry as registry
from persistence.repository import EventRepository
from tests import geopolitical_readiness_corpus as corpus
from tests import test_geopolitical_anchor_registry as phase2h
from tests import test_geopolitical_persistence_adapter as adapter_tests
from tests import test_persistence as unit
from tests import test_persistence_postgres as foundation
from tests.test_geopolitical_persistence_adapter import sample
from tests.test_geopolitical_pipeline import MemoryRedis

ROOT = Path(__file__).resolve().parents[1]
OBSERVED = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
TABLES = (events, event_versions, event_provenance, event_history, registry)
TRADE_WITH_EO = dict(corpus.DOCS["earlier_companion_disclosure"], identity_anchors=["eo:99990"])


class GeopoliticalToolsTests(unittest.TestCase):
    setUp = adapter_tests.GeopoliticalAdapterTests.setUp
    count = adapter_tests.GeopoliticalAdapterTests.count
    dump = phase2h.AnchorRegistryTests.dump

    def run_tool(self, *argv, engine=None):
        out, err = io.StringIO(), io.StringIO()
        code = tools.main(list(argv), engine=engine or self.engine, out=out, err=err)
        body = out.getvalue()
        return code, (json.loads(body) if "--json" in argv and body else body), err.getvalue()

    def persist(self, submitted):
        for event, kwargs in submitted:
            persist_geopolitical(self.engine, event, OBSERVED, **kwargs)

    def persist_corpus(self):
        for row in corpus.load_corpus()[1]:
            persist_geopolitical(self.engine, row["event"], datetime.fromisoformat(row["observed_at"]),
                                 make_current=row["make_current"])

    def diverge(self, base, companion, lookup=None):
        """Real collector: join, persist, expire Redis, resolve the companion alone, persist."""
        redis = MemoryRedis()
        _, joined = phase2h.collect([base, companion], redis)
        self.persist(joined)
        corpus.expire(redis, "expire_all")
        _, alone = phase2h.collect([companion], redis, lookup)
        self.persist(alone)
        return joined, alone

    def all_statements(self, callback):
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.lstrip().upper())
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            callback()
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        return statements

    def downgrade_registry(self):
        with self.engine.begin() as connection:
            command.downgrade(unit.migration_config(connection), "0002_macro_shadow_history")

    # ------------------------------------------------------------- CLI basics

    def test_invalid_arguments_are_usage_errors(self):
        for argv in ([], ["bogus"], ["conflicts", "--limit", "0"], ["audit", "--page-size", "x"],
                     ["audit", "--max-events", "999999999"], ["conflicts", "--after", "!!notbase64"],
                     ["audit", "--save-snapshot", "a", "--compare-to", "b"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_tool(*argv)
                self.assertEqual(code, tools.EXIT_USAGE)
                self.assertTrue(err.startswith("usage error:"))
                self.assertLess(len(err), 400)

    def test_expected_revision_matches_migration_head(self):
        from alembic.script import ScriptDirectory
        self.assertEqual(ScriptDirectory.from_config(unit.migration_config()).get_current_head(), tools.EXPECTED_REVISION)

    def test_import_is_inert(self):
        code = ("import sys, persistence.geopolitical_tools; "
                "print(*[m in sys.modules for m in ('dotenv', 'shared.config', 'redis', 'analyzer.deduplicator', 'psycopg')])")
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT, env=env)
        self.assertEqual(result.stdout.split(), ["False"] * 5, result.stderr)

    # ------------------------------------------------------------------ status

    def test_status_empty_healthy_database_and_switches(self):
        with patch.dict(os.environ, {"GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED": "TRUE"}):
            code, status, _ = self.run_tool("status", "--json")
        self.assertEqual(code, tools.EXIT_OK)
        self.assertTrue(status["healthy"])
        self.assertEqual((status["alembic_revision"], status["registry_table"]), (tools.EXPECTED_REVISION, True))
        self.assertEqual((status["registry"]["rows"], status["registry"]["conflicted"]), (0, 0))
        self.assertEqual(set(status["audit"]["summary"].values()), {0})
        self.assertIs(status["switches"]["GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED"], True)
        self.assertIs(status["switches"]["GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED"], False)
        self.assertIn("process", status["durable_lookup_counters"]["scope"])
        code, text, _ = self.run_tool("status")
        self.assertIn("healthy: True", text)

    def test_status_with_registry_rows_and_conflicts(self):
        self.diverge(corpus.DOCS["bis_final"], phase2h.FR_WITH_EO)  # Disabled: historical divergence -> conflict.
        code, status, _ = self.run_tool("status", "--json")
        self.assertEqual(code, tools.EXIT_OK)
        self.assertEqual(status["registry"]["conflicted"], 1)
        self.assertEqual(status["registry"]["active"], status["registry"]["rows"] - 1)
        self.assertIsNotNone(status["registry"]["latest_updated_at"])
        self.assertEqual(status["audit"]["summary"]["exact_authoritative_anchor"], 1)
        self.assertTrue(status["warnings"])

    def test_missing_registry_table_and_revision_behind(self):
        self.persist_corpus()
        self.downgrade_registry()
        code, status, _ = self.run_tool("status", "--json")
        self.assertEqual(code, tools.EXIT_FAILED)
        self.assertEqual((status["alembic_revision"], status["registry_table"], status["registry"]),
                         ("0002_macro_shadow_history", False, None))
        self.assertEqual(len(status["issues"]), 2)
        for argv in (("backfill",), ("conflicts",)):
            code, _, err = self.run_tool(*argv)
            self.assertEqual(code, tools.EXIT_SCHEMA)
            self.assertIn("0002_macro_shadow_history", err)
        code, report, _ = self.run_tool("audit", "--json")  # Audit needs only history tables.
        self.assertEqual((code, report["events_scanned"]), (tools.EXIT_OK, 14))

    def test_empty_unmigrated_database(self):
        engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        self.addCleanup(engine.dispose)
        code, status, _ = self.run_tool("status", "--json", engine=engine)
        self.assertEqual((code, status["alembic_revision"], status["history_tables"], status["audit"]),
                         (tools.EXIT_FAILED, None, False, None))

    def test_database_unavailable_is_bounded_and_credential_free(self):
        broken = make_engine(DatabaseSettings(url="sqlite:////nonexistent-mias-dir/x/db.sqlite", sqlite_enabled=True))
        self.addCleanup(broken.dispose)
        for argv in (("status",), ("audit",), ("conflicts",), ("backfill",)):
            code, _, err = self.run_tool(*argv, engine=broken)
            self.assertEqual(code, tools.EXIT_DATABASE)
            self.assertIn("database unavailable", err)
        reserved = self.enterContext(socket.socket())
        reserved.bind(("127.0.0.1", 0))  # Never listening: cannot be a real database.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DATABASE_URL", "TEST_DATABASE_URL"))}
        env["DATABASE_URL"] = f"postgresql://mias_user:TopSecretPassword@127.0.0.1:{reserved.getsockname()[1]}/mias_test"
        env["DB_CONNECT_TIMEOUT_SECONDS"] = "2"
        result = subprocess.run([sys.executable, "-m", "persistence.geopolitical_tools", "status", "--json"],
                                capture_output=True, text=True, timeout=60, cwd=ROOT, env=env)
        self.assertEqual(result.returncode, tools.EXIT_DATABASE)
        self.assertNotIn("TopSecretPassword", result.stdout + result.stderr)
        self.assertNotIn("mias_user", result.stdout + result.stderr)

    # ---------------------------------------------------------------- backfill

    def test_backfill_first_repeat_and_history_untouched(self):
        self.persist_corpus()
        with transaction(self.engine) as session:
            session.execute(registry.delete())
        history = self.dump((events, event_versions, event_provenance, event_history))
        code, first, _ = self.run_tool("backfill", "--json")
        self.assertEqual(code, tools.EXIT_OK)
        self.assertEqual((first["anchors_seen"], first["inserted"], first["already_present"], first["conflicts"],
                          first["skipped_non_authoritative"]), (16, 14, 2, 0, 0))
        code, again, _ = self.run_tool("backfill", "--json")
        self.assertEqual((again["inserted"], again["already_present"]), (0, 16))
        self.assertEqual(self.dump((events, event_versions, event_provenance, event_history)), history)
        code, text, _ = self.run_tool("backfill")
        self.assertIn("inserted: 0", text)

    def test_backfill_conflict_and_skipped_anchor(self):
        self.diverge(corpus.DOCS["bis_final"], phase2h.FR_WITH_EO)
        forged = deepcopy(sample("sanctions"))
        forged["identity_anchors"] = ["ofac:20260922", "rin:0694-AJ00"]
        self.persist([(forged, {})])
        with transaction(self.engine) as session:
            session.execute(registry.delete())
        code, summary, _ = self.run_tool("backfill", "--json")
        self.assertEqual((code, summary["conflicts"], summary["skipped_non_authoritative"]), (tools.EXIT_OK, 1, 1))

    def test_backfill_writes_only_the_registry(self):
        self.persist_corpus()
        statements = self.all_statements(lambda: self.run_tool("backfill"))
        writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"))]
        self.assertTrue(all("GEOPOLITICAL_ANCHOR_REGISTRY" in s for s in writes), writes)

    # --------------------------------------------------------------- conflicts

    def conflicted(self, count):
        with transaction(self.engine) as session:
            repo = AnchorRegistryRepository(session)
            for index in range(count):
                for root in (phase2h.ROOT_A, phase2h.ROOT_B):
                    repo.register(anchors=[f"fr:2026-{20000 + index}"], stage=phase2h.STAGE, policy_id=root,
                                  event_key="event-" + root[:4], observed_at=OBSERVED + timedelta(minutes=index))

    def test_conflicts_empty_and_fields(self):
        code, listing, _ = self.run_tool("conflicts", "--json")
        self.assertEqual((code, listing["count"], listing["next_cursor"]), (tools.EXIT_OK, 0, None))
        self.conflicted(1)
        _, listing, _ = self.run_tool("conflicts", "--json")
        [row] = listing["conflicts"]
        self.assertEqual((row["anchor_type"], row["anchor_value"]), ("fr", "2026-20000"))
        self.assertEqual(row["stage"], dict(event_type="policy_action", policy_stage="adopted", revision_id="original"))
        self.assertEqual(row["current_root"]["policy_id"], phase2h.ROOT_A)  # Preserved; no winner chosen.
        self.assertEqual(row["conflicting_policy_ids"], [phase2h.ROOT_B])
        self.assertEqual(row["conflicting_event_keys"], ["event-bbbb"])
        self.assertTrue(row["first_seen_at"] and row["last_seen_at"])

    def test_conflicts_paging_deterministic_and_read_only(self):
        self.conflicted(5)
        before = self.dump(TABLES)
        seen, cursor, pages = [], None, 0
        def page_through():
            nonlocal cursor, pages
            while True:
                argv = ["conflicts", "--json", "--limit", "2"] + (["--after", cursor] if cursor else [])
                code, listing, _ = self.run_tool(*argv)
                self.assertEqual(code, tools.EXIT_OK)
                seen.extend(r["anchor_value"] for r in listing["conflicts"])
                pages += 1
                cursor = listing["next_cursor"]
                if not cursor:
                    break
        statements = self.all_statements(page_through)
        self.assertEqual(pages, 3)
        self.assertEqual(seen, [f"2026-{20000 + i}" for i in range(5)])
        self.assertEqual(self.dump(TABLES), before)
        self.assertFalse([s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))])

    # ------------------------------------------------------------------- audit

    def test_audit_empty_one_and_multiple_groups(self):
        code, report, _ = self.run_tool("audit", "--json")
        self.assertEqual((code, report["groups"], report["total_groups"], report["next_cursor"]), (0, [], 0, None))
        self.diverge(corpus.DOCS["bis_final"], phase2h.FR_WITH_EO)
        _, report, _ = self.run_tool("audit", "--json")
        [group] = report["groups"]
        self.assertEqual(group["classification"], "exact_authoritative_anchor")
        for field in ("event_ids", "policy_ids", "shared_anchors", "shared_document_ids", "provenance_urls", "reasons"):
            self.assertTrue(group[field], field)
        self.diverge(corpus.DOCS["trade"], TRADE_WITH_EO)
        _, report, _ = self.run_tool("audit", "--json")
        self.assertEqual(report["summary"]["exact_authoritative_anchor"], 2)
        _, text, _ = self.run_tool("audit")
        self.assertIn("exact_authoritative_anchor", text)
        self.assertNotIn("duplicate", text.lower())

    def test_audit_paging_matches_full_report_and_is_deterministic(self):
        self.diverge(corpus.DOCS["bis_final"], phase2h.FR_WITH_EO)
        self.diverge(corpus.DOCS["trade"], TRADE_WITH_EO)
        persist_geopolitical(self.engine, sample("clarification"), OBSERVED)  # Cross-stage informational.
        with transaction(self.engine) as session:
            full = audit_geopolitical_identity_divergence(EventRepository(session))["groups"]
        paged, cursor = [], None
        while True:
            code, page, _ = self.run_tool("audit", "--json", "--page-size", "1", *(["--after", cursor] if cursor else []))
            self.assertEqual((code, page["total_groups"]), (0, len(full)))
            paged.extend(page["groups"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(paged, full)
        self.assertEqual(len(full), 3)
        code, _, err = self.run_tool("audit", "--after", "geo-divergence:unknown")
        self.assertEqual(code, tools.EXIT_USAGE)
        self.assertIn("cursor", err)

    def test_keyset_scan_over_larger_history(self):
        base = sample("sanctions")
        for index in range(120):
            event = deepcopy(base)
            event.update(event_id=f"synthetic-event-{index:03d}", policy_id=f"{index:064x}",
                         identity_anchors=[f"ofac:2026{index:04d}"], document_id=f"ofac:2026{index:04d}",
                         provenance=[])
            persist_geopolitical(self.engine, event, OBSERVED + timedelta(seconds=index))
        self.diverge(corpus.DOCS["bis_final"], phase2h.FR_WITH_EO)
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            reference = audit_geopolitical_identity_divergence(repo)
            for batch in (1, 7, 50):
                self.assertEqual(audit_geopolitical_identity_divergence(repo, batch_size=batch), reference, batch)
            bounded = audit_geopolitical_identity_divergence(repo, max_events=50, batch_size=7)
        self.assertEqual((reference["events_scanned"], reference["truncated"]), (122, False))
        self.assertEqual((bounded["events_scanned"], bounded["truncated"]), (50, True))
        self.assertEqual(reference["summary"]["exact_authoritative_anchor"], 1)  # No false grouping at scale.
        self.assertEqual(sum(reference["summary"].values()), 1)

    def test_later_distinct_stage_event_cannot_mask_historical_divergence(self):
        self.diverge(corpus.DOCS["bis_final"], phase2h.FR_WITH_EO)
        with transaction(self.engine) as session:
            before = capture_identity_audit_snapshot(EventRepository(session))
        persist_geopolitical(self.engine, sample("clarification"), OBSERVED)  # Amended stage, same anchor.
        with transaction(self.engine) as session:
            after = capture_identity_audit_snapshot(EventRepository(session))
        comparison = compare_identity_audits(before, after)
        self.assertEqual(after["summary"]["exact_authoritative_anchor"], 1)
        self.assertEqual(comparison["removed_groups"], [])
        self.assertEqual((comparison["new_exact_authoritative_anchor"], comparison["accepted"]), (0, True))
        self.assertEqual(comparison["new_by_classification"]["informational_only"], 1)

    # --------------------------------------------------- snapshots/acceptance

    def snapshot_file(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return os.path.join(directory.name, "baseline.json")

    def test_snapshot_save_refuses_overwrite_and_bad_baseline(self):
        path = self.snapshot_file()
        self.assertEqual(self.run_tool("audit", "--save-snapshot", path)[0], tools.EXIT_OK)
        code, _, err = self.run_tool("audit", "--save-snapshot", path)
        self.assertEqual(code, tools.EXIT_USAGE)
        Path(path + ".bad").write_text("{not json")
        self.assertEqual(self.run_tool("audit", "--compare-to", path + ".bad")[0], tools.EXIT_USAGE)
        Path(path + ".v0").write_text(json.dumps({"snapshot_version": 0}))
        self.assertEqual(self.run_tool("audit", "--compare-to", path + ".v0")[0], tools.EXIT_USAGE)

    def test_historical_versus_new_divergence(self):
        self.diverge(corpus.DOCS["trade"], TRADE_WITH_EO)  # Historical.
        path = self.snapshot_file()
        self.run_tool("audit", "--save-snapshot", path)
        code, unchanged, _ = self.run_tool("audit", "--json", "--compare-to", path)
        self.assertEqual((code, unchanged["historical_groups"], unchanged["accepted"]), (tools.EXIT_OK, 1, True))
        self.diverge(corpus.DOCS["bis_final"], phase2h.FR_WITH_EO)  # New divergence (lookup disabled).
        code, comparison, _ = self.run_tool("audit", "--json", "--compare-to", path)
        self.assertEqual(code, tools.EXIT_FAILED)
        self.assertEqual((comparison["historical_groups"], comparison["new_exact_authoritative_anchor"]), (1, 1))
        self.assertEqual(comparison["new_groups"][0]["shared_anchors"], ["fr:2026-99901"])

    def test_truncated_snapshot_is_inconclusive(self):
        self.diverge(corpus.DOCS["bis_final"], phase2h.FR_WITH_EO)
        with transaction(self.engine) as session:
            full = capture_identity_audit_snapshot(EventRepository(session))
            partial = capture_identity_audit_snapshot(EventRepository(session), max_events=1)
        result = compare_identity_audits(full, partial)
        self.assertEqual((result["inconclusive"], result["accepted"]), (True, False))

    # ----------------------------------------------------------------- rollout

    def lookup(self):
        """SQLite-safe inline lookup; the PostgreSQL subclass uses the real bounded service."""
        return phase2h.InlineLookup(self.engine)

    def rollout(self, enabled):
        # 1-2: reachable, migration current.
        code, status, _ = self.run_tool("status", "--json")
        self.assertEqual((code, status["revision_current"]), (tools.EXIT_OK, True))
        # 3: shadow persistence has recorded history, including one pre-2H divergence.
        self.diverge(corpus.DOCS["trade"], TRADE_WITH_EO)
        redis = MemoryRedis()
        _, joined = phase2h.collect([corpus.DOCS["bis_final"], phase2h.FR_WITH_EO], redis)
        self.persist(joined)
        with transaction(self.engine) as session:
            session.execute(registry.delete())  # History predates the registry.
        # 4: backfill once. 5: inspect conflicts. 6: baseline audit.
        code, summary, _ = self.run_tool("backfill", "--json")
        # Attempts depend on which historical root registers first (tie on first_seen_at is
        # broken by row id); the conflicted anchor set below is invariant.
        self.assertEqual(code, tools.EXIT_OK)
        self.assertGreaterEqual(summary["conflicts"], 1)
        code, listing, _ = self.run_tool("conflicts", "--json")
        self.assertEqual([c["anchor_value"] for c in listing["conflicts"]], ["2026-99920"])
        path = self.snapshot_file()
        self.assertEqual(self.run_tool("audit", "--save-snapshot", path)[0], tools.EXIT_OK)
        # 7-8: enable lookup (or not) and exercise the known post-expiry companion traffic.
        corpus.expire(redis, "expire_all")
        lookup = self.lookup() if enabled else None
        before = durable_identity.get_durable_identity_stats()
        _, alone = phase2h.collect([phase2h.FR_WITH_EO], redis, lookup)
        self.persist(alone)
        after = durable_identity.get_durable_identity_stats()
        # 10-11: rerun audit against the baseline.
        code, comparison, _ = self.run_tool("audit", "--json", "--compare-to", path)
        return joined, alone, code, comparison, lookup, before, after

    def test_rollout_enabled_no_new_exact_divergence(self):
        joined, alone, code, comparison, lookup, *_ = self.rollout(enabled=True)
        self.assertEqual(alone[0][0]["event_id"], joined[0][0]["event_id"])
        self.assertEqual(code, tools.EXIT_OK)
        self.assertEqual((comparison["new_exact_authoritative_anchor"], comparison["historical_groups"],
                          comparison["accepted"]), (0, 1, True))
        return lookup

    def test_rollout_disabled_reproduces_prior_behavior(self):
        joined, alone, code, comparison, *_ = self.rollout(enabled=False)
        self.assertNotEqual(alone[0][0]["event_id"], joined[0][0]["event_id"])
        self.assertEqual((code, comparison["new_exact_authoritative_anchor"], comparison["accepted"]),
                         (tools.EXIT_FAILED, 1, False))

    def test_toggling_lookup_off_is_sufficient_rollback(self):
        self.test_rollout_enabled_no_new_exact_divergence()
        rows = self.count(registry)
        history = self.dump((events, event_versions, event_provenance, event_history))
        # Rollback = switch off. collect(lookup=None) proves the lookup is never touched.
        redis = MemoryRedis()
        _, first = phase2h.collect([corpus.DOCS["entity_list"]], redis)
        self.assertTrue(first)
        with patch.dict(os.environ, {"GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED": "false"}):
            code, status, _ = self.run_tool("status", "--json")
        self.assertEqual((code, status["switches"]["GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED"]), (0, False))
        # No data deletion: registry and history intact; schema unchanged.
        self.assertEqual(self.count(registry), rows)
        self.assertEqual(self.dump((events, event_versions, event_provenance, event_history)), history)
        self.assertEqual(status["alembic_revision"], tools.EXPECTED_REVISION)
        # Runtime is back to the pre-2H resolver: the known scenario diverges again when disabled.
        joined, alone = self.diverge(corpus.DOCS["bis_final"], phase2h.FR_WITH_EO)
        self.assertNotEqual(alone[0][0]["event_id"], joined[0][0]["event_id"])


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLGeopoliticalToolsTests(GeopoliticalToolsTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema

    def lookup(self):
        lookup = durable_identity.DurableIdentityLookup(timeout_ms=2000, engine_factory=lambda: self.engine)
        self.addCleanup(lookup.close)
        return lookup

    def test_rollout_enabled_no_new_exact_divergence(self):
        joined, alone, code, comparison, lookup, before, after = self.rollout(enabled=True)
        self.assertEqual(alone[0][0]["event_id"], joined[0][0]["event_id"])
        self.assertEqual((code, comparison["new_exact_authoritative_anchor"], comparison["accepted"]), (0, 0, True))
        # 9: observe counters from the real bounded lookup.
        self.assertEqual((after["lookup_hit"] - before["lookup_hit"], after["lookup_error"] - before["lookup_error"],
                          after["lookup_timeout"] - before["lookup_timeout"]), (1, 0, 0))

    def test_empty_unmigrated_database(self):
        from uuid import uuid4
        from persistence.config import require_test_database
        schema = "mias_phase2a_" + uuid4().hex  # Same guarded naming as the foundation tests.
        with self.admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        engine = make_engine(require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"])))
        @sa.event.listens_for(engine, "connect")
        def path(connection, _):
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
            connection.autocommit = False
        try:
            code, status, _ = self.run_tool("status", "--json", engine=engine)
            self.assertEqual((code, status["alembic_revision"], status["history_tables"], status["audit"]),
                             (tools.EXIT_FAILED, None, False, None))
            self.assertEqual(self.run_tool("backfill", engine=engine)[0], tools.EXIT_SCHEMA)
        finally:
            engine.dispose()
            with self.admin.begin() as connection:
                connection.execute(sa.schema.DropSchema(schema, cascade=True))

    def test_read_commands_run_in_read_only_transactions(self):
        seen = []
        original = tools._session
        from contextlib import contextmanager
        @contextmanager
        def spy(engine, *, read_only):
            with original(engine, read_only=read_only) as session:
                seen.append((read_only, session.execute(sa.text("SHOW transaction_read_only")).scalar_one()))
                yield session
        with patch.object(tools, "_session", spy):
            for argv in (("status",), ("audit",), ("conflicts",)):
                self.assertEqual(self.run_tool(*argv)[0], tools.EXIT_OK)
            self.run_tool("backfill")
        self.assertEqual({flag for read_only, flag in seen if read_only}, {"on"})
        self.assertEqual([flag for read_only, flag in seen if not read_only], ["off"])  # Backfill only.
