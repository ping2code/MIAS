"""Phase 2N: disclosure-only promotion, reverse order, material control, historical audit and correction."""
from copy import deepcopy
from datetime import datetime, timedelta
from itertools import permutations
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from persistence import geopolitical_shadow as shadow
from persistence import geopolitical_tools as tools
from persistence.adapters import geopolitical as geo_adapter
from persistence.adapters.macro import source_order_promotion
from persistence.database import transaction
from persistence.geopolitical_audit import audit_geopolitical_identity_divergence
from persistence.geopolitical_disclosure import (
    audit_disclosure_pointers, correct_disclosure_pointers, revert_disclosure_corrections, _compare_and_set,
)
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.reconciliation import reconcile_geopolitical_event
from persistence.repository import EventRepository
from tests import geopolitical_readiness_corpus as corpus
from tests import test_geopolitical_anchor_registry as phase2h
from tests import test_geopolitical_persistence_adapter as adapter_tests
from tests import test_persistence_postgres as foundation
from tests.test_geopolitical_persistence_adapter import sample, NOW
from tests.test_geopolitical_pipeline import MemoryRedis

HISTORY = (event_versions, event_provenance, event_history)


def legacy_promotion(current, candidate):
    """Verbatim pre-Phase-2N rule (disclosure time inside the material comparison), for bug reproduction only."""
    def material(value):
        attrs = value["attributes"]
        facts = {k: attrs[k] for k in geo_adapter.FACTS if k in attrs and k not in geo_adapter.NON_MATERIAL}
        return dict(summary=" ".join(value["summary"].split()), facts=facts, event_type=value["event_type"],
                    market_scope=value["market_scope"], stage=value["stage"], revision_key=value["revision_key"],
                    disclosure=[value["published_at"], value["publication_date"]])
    return source_order_promotion(current, candidate, geo_adapter.IDENTITY_FACTS, material)


def at(event, stamp, **changes):
    result = deepcopy(event)
    result.update(published_at=stamp, **changes)
    return result


class DisclosurePromotionTests(unittest.TestCase):
    setUp = adapter_tests.GeopoliticalAdapterTests.setUp
    count = adapter_tests.GeopoliticalAdapterTests.count
    dump = phase2h.AnchorRegistryTests.dump

    def persist(self, event, observed_at=NOW, **kwargs):
        return shadow.persist_geopolitical(self.engine, event, observed_at, report=True, **kwargs)

    def current(self, version):
        with transaction(self.engine) as session:
            return EventRepository(session).current(version["event_id"])

    def reconcile(self, event, **kwargs):
        with transaction(self.engine) as session:
            return reconcile_geopolitical_event(event, EventRepository(session), **kwargs)

    def disclosure_audit(self, **kwargs):
        with transaction(self.engine) as session:
            if self.engine.dialect.name == "postgresql":
                session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            return audit_disclosure_pointers(session, **kwargs)

    # ------------------------------------------------ primary acceptance (Phase 2M)

    def test_phase2m_scenario_disclosure_only_companion_does_not_replace_current(self):
        redis = MemoryRedis()
        _, joined = phase2h.collect([corpus.DOCS["bis_final"], phase2h.FR_WITH_EO], redis)   # 12:00 BIS + 13:00 FR.
        for event, kwargs in joined:
            self.persist(event, **kwargs)
        original = joined[0][0]
        corpus.expire(redis, "expire_all")                                                    # Redis identity expiry.
        _, alone = phase2h.collect([phase2h.FR_WITH_EO], redis, phase2h.InlineLookup(self.engine))
        companion = alone[0][0]
        self.assertEqual(companion["event_id"], original["event_id"])                         # Durable lookup reuse.
        self.assertEqual(companion["published_at"], "2026-09-22T13:00:00+00:00")
        # Full expiry (incl. the 48h processing cache) means the companion is analyzed as its own
        # document: different document facts, identical action-level substance.
        companion_v = geo_adapter.adapt_geopolitical(companion, NOW)["record"]["normalized"]
        original_v = geo_adapter.adapt_geopolitical(original, NOW)["record"]["normalized"]
        self.assertFalse(geo_adapter.same_document(companion_v, original_v))
        self.assertTrue(geo_adapter.substantively_equal(companion_v, original_v))
        result = self.persist(companion)
        self.assertEqual((result["promotion"], result["duplicate"]), ("disclosure_only", False))
        current = self.current(result["version"])
        self.assertEqual(current["published_at"], datetime.fromisoformat("2026-09-22T12:00:00+00:00"))  # 12:00 stays.
        with transaction(self.engine) as session:
            stamps = sorted(str(v["published_at"]) for v in EventRepository(session).versions(result["version"]["event_id"]))
        self.assertEqual(len(stamps), 2)                                                       # 13:00 kept in history.
        self.assertEqual(self.reconcile(original)["mismatches"], [])
        self.assertEqual(self.reconcile(companion, expect_current=False)["mismatches"], [])
        with transaction(self.engine) as session:
            self.assertEqual(audit_geopolitical_identity_divergence(EventRepository(session))["summary"]["exact_authoritative_anchor"], 0)
        self.assertEqual(self.disclosure_audit()["flagged"], [])

    def test_reverse_order_converges_on_earliest_disclosure(self):
        redis = MemoryRedis()
        _, first = phase2h.collect([phase2h.FR_WITH_EO], redis)                                # 13:00 seen first.
        _, later = phase2h.collect([corpus.DOCS["bis_final"]], redis)                          # 12:00 joins it.
        self.assertEqual(first[0][0]["event_id"], later[0][0]["event_id"])
        self.assertEqual(later[0][0]["published_at"], "2026-09-22T12:00:00+00:00")             # Redis keeps earliest.
        self.assertEqual(self.persist(first[0][0])["promotion"], "first")
        result = self.persist(later[0][0])
        self.assertEqual(result["promotion"], "earlier_disclosure")                            # Not "newer_material".
        self.assertEqual(self.current(result["version"])["id"], result["version"]["id"])
        with transaction(self.engine) as session:
            self.assertEqual(len(EventRepository(session).versions(result["version"]["event_id"])), 2)

    def test_all_observation_orders_converge(self):
        base = sample("trade")
        for index, order in enumerate(permutations(("2026-09-22T11:00:00+00:00", "2026-09-22T12:00:00+00:00",
                                                     "2026-09-22T13:00:00+00:00"))):
            key = f"{base['event_id'][:40]}-{index}"
            for stamp in order:
                row = self.persist(at(base, stamp, event_id=key))["version"]
            self.assertEqual(self.current(row)["published_at"], datetime.fromisoformat("2026-09-22T11:00:00+00:00"), order)

    def test_material_change_with_later_disclosure_still_promotes(self):
        first = self.persist(sample("bis_final"))
        revised = at(sample("bis_final"), "2026-09-22T13:00:00+00:00")
        revised["summary"] += " Synthetic substantive revision of the licensing scope."
        result = self.persist(revised)
        self.assertEqual(result["promotion"], "newer_material")
        self.assertEqual(self.current(first["version"])["id"], result["version"]["id"])
        older_material = at(sample("bis_final"), "2026-09-22T11:00:00+00:00")
        older_material["summary"] += " Different superseded wording."
        self.assertEqual(self.persist(older_material)["promotion"], "older")
        self.assertEqual(self.current(first["version"])["id"], result["version"]["id"])

    def test_cosmetic_duplicate_equal_time_precision_and_missing_disclosure(self):
        base = self.persist(sample("bis_final"))
        self.assertTrue(self.persist(sample("bis_final"))["duplicate"])
        cosmetic = sample("bis_final")
        cosmetic["headline"] += " (page title)"
        self.assertEqual(self.persist(cosmetic)["promotion"], "cosmetic")
        equal_time = sample("bis_final")
        equal_time["summary"] += " Unordered content change."
        self.assertEqual(self.persist(equal_time)["promotion"], "ambiguous")
        dated = at(sample("bis_final"), "2026-09-22T04:00:00+00:00", timestamp_precision="date")
        self.assertEqual(self.persist(dated)["promotion"], "ambiguous")                         # Incomparable precision.
        undated = at(sample("bis_final"), None, timestamp_precision="unknown")
        self.assertEqual(self.persist(undated)["promotion"], "ambiguous")                       # Missing disclosure.
        self.assertEqual(self.current(base["version"])["id"], base["version"]["id"])

    def test_stale_rediscovery_and_later_disclosures_keep_pointer_stable(self):
        base = self.persist(sample("bis_final"))
        stale = self.persist(sample("stale_companion"), NOW + timedelta(days=3), make_current=False)
        self.assertEqual(stale["promotion"], "caller_disabled")
        for hour in (13, 14, 15):
            later = at(sample("bis_final"), f"2026-09-22T{hour}:00:00+00:00")
            self.assertEqual(self.persist(later)["promotion"], "disclosure_only")
        self.assertEqual(self.current(base["version"])["id"], base["version"]["id"])
        with transaction(self.engine) as session:
            self.assertEqual(len(EventRepository(session).versions(base["version"]["event_id"])), 5)

    def test_companion_documents_compare_action_facts_only(self):
        base = self.persist(sample("bis_final"))["version"]                        # BIS analysis 12:00.
        companion = at(sample("stale_companion"), "2026-09-22T13:00:00+00:00")    # FR document of same action.
        self.assertEqual(self.persist(companion)["promotion"], "disclosure_only")
        disagreeing = at(sample("stale_companion"), "2026-09-22T14:00:00+00:00", related_symbols=[], symbols=[])
        self.assertEqual(self.persist(disagreeing)["promotion"], "ambiguous")      # Never ordered by timestamp.
        earlier = at(sample("stale_companion"), "2026-09-22T11:00:00+00:00")
        self.assertEqual(self.persist(earlier)["promotion"], "earlier_disclosure")
        self.assertEqual(self.current(base)["published_at"], datetime.fromisoformat("2026-09-22T11:00:00+00:00"))

    def test_other_stages_remain_distinct_events(self):
        labels = ("bis_final", "fr_companion", "clarification", "amendment", "proposal", "final_of_proposal",
                  "moea_disruption", "whitehouse_action", "whitehouse_fr_companion")
        for label in labels:
            self.persist(sample(label))
        # Companions join their action; stages remain separate collector events.
        self.assertEqual(self.count(events), len(labels) - 2)
        self.assertEqual(self.disclosure_audit()["flagged"], [])

    def test_writer_counts_disclosure_only_as_held_at_info(self):
        from persistence.geopolitical_shadow import GeopoliticalShadowWriter
        from tests.test_treasury_shadow_persistence import FakeEngine
        with patch.object(shadow, "persist_geopolitical", return_value={"duplicate": False, "promotion": "disclosure_only"}):
            writer = GeopoliticalShadowWriter(engine_factory=FakeEngine)
            with self.assertLogs("geopolitical_shadow", level="INFO") as logs:
                writer.submit(at(sample("bis_final"), "2026-09-22T13:00:00+00:00"))
                stats = writer.shutdown(timeout=5)["stats"]
        self.assertEqual((stats["promotion_held"], stats["promotion_disclosure_only"], stats["promotion_ambiguous"]), (1, 1, 0))
        self.assertIn("INFO:geopolitical_shadow:Geopolitical shadow disclosure-only version retained; current unchanged", logs.output)
        self.assertFalse([line for line in logs.output if line.startswith("WARNING")])

    def test_corpus_replay_has_no_flagged_pointers_under_new_rule(self):
        for row in corpus.load_corpus()[1]:
            shadow.persist_geopolitical(self.engine, row["event"], datetime.fromisoformat(row["observed_at"]),
                                        make_current=row["make_current"])
        audit = self.disclosure_audit()
        self.assertEqual((audit["events_scanned"], audit["flagged"]), (14, []))

    # ---------------------------------------------- historical audit + correction

    def legacy_state(self):
        """Recreate a pre-2N pointer: 12:00 then 13:00 (identical content) under the old rule."""
        with patch.object(shadow, "geopolitical_promotion", legacy_promotion):
            base = self.persist(sample("bis_final"))
            later = self.persist(at(sample("bis_final"), "2026-09-22T13:00:00+00:00"))
        self.assertEqual(later["promotion"], "newer_material")  # The Phase 2M defect, reproduced.
        # Unrelated events must not be flagged: a legitimate material newer version and a single-version event.
        self.persist(sample("trade"))
        revised = at(sample("trade"), "2026-09-22T13:00:00+00:00")
        revised["summary"] += " Substantive change."
        self.persist(revised)
        self.persist(sample("sanctions"))
        return base["version"], later["version"]

    def test_historical_audit_flags_only_disclosure_only_promotions_read_only(self):
        base, later = self.legacy_state()
        before = self.dump(HISTORY + (events,))
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.lstrip().upper())
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            with patch("analyzer.deduplicator.redis_client", side_effect=AssertionError("Redis")):
                audit = self.disclosure_audit()
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        self.assertEqual(self.dump(HISTORY + (events,)), before)
        self.assertFalse([s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))])
        [flag] = audit["flagged"]
        self.assertEqual((flag["event_id"], flag["current_version_id"], flag["candidate_version_id"]),
                         (sample("bis_final")["event_id"], later["id"], base["id"]))
        self.assertEqual((flag["current_disclosure"][:16], flag["candidate_disclosure"][:16]),
                         ("2026-09-22 13:00", "2026-09-22 12:00"))
        self.assertEqual((flag["content_identical"], flag["reason"]),
                         (True, "current_not_earliest_disclosure_for_identical_content"))

    def test_correction_dry_run_apply_idempotent_and_revert(self):
        base, later = self.legacy_state()
        history = self.dump(HISTORY)
        with transaction(self.engine) as session:
            dry = correct_disclosure_pointers(session)
        self.assertEqual((dry["mode"], [c["outcome"] for c in dry["changes"]]), ("dry_run", ["proposed"]))
        self.assertEqual(self.current(later)["id"], later["id"])  # Dry run changes nothing.
        with transaction(self.engine) as session:
            applied = correct_disclosure_pointers(session, apply=True)
        self.assertEqual((applied["changed"], self.current(later)["id"]), (1, base["id"]))
        self.assertEqual(self.dump(HISTORY), history)  # Only current_version_id changed.
        with transaction(self.engine) as session:
            self.assertEqual(correct_disclosure_pointers(session, apply=True)["changes"], [])  # Idempotent.
        with transaction(self.engine) as session:
            reverted = revert_disclosure_corrections(session, applied["changes"], apply=True)
        self.assertEqual((reverted["changed"], self.current(later)["id"]), (1, later["id"]))
        with transaction(self.engine) as session:
            again = revert_disclosure_corrections(session, applied["changes"], apply=True)
        self.assertEqual([c["outcome"] for c in again["changes"]], ["already"])
        self.assertEqual(self.dump(HISTORY), history)
        with transaction(self.engine) as session:  # Concurrent pointer change between audit and apply is skipped.
            event_row = session.execute(sa.select(events.c.id).where(events.c.event_key == sample("bis_final")["event_id"])).scalar_one()
            self.assertEqual(_compare_and_set(session, event_row, "stale-expectation", base["id"]), "skipped_changed")

    def test_cli_disclosure_audit_and_correct(self):
        base, later = self.legacy_state()
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            code = tools.main(list(argv), engine=self.engine, out=out, err=err)
            return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()
        code, audit, _ = run("disclosure-audit", "--json")
        self.assertEqual((code, audit["flagged_count"]), (0, 1))
        code, dry, _ = run("disclosure-correct", "--json")
        self.assertEqual((code, dry["mode"], dry["changed"]), (0, "dry_run", 0))
        self.assertEqual(self.current(later)["id"], later["id"])
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        report = os.path.join(directory.name, "changes.json")
        code, applied, _ = run("disclosure-correct", "--json", "--apply", "--changes-out", report)
        self.assertEqual((code, applied["changed"], self.current(later)["id"]), (0, 1, base["id"]))
        self.assertEqual(run("disclosure-correct", "--apply", "--changes-out", report)[0], tools.EXIT_USAGE)  # No overwrite.
        code, reverted, _ = run("disclosure-correct", "--json", "--apply", "--revert", report)
        self.assertEqual((code, reverted["changed"], self.current(later)["id"]), (0, 1, later["id"]))
        self.assertEqual(run("disclosure-correct", "--revert", report, "--changes-out", report)[0], tools.EXIT_USAGE)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLDisclosurePromotionTests(DisclosurePromotionTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema

    def concurrent(self, first, second):
        from tests.test_geopolitical_persistence_postgres import PostgreSQLGeopoliticalOperationsTests
        return PostgreSQLGeopoliticalOperationsTests.concurrent_writers(self, first, second, existing=True)

    def test_concurrent_disclosure_only_and_earlier_disclosure(self):
        base = self.persist(sample("bis_final"))["version"]
        later, earlier = at(sample("bis_final"), "2026-09-22T13:00:00+00:00"), at(sample("bis_final"), "2026-09-22T11:00:00+00:00")
        outcomes = self.concurrent(later, earlier)
        self.assertEqual(sum(o["stats"]["duplicate"] for o in outcomes), 0)
        self.assertEqual(self.current(base)["published_at"], datetime.fromisoformat("2026-09-22T11:00:00+00:00"))
        with transaction(self.engine) as session:
            self.assertEqual(len(EventRepository(session).versions(base["event_id"])), 3)
        self.assertEqual(self.reconcile(earlier)["mismatches"], [])
        self.assertEqual(self.reconcile(later, expect_current=False)["mismatches"], [])

    def test_concurrent_identical_disclosure_only_observations(self):
        self.persist(sample("bis_final"))
        later = at(sample("bis_final"), "2026-09-22T13:00:00+00:00")
        outcomes = self.concurrent(later, later)
        self.assertEqual(sum(o["stats"]["duplicate"] for o in outcomes), 1)
        self.assertEqual(sum(o["stats"]["promotion_disclosure_only"] for o in outcomes), 1)
        self.assertEqual(self.reconcile(sample("bis_final"))["mismatches"], [])

    def test_correction_apply_uses_real_row_lock_and_read_only_dry_run(self):
        base, later = self.legacy_state()
        with transaction(self.engine) as session:
            session.execute(sa.text("SET TRANSACTION READ ONLY"))
            self.assertEqual(correct_disclosure_pointers(session)["changes"][0]["outcome"], "proposed")
        with self.engine.connect() as holder:
            tx = holder.begin()
            holder.execute(sa.select(events).where(events.c.id == later["event_id"]).with_for_update())
            with transaction(self.engine) as session:
                session.execute(sa.text("SET LOCAL lock_timeout = '200ms'"))
                with self.assertRaises(Exception):  # Correction waits on the same row lock the writer uses.
                    correct_disclosure_pointers(session, apply=True)
            tx.rollback()
        with transaction(self.engine) as session:
            self.assertEqual(correct_disclosure_pointers(session, apply=True)["changed"], 1)
        self.assertEqual(self.current(later)["id"], base["id"])
