"""Geopolitical fixture fidelity, relevance evidence, provenance links and rollback."""
from copy import deepcopy
from datetime import date, datetime, timezone
from functools import lru_cache
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic import command

from tests import geopolitical_readiness_corpus as corpus
from tests.test_persistence import migration_config
from persistence.adapters.geopolitical import adapt_geopolitical, geopolitical_promotion
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction, PersistenceError
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.repository import EventRepository, IdentityConflict
from persistence.geopolitical_shadow import persist_geopolitical

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


@lru_cache(maxsize=1)
def _rows():
    return {row["label"]: row for row in corpus.load_corpus()[1]}


def row(label):
    return deepcopy(_rows()[label])


def sample(label="bis_final"):
    return row(label)["event"]


class GeopoliticalAdapterTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        with self.engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        self.addCleanup(self.engine.dispose)

    def count(self, table):
        with transaction(self.engine) as session:
            return session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()

    def stored(self, version):
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            anchor = session.execute(sa.select(events).where(events.c.id == version["event_id"])).mappings().one()
            return dict(anchor), repo.current(version["event_id"]), repo.provenance(version["id"]), repo.history(version["id"])

    def test_representative_roundtrips_preserve_collector_identity(self):
        for label in ("bis_final", "entity_list", "sanctions", "trade", "platform_remedy", "complaint",
                      "proposal", "clarification", "amendment", "moea_disruption", "whitehouse_action", "correction"):
            with self.subTest(label=label):
                original = sample(label)
                saved = deepcopy(original)
                version = persist_geopolitical(self.engine, original, NOW)
                self.assertEqual(original, saved)  # Adapter never mutates the collector event.
                anchor, current, provenance, _ = self.stored(version)
                self.assertEqual((anchor["source_family"], anchor["identity_version"]), ("geopolitical", "geopolitical-v1"))
                self.assertEqual(anchor["event_key"], original["event_id"])
                self.assertEqual(current["id"], version["id"])
                for key in ("headline", "summary", "event_type", "market_scope", "publication_basis"):
                    self.assertEqual(version[key], original[key])
                self.assertEqual((version["stage"], version["revision_key"]), (original["policy_stage"], original["revision_id"]))
                attrs = version["attributes"]
                for key in ("policy_id", "document_id", "identity_anchors", "geopolitical_category", "policy_action",
                            "policy_stage", "legal_status", "legal_references", "effective_at", "symbols",
                            "direct_symbols", "related_symbols", "relevance_reasons", "evidence",
                            "matched_entities", "matched_products", "matched_jurisdictions", "policy_scope"):
                    self.assertEqual(attrs[key], original[key], key)
                self.assertNotIn("body", attrs)
                self.assertFalse(any(key.startswith("ai_") for key in attrs))
                self.assertEqual(len(provenance), len(original["provenance"]))

    def test_direct_meta_and_indirect_nvda_evidence(self):
        meta = persist_geopolitical(self.engine, sample("platform_remedy"), NOW)["attributes"]
        self.assertEqual((meta["direct_symbols"], meta["related_symbols"]), (["META"], []))
        self.assertEqual(meta["evidence"][0]["rule"], "explicit_company_action_v1")
        nvda = persist_geopolitical(self.engine, sample("bis_final"), NOW)["attributes"]
        self.assertEqual((nvda["direct_symbols"], nvda["related_symbols"]), ([], ["NVDA"]))
        evidence = nvda["evidence"][0]
        self.assertEqual(evidence["rule"], "nvda_advanced_compute_supply_v1")
        self.assertEqual((evidence["matched_products"], evidence["matched_jurisdictions"]), (["advanced_computing"], ["China"]))
        self.assertEqual(evidence["policy_scope"], "export_controls")
        body = corpus.DOCS["bis_final"]["body"]
        self.assertEqual(body[evidence["start"]:evidence["end"]], evidence["quote"])

    def test_persistence_never_reruns_relevance_scoring_or_identity(self):
        from tests.test_geopolitical_pipeline import geo
        event = sample()
        with patch.object(geo, "detect_relevance", side_effect=AssertionError("relevance")), \
             patch.object(geo, "score_geopolitical_event", side_effect=AssertionError("rescore")), \
             patch.object(geo, "evaluate_alert", side_effect=AssertionError("re-decide")), \
             patch.object(geo, "resolve_identity", side_effect=AssertionError("resolve")):
            version = persist_geopolitical(self.engine, event, NOW)
        self.assertEqual({h["kind"] for h in self.stored(version)[3]}, {"score", "decision", "ai"})

    def test_unresolved_or_invented_identity_rejected(self):
        for change in (dict(identity_status="unresolved"), dict(event_id=None), dict(policy_id=None),
                       dict(event_type="macro_release")):
            event = sample()
            event.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                persist_geopolitical(self.engine, event, NOW)
        self.assertEqual(self.count(events), 0)

    def test_scores_decisions_official_quality(self):
        version = persist_geopolitical(self.engine, sample("complaint"), NOW)
        history = {h["kind"]: h["attributes"] for h in self.stored(version)[3]}
        self.assertEqual((history["score"]["impact_score"], history["score"]["quality_adjustment"]), (75, 0))
        self.assertEqual((history["decision"]["alert_decision"], history["decision"]["initial_decision"]), ("ALERT", "ALERT"))
        self.assertEqual(history["decision"]["score_snapshot"]["impact_score"], 75)

    def test_ai_present_absent_invalid_and_cannot_change_facts(self):
        present = persist_geopolitical(self.engine, sample("bis_final"), NOW)
        ai = [h["attributes"] for h in self.stored(present)[3] if h["kind"] == "ai"]
        self.assertEqual(ai, [dict(corpus.AI)])
        self.assertEqual((present["stage"], present["attributes"]["symbols"]), ("adopted", ["NVDA"]))  # enrich tried "fake"/"FAKE".
        absent = persist_geopolitical(self.engine, sample("proposal"), NOW)
        self.assertEqual({h["kind"] for h in self.stored(absent)[3]}, {"score", "decision"})
        invalid = sample("entity_list")
        invalid["ai_confidence"] = True
        version = persist_geopolitical(self.engine, invalid, NOW)
        self.assertNotIn("ai", {h["kind"] for h in self.stored(version)[3]})

    def test_companion_documents_link_to_one_policy_version(self):
        first = persist_geopolitical(self.engine, sample("bis_final"), NOW, report=True)
        companion = persist_geopolitical(self.engine, sample("fr_companion"), NOW, report=True)
        self.assertEqual(companion["version"]["id"], first["version"]["id"])
        self.assertFalse(companion["duplicate"])  # New document evidence only.
        provenance = {p["document_id"]: p for p in self.stored(first["version"])[2]}
        self.assertEqual(set(provenance), {"bis:synthetic-chip-rule", "fr:2026-99901"})
        policy_id = sample()["policy_id"]
        for document_id, entry in provenance.items():
            self.assertEqual(entry["attributes"]["relation"], "policy_document")
            self.assertEqual(entry["attributes"]["policy_id"], policy_id)
            self.assertEqual(entry["attributes"]["analyzed_document"], document_id == "bis:synthetic-chip-rule")
            self.assertEqual(len(entry["content_hash"]), 64)
        self.assertEqual(provenance["fr:2026-99901"]["attributes"]["fr_edition"], "published")
        self.assertEqual(provenance["fr:2026-99901"]["source_name"], "Federal Register")
        self.assertTrue(persist_geopolitical(self.engine, sample("duplicate"), NOW, report=True)["duplicate"])
        self.assertEqual((self.count(events), self.count(event_versions), self.count(event_provenance)), (1, 1, 2))

    def test_whitehouse_action_and_federal_register_companion(self):
        action = persist_geopolitical(self.engine, sample("whitehouse_action"), NOW)
        companion = persist_geopolitical(self.engine, sample("whitehouse_fr_companion"), NOW)
        self.assertEqual(companion["id"], action["id"])
        self.assertEqual({p["attributes"]["agency"] for p in self.stored(action)[2]}, {"whitehouse", "fr"})
        self.assertIn("eo:99960", action["attributes"]["identity_anchors"])

    def test_public_inspection_then_published_edition_one_event(self):
        inspection = persist_geopolitical(self.engine, sample("public_inspection"), NOW)
        published = persist_geopolitical(self.engine, sample("published_edition"), NOW)
        self.assertEqual(published["id"], inspection["id"])
        self.assertEqual(inspection["publication_basis"], "public_inspection_filed_at")
        rows = self.stored(inspection)[2]
        self.assertEqual({r["document_id"] for r in rows}, {"fr:2026-99950"})
        self.assertEqual(sorted(r["attributes"]["fr_edition"] for r in rows), ["public_inspection", "published"])
        # Redis keeps only the latest edition per document ID; PostgreSQL keeps both.
        self.assertEqual(len(sample("published_edition")["provenance"]), 1)

    def test_provenance_duplicate_is_idempotent(self):
        event = sample("public_inspection")
        persist_geopolitical(self.engine, event, NOW)
        for _ in range(3):
            self.assertTrue(persist_geopolitical(self.engine, deepcopy(event), NOW, report=True)["duplicate"])
        self.assertEqual(self.count(event_provenance), 1)

    def test_timestamp_precision_and_source_timezone(self):
        second = persist_geopolitical(self.engine, sample("moea_disruption"), NOW)
        self.assertEqual((second["timestamp_precision"], second["published_at"]),
                         ("second", datetime(2026, 9, 22, 12, tzinfo=timezone.utc)))
        dated = sample("moea_disruption")
        dated.update(timestamp_precision="date", published_at="2026-09-22T16:00:00+00:00")  # Taipei midnight.
        row_ = persist_geopolitical(self.engine, dated, NOW)
        self.assertEqual((row_["publication_date"], row_["published_at"]), (date(2026, 9, 23), None))
        unknown = sample("trade")
        unknown["timestamp_precision"] = "unknown"
        with self.assertRaises(ValueError):
            persist_geopolitical(self.engine, unknown, NOW)

    def test_body_is_never_stored_and_writer_strips_it(self):
        from persistence.geopolitical_shadow import GeopoliticalShadowWriter
        event = sample()
        event["body"] = "x" * 600_000
        adapted = adapt_geopolitical(event, NOW)
        self.assertNotIn("body", adapted["record"]["normalized"]["attributes"])
        captured = []
        with patch("persistence.geopolitical_shadow.persist_geopolitical", side_effect=lambda e, ev, *a, **k: captured.append(ev) or {"duplicate": False}):
            writer = GeopoliticalShadowWriter(engine_factory=lambda: type("E", (), {"dispose": lambda self: None})())
            self.assertTrue(writer.submit(event))
            stats = writer.shutdown(timeout=5)["stats"]
        self.assertEqual((stats["persisted"], stats["dropped_invalid"]), (1, 0))
        self.assertNotIn("body", captured[0])
        self.assertIn("body", event)  # Caller's event is untouched.

    def test_rollback_on_history_failure(self):
        with patch.object(EventRepository, "append_history", side_effect=PersistenceError("private")):
            with self.assertRaises(PersistenceError):
                persist_geopolitical(self.engine, sample(), NOW)
        for table in (events, event_versions, event_provenance, event_history):
            self.assertEqual(self.count(table), 0)

    def test_conflicting_version_key_rolls_back(self):
        original = persist_geopolitical(self.engine, sample(), NOW)
        changed = sample()
        changed["summary"] += " Synthetic material change."
        inputs = adapt_geopolitical(changed, NOW)["record"]
        inputs["version_key"] = original["version_key"]
        with self.assertRaises(IdentityConflict), transaction(self.engine) as session:
            EventRepository(session).record(**inputs, promotion_policy=geopolitical_promotion)
        self.assertEqual(self.count(event_versions), 1)

    def test_restart_deduplicates_from_database_only(self):
        from persistence.geopolitical_shadow import GeopoliticalShadowWriter
        event = sample("sanctions")
        for expected in (0, 1):
            if self.engine.dialect.name == "sqlite":
                # In-memory SQLite is bound to this thread; each call is still a fresh repository.
                self.assertEqual(persist_geopolitical(self.engine, event, NOW, report=True)["duplicate"], bool(expected))
                continue
            writer = GeopoliticalShadowWriter(engine_factory=lambda: self.engine)
            self.assertTrue(writer.submit(event))
            stats = writer.shutdown(timeout=5)["stats"]
            self.assertEqual((stats["persisted"], stats["duplicate"], stats["failed"]), (1, expected, 0))
        self.assertEqual((self.count(events), self.count(event_versions)), (1, 1))
