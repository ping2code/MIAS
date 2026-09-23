"""Phase 2G read-only geopolitical identity-divergence audit, using real collector identity behavior."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from persistence.database import transaction
from persistence.geopolitical_audit import audit_geopolitical_identity_divergence
from persistence.geopolitical_shadow import persist_geopolitical
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.repository import EventRepository
from tests import geopolitical_readiness_corpus as corpus
from tests import test_persistence_postgres as foundation
from tests import test_geopolitical_persistence_adapter as adapter_tests
from tests.test_geopolitical_persistence_adapter import sample
from tests.test_geopolitical_pipeline import MemoryRedis

OBSERVED = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
TABLES = (events, event_versions, event_provenance, event_history)


def diverge(anchor_doc, companion, eo):
    """Real collector: join via a companion, expire Redis, resolve the companion alone."""
    redis = MemoryRedis()
    fr = dict(companion, identity_anchors=[eo])
    _, joined = corpus.run_collector([anchor_doc, fr], redis)
    corpus.expire(redis, "expire_all")
    _, alone = corpus.run_collector([fr], redis)
    return joined, alone


class GeopoliticalIdentityAuditTests(unittest.TestCase):
    setUp = adapter_tests.GeopoliticalAdapterTests.setUp

    def persist(self, submitted):
        for event, kwargs in submitted:
            persist_geopolitical(self.engine, event, OBSERVED, **kwargs)

    def audit(self, **kwargs):
        with transaction(self.engine) as session:
            if self.engine.dialect.name == "postgresql":
                session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            return audit_geopolitical_identity_divergence(EventRepository(session), **kwargs)

    def dump(self):
        with transaction(self.engine) as session:
            return {t.name: sorted(json.dumps(dict(r), sort_keys=True, default=str) for r in session.execute(sa.select(t)).mappings())
                    for t in TABLES}

    def test_empty_database(self):
        report = self.audit()
        self.assertEqual((report["events_scanned"], report["groups"], report["truncated"]), (0, [], False))
        self.assertEqual(set(report["summary"].values()), {0})

    def test_alias_expiry_same_document_resolves_identically_no_group(self):
        redis = MemoryRedis()
        _, first = corpus.run_collector([corpus.DOCS["bis_final"], corpus.DOCS["fr_companion"]], redis)
        self.persist(first)
        corpus.expire(redis, "expire_all")
        _, again = corpus.run_collector([corpus.DOCS["bis_final"]], redis)
        self.assertEqual(again[0][0]["event_id"], first[0][0]["event_id"])
        self.persist(again)
        report = self.audit()
        self.assertEqual((report["events_scanned"], report["groups"]), (1, []))

    def test_companion_divergence_flagged_as_shared_authoritative_anchor(self):
        joined, alone = diverge(corpus.DOCS["bis_final"], corpus.DOCS["fr_companion"], "eo:99980")
        self.assertEqual(joined[0][0]["event_id"], joined[1][0]["event_id"])  # One action before expiry.
        self.assertNotEqual(alone[0][0]["event_id"], joined[0][0]["event_id"])  # Unchanged runtime behavior.
        self.persist(joined + alone)
        report = self.audit()
        self.assertEqual(report["summary"]["exact_authoritative_anchor"], 1)
        [group] = report["groups"]
        self.assertEqual(group["classification"], "exact_authoritative_anchor")
        self.assertEqual(group["event_ids"], sorted({joined[0][0]["event_id"], alone[0][0]["event_id"]}))
        self.assertEqual(group["shared_anchors"], ["fr:2026-99901"])
        self.assertEqual(group["shared_document_ids"], ["fr:2026-99901"])
        self.assertIn("shared_authoritative_anchor_same_stage", group["reasons"])
        self.assertEqual(len(group["policy_ids"]), 2)
        self.assertEqual(group["stages"], [dict(event_type="policy_action", stage="adopted", revision="original")])

    def test_audit_is_read_only(self):
        joined, alone = diverge(corpus.DOCS["bis_final"], corpus.DOCS["fr_companion"], "eo:99980")
        self.persist(joined + alone)
        before = self.dump()
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            with patch("analyzer.deduplicator.redis_client", side_effect=AssertionError("Redis")):
                self.audit()
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        self.assertEqual(self.dump(), before)
        self.assertTrue(statements)
        self.assertTrue(all(s.lstrip().upper().startswith(("SELECT", "BEGIN", "SET TRANSACTION")) for s in statements), statements)

    def test_unrelated_and_distinct_stage_events_not_falsely_grouped(self):
        for row in corpus.load_corpus()[1]:
            persist_geopolitical(self.engine, row["event"], datetime.fromisoformat(row["observed_at"]),
                                 make_current=row["make_current"])
        report = self.audit()
        self.assertEqual(report["events_scanned"], 14)
        self.assertEqual(report["summary"], dict(exact_authoritative_anchor=0, shared_policy_id=0,
                                                 shared_document_id=0, informational_only=1))
        [group] = report["groups"]  # Final rule and its clarification: intentionally distinct stages.
        self.assertEqual((group["reasons"], group["shared_anchors"]), (["shared_anchor_distinct_stages"], ["fr:2026-99901"]))
        self.assertEqual([s["stage"] for s in group["stages"]], ["adopted", "amended"])

    def test_shared_document_id_reused_url_new_instrument(self):
        redis = MemoryRedis()
        _, first = corpus.run_collector([corpus.DOCS["bis_final"]], redis)
        _, reused = corpus.run_collector([dict(corpus.DOCS["bis_final"], identity_anchors=["fr:2026-99999"])], redis)
        self.assertNotEqual(first[0][0]["event_id"], reused[0][0]["event_id"])
        self.persist(first + reused)
        [group] = self.audit()["groups"]
        self.assertEqual(group["classification"], "shared_document_id")
        self.assertEqual(group["shared_document_ids"], ["bis:synthetic-chip-rule"])
        self.assertEqual(group["shared_anchors"], [])

    def test_shared_policy_id(self):
        event = sample("sanctions")
        persist_geopolitical(self.engine, event, OBSERVED)
        forged = deepcopy(event)  # Synthetic history row: only the policy root is shared.
        forged.update(event_id="synthetic-other-event", identity_anchors=["ofac:synthetic-other"],
                      document_id="ofac:synthetic-other", provenance=[])
        persist_geopolitical(self.engine, forged, OBSERVED)
        [group] = self.audit()["groups"]
        self.assertEqual(group["classification"], "shared_policy_id")
        self.assertEqual(group["shared_policy_ids"], [event["policy_id"]])

    def test_multiple_groups_deterministic_ordering(self):
        first = diverge(corpus.DOCS["bis_final"], corpus.DOCS["fr_companion"], "eo:99980")
        # USTR release anchored fr:2026-99920; its FR notice also carries an EO anchor.
        second = diverge(corpus.DOCS["trade"], corpus.DOCS["earlier_companion_disclosure"], "eo:99990")
        self.persist(second[0] + second[1] + first[0] + first[1])
        report = self.audit()
        self.assertEqual(report["summary"]["exact_authoritative_anchor"], 2)
        self.assertEqual(sorted(g["shared_anchors"][0] for g in report["groups"]), ["fr:2026-99901", "fr:2026-99920"])
        self.assertEqual(len({e for g in report["groups"] for e in g["event_ids"]}), 4)
        self.assertEqual(report, self.audit())
        self.assertEqual(report["groups"], sorted(report["groups"], key=lambda g: g["event_ids"]))
        json.dumps(report)  # Safe for later CLI/dashboard serialization.

    def test_bounded_scan_and_argument_validation(self):
        joined, alone = diverge(corpus.DOCS["bis_final"], corpus.DOCS["fr_companion"], "eo:99980")
        self.persist(joined + alone)
        report = self.audit(max_events=1)
        self.assertEqual((report["events_scanned"], report["truncated"], report["groups"]), (1, True, []))
        for bad in (0, -1, True, 100_001, "10"):
            with self.assertRaises(ValueError):
                self.audit(max_events=bad)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLGeopoliticalIdentityAuditTests(GeopoliticalIdentityAuditTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema
