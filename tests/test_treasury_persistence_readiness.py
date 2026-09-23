"""Treasury readiness contract: fixed replay, late arrival, safe promotion, read-only audit."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from persistence import treasury_shadow as shadow
from persistence.database import transaction
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.reconciliation import reconcile_treasury_event
from persistence.repository import EventRepository
from tests import treasury_readiness_corpus as corpus
from tests import test_treasury_persistence_adapter as adapter
from tests.test_treasury_persistence_adapter import sample, NOW


def replay(engine):
    _, rows = corpus.load_corpus()
    duplicates, promotions = 0, Counter()
    for row in rows:
        result = shadow.persist_treasury(engine, row["event"], datetime.fromisoformat(row["observed_at"]),
                                         make_current=row["make_current"], report=True)
        duplicates += int(result["duplicate"])
        promotions[result["promotion"]] += 1
    summary = reconcile_corpus(engine, duplicates)
    summary["promotions"] = dict(promotions)
    return summary


def reconcile_corpus(engine, duplicates=None):
    manifest, rows = corpus.load_corpus()
    results = []
    with transaction(engine) as session:
        if engine.dialect.name == "postgresql":
            session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        repo = EventRepository(session)
        for row in rows:
            result = reconcile_treasury_event(row["event"], repo, expect_current=row["expect_current"])
            results.append(dict(label=row["label"], **result))
        counts = {table.name: session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
                  for table in (events, event_versions, event_provenance, event_history)}
    mismatches = [{"label": r["label"], "fields": r["mismatches"]} for r in results if r["mismatches"]]
    return dict(corpus_version=manifest["corpus_version"], observations=len(rows),
                matched=len(rows) - len(mismatches), mismatches=mismatches,
                duplicates=duplicates, counts=counts, results=results)


class TreasuryReadinessTests(unittest.TestCase):
    setUp = adapter.TreasuryAdapterTests.setUp

    def persist(self, event, observed_at=NOW, **kwargs):
        return shadow.persist_treasury(self.engine, event, observed_at, report=True, **kwargs)

    def current(self, row):
        with transaction(self.engine) as session:
            return EventRepository(session).current(row["event_id"])

    def reconcile(self, event, **kwargs):
        with transaction(self.engine) as session:
            return reconcile_treasury_event(event, EventRepository(session), **kwargs)

    def test_fixed_corpus_is_reproducible_and_covers_families(self):
        manifest, rows = corpus.load_corpus()
        self.assertEqual((manifest, rows), corpus.load_corpus())
        self.assertEqual(len(rows), manifest["expected_observations"])
        self.assertEqual({r["event"]["treasury_category"] for r in rows}, set(manifest["families"]))
        self.assertTrue(all(r["event"]["symbols"] == [] for r in rows))  # No symbols are manufactured.
        self.assertTrue(all(r["event"].get("quality_adjustment", 0) == 0 for r in rows))
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        expected = corpus.MANIFEST.with_suffix(".sha256").read_text().strip()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), expected)

    def test_replay_full_read_only_reconciliation_and_repeat(self):
        manifest, rows = corpus.load_corpus()
        result = replay(self.engine)
        self.assertEqual(result["mismatches"], [])
        self.assertEqual(result["matched"], manifest["expected_observations"])
        self.assertEqual(result["duplicates"], manifest["expected_duplicates"])
        self.assertEqual(result["counts"], manifest["expected_counts"])
        self.assertEqual(result["promotions"], manifest["expected_promotions"])
        again = replay(self.engine)
        self.assertEqual(again["mismatches"], [])
        self.assertEqual(again["duplicates"], len(rows))
        self.assertEqual(again["counts"], result["counts"])
        self.assertEqual(again["promotions"], {"duplicate": len(rows)})
        print("PHASE2E_RECONCILIATION " + json.dumps({k: v for k, v in result.items() if k != "results"}, sort_keys=True))

    def test_expected_outcome_history_per_family(self):
        replay(self.engine)
        _, rows = corpus.load_corpus()
        by_label = {r["label"]: r for r in reconcile_corpus(self.engine)["results"]}
        for row in rows:
            event, result = row["event"], by_label[row["label"]]
            self.assertEqual(result["score_match"], True if "impact_score" in event else None, row["label"])
            self.assertEqual(result["decision_match"], True if "alert_decision" in event else None, row["label"])
            self.assertEqual(result["ai_match"], True if "ai_summary" in event else None, row["label"])

    def test_newer_first_unseen_older_arrives_late(self):
        newer = self.persist(sample("refunding:material"))
        older = self.persist(sample("refunding:older"), NOW + timedelta(days=1))
        self.assertEqual(older["promotion"], "older")
        self.assertEqual(self.current(newer["version"])["id"], newer["version"]["id"])

    def test_material_with_explicit_source_chronology_promotes_regardless_of_arrival(self):
        first = self.persist(sample())
        newer = self.persist(sample("refunding:material"), NOW - timedelta(days=1))
        self.assertEqual(newer["promotion"], "newer_material")
        self.assertEqual(self.current(first["version"])["id"], newer["version"]["id"])

    def test_both_arrival_orders_select_same_material_version(self):
        for order in (("refunding:initial", "refunding:material"), ("refunding:material", "refunding:initial")):
            for label in order:
                event = sample(label)
                event["event_id"] += "-" + order[0]  # Separate synthetic anchor per permutation.
                row = self.persist(event)["version"]
            self.assertEqual(self.current(row)["summary"], sample("refunding:material")["summary"])

    def test_cosmetic_and_duplicate_never_promote(self):
        first = self.persist(sample("refunding:material"))
        cosmetic = self.persist(sample("refunding:cosmetic"), NOW + timedelta(days=1))
        self.assertEqual(cosmetic["promotion"], "cosmetic")
        older = self.persist(sample("refunding:initial"))
        self.assertEqual(older["promotion"], "older")
        older_again = self.persist(sample("refunding:initial"), NOW + timedelta(days=5))
        self.assertTrue(older_again["duplicate"])
        self.assertEqual(older_again["promotion"], "duplicate")
        self.assertEqual(self.current(first["version"])["id"], first["version"]["id"])
        self.assertNotEqual(older["version"]["id"], first["version"]["id"])

    def test_stale_repost_and_missing_date_do_not_refresh_or_promote(self):
        current = self.persist(sample())
        for label in ("refunding:stale_repost", "refunding:missing_date"):
            result = self.persist(sample(label), NOW + timedelta(days=30), make_current=False)
            self.assertIn(result["promotion"], {"duplicate", "caller_disabled"})
            self.assertEqual(self.current(current["version"])["id"], current["version"]["id"])
        # Rediscovery never changes source publication facts of the current version.
        self.assertEqual(self.current(current["version"])["published_at"], current["version"]["published_at"])

    def test_stale_first_observation_is_retained_not_alertable(self):
        stale = self.persist(sample("borrowing_estimates:stale"), make_current=False)
        self.assertEqual(stale["promotion"], "first")
        with transaction(self.engine) as session:
            self.assertEqual(EventRepository(session).history(stale["version"]["id"]), [])  # No score/decision invented.

    def test_unverified_yield_revision_never_gains_order_or_freshness(self):
        first = self.persist(sample("yield:routine"))
        revised = self.persist(sample("yield:unverified_revision"), NOW + timedelta(days=1))
        self.assertEqual(revised["promotion"], "ambiguous")
        self.assertIsNone(revised["version"]["published_at"])
        self.assertEqual(revised["version"]["timestamp_precision"], "unknown")
        self.assertEqual(self.current(first["version"])["id"], first["version"]["id"])

    def test_auction_result_update_without_source_order_is_held(self):
        first = self.persist(sample("bill_auction:result"))
        update = self.persist(sample("bill_auction:unordered_result_update"), NOW + timedelta(days=1))
        self.assertEqual(update["promotion"], "ambiguous")
        self.assertEqual(self.current(first["version"])["id"], first["version"]["id"])

    def test_announcement_never_replaced_by_result(self):
        announcement = self.persist(sample("bill_auction:announcement"))
        self.persist(sample("bill_auction:result"))
        self.assertEqual(self.current(announcement["version"])["id"], announcement["version"]["id"])
        self.assertEqual(self.current(announcement["version"])["stage"], "announcement")

    def test_explicit_correction_is_distinct_collector_identity(self):
        original = self.persist(sample())
        correction = self.persist(sample("refunding:explicit_correction"))
        self.assertEqual(correction["promotion"], "first")
        self.assertNotEqual(original["version"]["event_id"], correction["version"]["event_id"])
        self.assertEqual(self.current(original["version"])["id"], original["version"]["id"])

    def test_changed_identity_facts_are_ambiguous_not_merged(self):
        first = self.persist(sample())
        changed = sample("refunding:material")
        changed["treasury_category"] = "issuance_policy"
        result = self.persist(changed)
        self.assertEqual(result["promotion"], "ambiguous")
        self.assertEqual(self.current(first["version"])["id"], first["version"]["id"])

    def test_reconciliation_success_no_ai_not_applicable(self):
        event = sample("note_auction:result")
        self.persist(event)
        before = deepcopy(event)
        result = self.reconcile(event)
        self.assertEqual(result["mismatches"], [])
        self.assertIsNone(result["ai_match"])
        self.assertEqual(event, before)

    def test_reconciliation_mismatches(self):
        event = sample("debt_limit:initial_with_ai")
        self.assertFalse(self.reconcile(event)["event_found"])
        row = self.persist(event)["version"]
        changed = deepcopy(event)
        changed["summary"] += " Unpersisted change."
        self.assertIn("version_found", self.reconcile(changed)["mismatches"])
        with transaction(self.engine) as session:  # Deliberate fixture corruption; audit only reads.
            session.execute(event_history.delete().where(event_history.c.event_version_id == row["id"]))
            session.execute(event_provenance.delete().where(event_provenance.c.event_version_id == row["id"]))
        from persistence import reconciliation
        with patch.dict(reconciliation._last_warning, clear=True), \
             self.assertLogs("macro_reconciliation", level="WARNING") as logs:
            result = self.reconcile(event)
            self.reconcile(event)  # Mismatch warnings stay rate-limited.
        self.assertEqual(len(logs.output), 1)
        for key in ("provenance_match", "score_match", "decision_match", "ai_match"):
            self.assertIn(key, result["mismatches"])
        self.assertIn("Treasury shadow reconciliation mismatch", logs.output[0])

    def test_historical_current_expectation(self):
        self.persist(sample())
        self.persist(sample("refunding:material"))
        self.assertIn("current_version_match", self.reconcile(sample())["mismatches"])
        self.assertEqual(self.reconcile(sample(), expect_current=False)["mismatches"], [])

    def test_reconciliation_issues_only_reads(self):
        event = sample("yield:threshold")
        self.persist(event)
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            self.assertEqual(self.reconcile(event)["mismatches"], [])
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        self.assertTrue(statements)
        self.assertTrue(all(s.lstrip().upper().startswith(("SELECT", "BEGIN")) for s in statements), statements)
