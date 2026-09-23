"""Fixture fidelity, immutable history, and atomic rollback (no dotenv/network)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa
from alembic import command

from tests import test_macro_pipeline as fixtures
from tests.test_persistence import migration_config
from persistence.adapters.macro import adapt_macro
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction, PersistenceError
from persistence.models import metadata, events, event_versions, event_provenance, event_history
from persistence.repository import EventRepository
from persistence.macro_shadow import persist_macro

def source_revision(event):
    """Synthetic material update with explicit source publication chronology."""
    event["published_at"] = (datetime.fromisoformat(event["published_at"]) + timedelta(minutes=1)).isoformat()
    return event


NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def sample(category="cpi", scored=True):
    event = fixtures.pipeline_event(category)
    if event["agency"] == "bls":
        payload = json.loads((fixtures.FIXTURES / "bls_api.json").read_text())
        event = fixtures.bls.enrich_bls_event(event, payload)
    if scored:
        event = fixtures.macro._analyze_candidate(event, False, {"analysis_errors": 0})
    return event


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        with self.engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        self.addCleanup(self.engine.dispose)

    def test_six_representative_roundtrips(self):
        for category in fixtures.SOURCES:
            with self.subTest(category=category):
                original = sample(category)
                saved = deepcopy(original)
                row = persist_macro(self.engine, original, NOW)
                expected = adapt_macro(original, NOW)["record"]
                self.assertEqual(original, saved)
                with transaction(self.engine) as session:
                    repo = EventRepository(session)
                    anchor = session.execute(sa.select(events).where(events.c.id == row["event_id"])).mappings().one()
                    self.assertEqual(anchor["event_key"], original["event_id"])
                    self.assertEqual(row["version_key"], expected["version_key"])
                    self.assertEqual(repo.current(row["event_id"])["id"], row["id"])
                    for field in ("headline", "summary", "published_at", "publication_date", "timestamp_precision", "stage"):
                        self.assertEqual(row[field], expected["normalized"][field])
                    for field in ("reference_period", "release_category", "release_stage", "release_id", "revision_id",
                                  "symbols", "direct_symbols", "related_symbols"):
                        self.assertEqual(row["attributes"][field], original[field])
                    if "metrics" in original:
                        self.assertEqual(row["attributes"]["metrics"], original["metrics"])
                    history = {r["kind"]: r["attributes"] for r in repo.history(row["id"])}
                    self.assertEqual(history["score"]["impact_score"], original["impact_score"])
                    self.assertEqual(history["score"]["score_reasons"], original["score_reasons"])
                    self.assertEqual(history["decision"]["alert_decision"], original["alert_decision"])
                    provenance = repo.provenance(row["id"])
                    release = next(p for p in provenance if p["attributes"]["role"] == "release")
                    self.assertEqual(release["canonical_url"], original["url"])
                    self.assertEqual(release["source_name"], original["source"])
                    self.assertEqual(release["document_id"], original["release_id"])
                    self.assertEqual(release["attributes"]["publisher"], original["publisher"])
                    self.assertEqual(release["attributes"]["published_at"], original["published_at"])

    def test_duplicates_all_snapshots_and_provenance(self):
        event = sample()
        first = persist_macro(self.engine, event, NOW)
        second = persist_macro(self.engine, event, NOW + timedelta(hours=1))
        self.assertEqual(first["id"], second["id"])
        with transaction(self.engine) as session:
            for table, count in ((events, 1), (event_versions, 1), (event_provenance, 3), (event_history, 2)):
                self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one(), count)

    def test_material_version_preserves_identity_and_old_history(self):
        event = sample()
        first = persist_macro(self.engine, event, NOW)
        changed = deepcopy(event)
        changed["metrics"]["headline_cpi_sa"]["value"] += 1
        source_revision(changed)
        second = persist_macro(self.engine, changed, NOW + timedelta(minutes=1))
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertNotEqual(first["id"], second["id"])
        persist_macro(self.engine, event, NOW + timedelta(minutes=2))
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            self.assertEqual(repo.current(first["event_id"])["id"], second["id"])
            versions = {r["id"]: r for r in repo.versions(first["event_id"])}
            self.assertEqual(versions[first["id"]]["attributes"], first["attributes"])
            self.assertEqual(len(versions), 2)

    def test_explicit_correction_keeps_collector_identity(self):
        event = fixtures.pipeline_event("cpi")
        corrected = fixtures.bls.normalize_bls_feed(fixtures.pipeline_fixture("cpi").replace(
            "<title>Consumer", "<title>Correction: Consumer"), fixtures.SOURCES["cpi"])
        self.assertNotEqual(event["event_id"], corrected["event_id"])
        first = persist_macro(self.engine, event, NOW)
        second = persist_macro(self.engine, corrected, NOW)
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["revision_key"], corrected["revision_id"])

    def test_cosmetic_change_never_changes_event_anchor(self):
        event = sample()
        first = persist_macro(self.engine, event, NOW)
        event["headline"] += " "
        event["url"] = "https://www.bls.gov/news.release/archives/cpi_09112026.htm"
        second = persist_macro(self.engine, event, NOW)
        self.assertEqual(first["event_id"], second["event_id"])

    def test_no_symbols_no_ai(self):
        row = persist_macro(self.engine, sample(), NOW)
        self.assertEqual(row["attributes"]["symbols"], [])
        with transaction(self.engine) as session:
            self.assertEqual({r["kind"] for r in EventRepository(session).history(row["id"])}, {"score", "decision"})

    def test_ai_and_score_changes_append_without_changing_facts(self):
        event = sample()
        first = persist_macro(self.engine, event, NOW)
        event.update(ai_summary="Visible summary", ai_sentiment="NEUTRAL", ai_confidence=80,
                     ai_why_it_matters="Visible context", ai_event_type="macro", ai_model="fixture",
                     hidden_reasoning="must not persist", api_key="must not persist")
        second = persist_macro(self.engine, event, NOW)
        self.assertEqual(first["id"], second["id"])
        event["impact_score"] = 88
        event["ai_summary"] = "Another validated summary"
        persist_macro(self.engine, event, NOW)
        with transaction(self.engine) as session:
            history = EventRepository(session).history(first["id"])
            self.assertEqual(len(history), 6)  # two score, decision and AI snapshots each
            self.assertNotIn("must not persist", str(history))
            self.assertEqual(sum(r["kind"] == "ai" for r in history), 2)

    def test_invalid_ai_not_persisted(self):
        event = sample()
        event.update(ai_summary="text", ai_sentiment="INVALID", ai_confidence=900,
                     ai_why_it_matters="text", ai_event_type="macro")
        row = persist_macro(self.engine, event, NOW)
        with transaction(self.engine) as session:
            self.assertNotIn("ai", {r["kind"] for r in EventRepository(session).history(row["id"])})

    def test_missing_date_unscored(self):
        event = sample(scored=False)
        event.update(published_at=None, timestamp_precision="unknown")
        row = persist_macro(self.engine, event, NOW, make_current=False)
        self.assertIsNone(row["published_at"])
        self.assertIsNone(row["publication_date"])
        with transaction(self.engine) as session:
            self.assertEqual(EventRepository(session).history(row["id"]), [])

    def test_date_only_not_invented_instant(self):
        event = sample("gdp")
        event.update(timestamp_precision="date", published_at="2026-09-23T04:00:00+00:00")
        row = persist_macro(self.engine, event, NOW)
        self.assertIsNone(row["published_at"])
        self.assertEqual(row["publication_date"].isoformat(), "2026-09-23")

    def test_skipped_observation_does_not_replace_richer_version(self):
        row = persist_macro(self.engine, sample(), NOW)
        skipped = fixtures.pipeline_event("cpi")
        persist_macro(self.engine, skipped, NOW, make_current=False)
        with transaction(self.engine) as session:
            self.assertEqual(EventRepository(session).current(row["event_id"])["id"], row["id"])

    def test_whitelisted_optional_facts_and_fetch_time(self):
        event = sample()
        event.update(effective_at="2026-09-01", relevance_rule="explicit rule",
                     relevance_reason="explicit reason", relevance_evidence={"fact": 1},
                     fetched_at=NOW.isoformat(), source_hash="a" * 64,
                     raw_payload="not stored", headers={"authorization": "not stored"})
        row = persist_macro(self.engine, event, NOW)
        self.assertEqual(row["attributes"]["effective_at"], event["effective_at"])
        self.assertEqual(row["attributes"]["relevance_evidence"], {"fact": 1})
        self.assertNotIn("not stored", str(row))
        with transaction(self.engine) as session:
            provenance = EventRepository(session).provenance(row["id"])
            self.assertEqual(provenance[0]["retrieved_at"], NOW)
            release = next(p for p in provenance if p["attributes"]["role"] == "release")
            self.assertEqual(release["content_hash"], "a" * 64)

    def test_repository_exception_rolls_back_entire_snapshot(self):
        with patch.object(EventRepository, "append_history", side_effect=RuntimeError("private")):
            with self.assertRaises(RuntimeError):
                persist_macro(self.engine, sample(), NOW)
        with transaction(self.engine) as session:
            for table in metadata.tables.values():
                self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one(), 0)
        self.assertIsNotNone(persist_macro(self.engine, sample(), NOW))

    def test_history_foreign_key_and_kind_constraints(self):
        with self.assertRaises(PersistenceError), transaction(self.engine) as session:
            EventRepository(session).append_history(str(uuid4()), "score", {"impact_score": 1})
        row = persist_macro(self.engine, sample(), NOW)
        with self.assertRaises(PersistenceError), transaction(self.engine) as session:
            EventRepository(session).append_history(row["id"], "delivery", {})

    def test_failed_revision_preserves_committed_snapshot(self):
        event = sample()
        original = persist_macro(self.engine, event, NOW)
        event["metrics"]["headline_cpi_sa"]["value"] += 1
        with patch.object(EventRepository, "append_history", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                persist_macro(self.engine, event, NOW + timedelta(minutes=1))
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            self.assertEqual(repo.current(original["event_id"])["id"], original["id"])
            self.assertEqual(len(repo.versions(original["event_id"])), 1)
            self.assertEqual(len(repo.history(original["id"])), 2)

    def test_history_migration_downgrade_preserves_foundation(self):
        row = persist_macro(self.engine, sample(), NOW)
        with self.engine.begin() as connection:
            config = migration_config(connection)
            # Explicit target: later additive migrations (e.g. 0003) sit above the history revision.
            command.downgrade(config, "0001_persistence_foundation")
            self.assertNotIn("event_history", sa.inspect(connection).get_table_names())
            self.assertEqual(connection.execute(sa.select(sa.func.count()).select_from(event_versions)).scalar_one(), 1)
            command.upgrade(config, "head")
        with transaction(self.engine) as session:
            self.assertEqual(EventRepository(session).current(row["event_id"])["id"], row["id"])
            self.assertEqual(EventRepository(session).history(row["id"]), [])
