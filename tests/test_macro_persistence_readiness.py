"""Macro readiness contract: fixed replay, late arrival, safe promotion and loss."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from persistence import macro_shadow as shadow
from persistence.adapters.macro import adapt_macro, macro_promotion
from persistence.database import transaction
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.reconciliation import reconcile_macro_event
from persistence.repository import EventRepository, IdentityConflict
from tests import macro_readiness_corpus as corpus
from tests import test_macro_persistence_adapter as adapter
from tests.test_macro_persistence_adapter import sample, NOW


def replay(engine):
    manifest, rows = corpus.load_corpus()
    duplicates = 0
    for row in rows:
        result = shadow.persist_macro(engine, row["event"], datetime.fromisoformat(row["observed_at"]),
                                      make_current=row["make_current"], report=True)
        duplicates += int(result["duplicate"])
    return reconcile_corpus(engine, duplicates)


def reconcile_corpus(engine, duplicates=None):
    manifest, rows = corpus.load_corpus()
    results = []
    with transaction(engine) as session:
        if engine.dialect.name == "postgresql":
            session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        repo = EventRepository(session)
        for row in rows:
            result = reconcile_macro_event(row["event"], repo, expect_current=row["expect_current"])
            results.append(dict(label=row["label"], **result))
        counts = {table.name: session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
                  for table in (events, event_versions, event_provenance, event_history)}
    mismatches = [{"label": r["label"], "fields": r["mismatches"]} for r in results if r["mismatches"]]
    return dict(corpus_version=manifest["corpus_version"], observations=len(rows),
                matched=len(rows) - len(mismatches), mismatches=mismatches,
                duplicates=duplicates, counts=counts, results=results)


class ReadinessTests(unittest.TestCase):
    setUp = adapter.AdapterTests.setUp

    def current(self, row):
        with transaction(self.engine) as session:
            return EventRepository(session).current(row["event_id"])

    def test_fixed_corpus_is_reproducible(self):
        manifest, rows = corpus.load_corpus()
        self.assertEqual((manifest, rows), corpus.load_corpus())
        self.assertEqual(len(rows), 56)
        self.assertEqual({r["event"]["release_category"] for r in rows}, set(manifest["categories"]))
        self.assertTrue(all(r["event"]["symbols"] == [] for r in rows))
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        expected = corpus.MANIFEST.with_suffix(".sha256").read_text().strip()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), expected)

    def test_replay_full_read_only_reconciliation_and_repeat(self):
        manifest, _ = corpus.load_corpus()
        result = replay(self.engine)
        self.assertEqual(result["mismatches"], [])
        self.assertEqual(result["matched"], 56)
        self.assertEqual(result["duplicates"], manifest["expected_duplicates"])
        self.assertEqual(result["counts"], manifest["expected_counts"])
        again = replay(self.engine)
        self.assertEqual(again["mismatches"], [])
        self.assertEqual(again["duplicates"], 56)
        self.assertEqual(again["counts"], result["counts"])
        print("PHASE2D_RECONCILIATION " + json.dumps({k: v for k, v in result.items() if k != "results"}, sort_keys=True))

    def test_newer_first_unseen_older_arrives_late(self):
        initial = sample()
        newer = corpus.material(initial)
        first = shadow.persist_macro(self.engine, newer, NOW)
        older = shadow.persist_macro(self.engine, initial, NOW + timedelta(days=1), report=True)
        self.assertEqual(older["promotion"], "older")
        self.assertEqual(self.current(first)["id"], first["id"])
        self.assertNotEqual(older["version"]["id"], first["id"])

    def test_material_with_explicit_source_chronology_promotes(self):
        event = sample()
        first = shadow.persist_macro(self.engine, event, NOW)
        newer = shadow.persist_macro(self.engine, corpus.material(event), NOW - timedelta(days=1), report=True)
        self.assertEqual(newer["promotion"], "newer_material")
        self.assertEqual(self.current(first)["id"], newer["version"]["id"])

    def test_explicit_correction_preserves_distinct_collector_id(self):
        _, rows = corpus.load_corpus()
        original, correction = rows[0]["event"], rows[-2]["event"]
        self.assertNotEqual(original["event_id"], correction["event_id"])
        first = shadow.persist_macro(self.engine, original, NOW)
        corrected = shadow.persist_macro(self.engine, correction, NOW, report=True)
        self.assertEqual(corrected["promotion"], "first")
        self.assertNotEqual(first["event_id"], corrected["version"]["event_id"])
        self.assertEqual(self.current(first)["id"], first["id"])

    def test_duplicate_old_version_never_repromoted(self):
        event = sample()
        first = shadow.persist_macro(self.engine, event, NOW)
        newer = shadow.persist_macro(self.engine, corpus.material(event), NOW)
        duplicate = shadow.persist_macro(self.engine, event, NOW + timedelta(days=10), report=True)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["promotion"], "duplicate")
        self.assertEqual(self.current(first)["id"], newer["id"])

    def test_cosmetic_late_arrival_even_with_later_publication(self):
        event = sample()
        first = shadow.persist_macro(self.engine, event, NOW)
        cosmetic = deepcopy(event)
        cosmetic["headline"] += " "
        cosmetic["summary"] = cosmetic["summary"].replace(" ", "  ")
        cosmetic["url"] = "https://www.bls.gov/news.release/archives/cpi_09112026.htm"
        adapter.source_revision(cosmetic)
        held = shadow.persist_macro(self.engine, cosmetic, NOW + timedelta(days=1), report=True)
        self.assertEqual(held["promotion"], "cosmetic")
        self.assertEqual(self.current(first)["id"], first["id"])
        self.assertNotEqual(held["version"]["id"], first["id"])

    def test_stale_and_missing_date_late_arrivals_not_promoted(self):
        _, rows = corpus.load_corpus()
        current = shadow.persist_macro(self.engine, rows[2]["event"], NOW)
        for entry in rows[5:7]:
            result = shadow.persist_macro(self.engine, entry["event"], NOW + timedelta(days=1), make_current=False, report=True)
            self.assertEqual(result["promotion"], "caller_disabled")
            self.assertEqual(self.current(current)["id"], current["id"])

    def test_ambiguous_material_change_never_uses_arrival_or_metric_direction(self):
        event = sample()
        first = shadow.persist_macro(self.engine, event, NOW)
        for delta in (1, -1):
            changed = corpus.material(event, minutes=0, delta=delta)
            result = shadow.persist_macro(self.engine, changed, NOW + timedelta(days=1), report=True)
            self.assertEqual(result["promotion"], "ambiguous")
            self.assertEqual(self.current(first)["id"], first["id"])

    def test_unknown_or_incomparable_precision_blocks_promotion(self):
        event = sample()
        first = shadow.persist_macro(self.engine, event, NOW)
        for precision in ("unknown", "minute"):
            changed = corpus.material(event)
            changed["timestamp_precision"] = precision
            if precision == "unknown": changed["published_at"] = None
            held = shadow.persist_macro(self.engine, changed, NOW, report=True)
            self.assertEqual(held["promotion"], "ambiguous")
            self.assertEqual(self.current(first)["id"], first["id"])

    def test_date_only_publication_order(self):
        event = sample("gdp")
        event.update(timestamp_precision="date", published_at="2026-09-20T04:00:00+00:00")
        first = shadow.persist_macro(self.engine, event, NOW)
        newer = corpus.material(event, minutes=24 * 60)
        result = shadow.persist_macro(self.engine, newer, NOW, report=True)
        self.assertEqual(result["promotion"], "newer_material")
        self.assertIsNone(result["version"]["published_at"])
        self.assertEqual(self.current(first)["id"], result["version"]["id"])

    def test_changed_identity_facts_are_ambiguous_not_merged(self):
        event = sample()
        first = shadow.persist_macro(self.engine, event, NOW)
        changed = corpus.material(event)
        changed["reference_period"] = "2026-07"
        result = shadow.persist_macro(self.engine, changed, NOW, report=True)
        self.assertEqual(result["promotion"], "ambiguous")
        self.assertEqual(self.current(first)["id"], first["id"])

    def test_conflicting_version_rolls_back_without_promotion(self):
        event = sample()
        original = shadow.persist_macro(self.engine, event, NOW)
        inputs = adapt_macro(corpus.material(event), NOW)["record"]
        inputs["version_key"] = original["version_key"]
        with self.assertRaises(IdentityConflict), transaction(self.engine) as session:
            EventRepository(session).record(**inputs, promotion_policy=macro_promotion)
        self.assertEqual(self.current(original)["id"], original["id"])
        with transaction(self.engine) as session:
            self.assertEqual(len(EventRepository(session).versions(original["event_id"])), 1)

    def test_both_arrival_orders_select_same_ordered_material_version(self):
        base = sample()
        newer = corpus.material(base)
        for order in ((base, newer), (newer, base)):
            # Separate synthetic anchor per permutation; use supplied identities verbatim.
            for event in order:
                value = deepcopy(event)
                value["event_id"] += str(order is not None) + ("a" if order[0] is base else "b")
                row = shadow.persist_macro(self.engine, value, NOW)
            self.assertEqual(self.current(row)["attributes"]["metrics"], newer["metrics"])
