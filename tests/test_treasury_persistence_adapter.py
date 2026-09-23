"""Treasury fixture fidelity, immutable history and atomic rollback (no dotenv/network)."""
from copy import deepcopy
from datetime import date, datetime, timezone
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic import command

from tests import treasury_readiness_corpus as corpus
from tests.test_persistence import migration_config
from persistence.adapters.treasury import adapt_treasury, treasury_promotion
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction, PersistenceError
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.repository import EventRepository, IdentityConflict
from persistence.treasury_shadow import persist_treasury

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def rows():
    return {row["label"]: row["event"] for row in corpus.load_corpus()[1]}


def sample(label="refunding:initial"):
    return deepcopy(rows()[label])


class TreasuryAdapterTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        with self.engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        self.addCleanup(self.engine.dispose)

    def count(self, table):
        with transaction(self.engine) as session:
            return session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()

    def stored(self, row):
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            anchor = session.execute(sa.select(events).where(events.c.id == row["event_id"])).mappings().one()
            return dict(anchor), repo.current(row["event_id"]), repo.provenance(row["id"]), repo.history(row["id"])

    def test_representative_family_roundtrips_preserve_collector_identity(self):
        labels = ("refunding:initial", "borrowing_estimates:initial", "debt_limit:initial_with_ai",
                  "issuance_policy:initial", "bill_auction:announcement", "bill_auction:result",
                  "note_auction:result", "reopening:result", "yield:routine", "yield:threshold")
        for label in labels:
            with self.subTest(label=label):
                original = sample(label)
                saved = deepcopy(original)
                row = persist_treasury(self.engine, original, NOW)
                self.assertEqual(original, saved)  # Adapter never mutates the collector event.
                anchor, current, provenance, _ = self.stored(row)
                self.assertEqual((anchor["source_family"], anchor["identity_version"]), ("treasury", "treasury-v1"))
                self.assertEqual(anchor["event_key"], original["event_id"])
                self.assertEqual(current["id"], row["id"])
                self.assertEqual(row["version_key"], adapt_treasury(original, NOW)["record"]["version_key"])
                for key in ("headline", "summary", "event_type", "market_scope", "publication_basis"):
                    self.assertEqual(row[key], original[key])
                self.assertEqual((row["source_name"], row["publisher"], row["canonical_url"]),
                                 (original["source"], original["publisher"], original["url"]))
                self.assertEqual((row["stage"], row["revision_key"]), (original["release_stage"], original["revision_id"]))
                for key in ("treasury_category", "release_id", "release_stage", "revision_id", "reference_period",
                            "metrics", "symbols", "relevant", "agency"):
                    self.assertEqual(row["attributes"][key], original[key])
                self.assertFalse(any(key.startswith("ai_") for key in row["attributes"]))
                self.assertEqual(len(provenance), 1)

    def test_timestamp_precision_mapping(self):
        release = persist_treasury(self.engine, sample(), NOW)
        self.assertEqual(release["published_at"], datetime.fromisoformat(sample()["published_at"]))
        self.assertEqual(release["timestamp_precision"], "second")
        letter = persist_treasury(self.engine, sample("debt_limit:initial_with_ai"), NOW)
        self.assertIsNone(letter["published_at"])
        self.assertEqual((letter["publication_date"], letter["timestamp_precision"]), (date(2026, 9, 21), "date"))
        self.assertEqual(letter["publication_basis"], "letter_date")
        observation = persist_treasury(self.engine, sample("yield:threshold"), NOW)
        self.assertEqual((observation["published_at"], observation["publication_date"]), (None, None))
        self.assertEqual(observation["timestamp_precision"], "unknown")
        self.assertEqual(observation["publication_basis"], "unverified_observation_only")

    def test_auction_metadata_and_provenance(self):
        event = sample("reopening:result")
        row = persist_treasury(self.engine, event, NOW)
        attrs = row["attributes"]
        self.assertEqual((attrs["cusip"], attrs["auction_date"], attrs["security_type"], attrs["security_term"]),
                         ("912797TC1", "2026-09-21", "Bill", "13-Week"))
        self.assertIs(attrs["reopening"], True)
        self.assertEqual(attrs["source_document"], "R_20260921_2.pdf")
        self.assertEqual(attrs["metrics"], event["metrics"])
        self.assertEqual(attrs["metrics"]["offering_amount_usd"], 92000000000.0)
        self.assertEqual((row["stage"], row["publication_basis"]), ("result", "result_document_date"))
        _, _, provenance, _ = self.stored(row)
        self.assertEqual(provenance[0]["document_id"], "R_20260921_2.pdf")
        self.assertEqual(provenance[0]["attributes"]["role"], "auction_record")
        self.assertEqual(provenance[0]["attributes"]["retrieval_basis"], "shadow_observation")
        self.assertIsNone(provenance[0]["content_hash"])
        announcement = persist_treasury(self.engine, sample("bill_auction:announcement"), NOW)
        self.assertEqual(announcement["publication_date"], date(2026, 9, 17))
        self.assertEqual(announcement["publication_basis"], "announcement_date")

    def test_yield_metadata_and_movement_preserved(self):
        event = sample("yield:threshold")
        row = persist_treasury(self.engine, event, NOW)
        attrs = row["attributes"]
        self.assertEqual((attrs["dataset"], attrs["observation_date"]), ("daily_treasury_yield_curve", "2026-09-21"))
        self.assertEqual(attrs["metrics"]["prior_observation_date"], "2026-09-18")
        self.assertEqual(attrs["metrics"]["movement_bps"]["BC_2YEAR"], 15)
        self.assertEqual(attrs["metrics"]["yields_percent"], event["metrics"]["yields_percent"])
        _, _, provenance, history = self.stored(row)
        self.assertEqual(provenance[0]["attributes"]["role"], "yield_dataset")
        self.assertEqual(provenance[0]["attributes"]["dataset"], "daily_treasury_yield_curve")
        score = next(h for h in history if h["kind"] == "score")["attributes"]
        self.assertEqual(score["impact_score"], 70)  # Configured movement result as computed by collector.

    def test_scores_decisions_and_official_quality(self):
        event = sample("yield:threshold")
        row = persist_treasury(self.engine, event, NOW)
        history = {h["kind"]: h["attributes"] for h in self.stored(row)[3]}
        self.assertEqual(set(history), {"score", "decision"})
        self.assertEqual(history["score"]["quality_adjustment"], 0)
        self.assertEqual(history["score"]["score_reasons"], event["score_reasons"])
        self.assertEqual((history["decision"]["alert_decision"], history["decision"]["initial_decision"]),
                         ("DISPLAY_ONLY", "ALERT"))
        self.assertIs(history["decision"]["alert_eligible"], False)
        self.assertEqual(history["decision"]["score_snapshot"]["impact_score"], 70)

    def test_persistence_never_rescores_or_reevaluates(self):
        from tests.test_treasury_pipeline import treasury
        event = sample()
        with patch.object(treasury, "score_treasury_event", side_effect=AssertionError("rescore")), \
             patch.object(treasury, "evaluate_alert", side_effect=AssertionError("re-decide")):
            row = persist_treasury(self.engine, event, NOW)
        self.assertEqual({h["kind"] for h in self.stored(row)[3]}, {"score", "decision"})

    def test_ai_present_absent_and_invalid(self):
        with_ai = persist_treasury(self.engine, sample("debt_limit:initial_with_ai"), NOW)
        ai = [h["attributes"] for h in self.stored(with_ai)[3] if h["kind"] == "ai"]
        self.assertEqual(ai, [dict(corpus.AI)])
        without = persist_treasury(self.engine, sample("issuance_policy:initial"), NOW)
        self.assertNotIn("ai", {h["kind"] for h in self.stored(without)[3]})
        invalid = sample("borrowing_estimates:initial")
        invalid.update(corpus.AI, ai_confidence=999)
        row = persist_treasury(self.engine, invalid, NOW)
        self.assertNotIn("ai", {h["kind"] for h in self.stored(row)[3]})

    def test_ai_fields_cannot_change_facts_or_version(self):
        base = sample("note_auction:result")
        enriched = deepcopy(base)
        enriched.update(corpus.AI)
        first = persist_treasury(self.engine, base, NOW)
        second = persist_treasury(self.engine, enriched, NOW, report=True)
        self.assertEqual(second["version"]["id"], first["id"])
        self.assertEqual(second["version"]["attributes"], first["attributes"])
        self.assertEqual({h["kind"] for h in self.stored(first)[3]}, {"score", "decision", "ai"})

    def test_duplicate_and_provenance_idempotency(self):
        event = sample("bill_auction:result")
        first = persist_treasury(self.engine, event, NOW, report=True)
        again = persist_treasury(self.engine, deepcopy(event), NOW, report=True)
        self.assertFalse(first["duplicate"])
        self.assertTrue(again["duplicate"])
        self.assertEqual((self.count(event_versions), self.count(event_provenance), self.count(event_history)), (1, 1, 2))
        event["source_hash"] = "c" * 64
        evidence = persist_treasury(self.engine, event, NOW, report=True)
        self.assertFalse(evidence["duplicate"])
        self.assertEqual(evidence["version"]["id"], first["version"]["id"])
        self.assertEqual(self.count(event_provenance), 2)
        self.assertTrue(persist_treasury(self.engine, event, NOW, report=True)["duplicate"])

    def test_auction_announcement_and_result_are_separate_events(self):
        announcement = persist_treasury(self.engine, sample("bill_auction:announcement"), NOW, report=True)
        result = persist_treasury(self.engine, sample("bill_auction:result"), NOW, report=True)
        self.assertEqual((announcement["promotion"], result["promotion"]), ("first", "first"))
        self.assertNotEqual(announcement["version"]["event_id"], result["version"]["event_id"])
        self.assertEqual(self.count(events), 2)
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            self.assertEqual(repo.current(announcement["version"]["event_id"])["stage"], "announcement")
            self.assertEqual(repo.current(result["version"]["event_id"])["stage"], "result")

    def test_reopening_identity_is_collector_owned(self):
        first = persist_treasury(self.engine, sample("reopening:result"), NOW)
        later = persist_treasury(self.engine, sample("reopening:later_auction_result"), NOW)
        self.assertNotEqual(first["event_id"], later["event_id"])
        self.assertEqual(first["attributes"]["cusip"], later["attributes"]["cusip"])
        self.assertTrue(later["attributes"]["reopening"])

    def test_invalid_identity_rejected_without_writes(self):
        for change in (dict(event_id=None), dict(event_type="macro_release"), dict(agency="bls")):
            event = sample()
            event.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                persist_treasury(self.engine, event, NOW)
        self.assertEqual(self.count(events), 0)

    def test_rollback_on_history_failure(self):
        with patch.object(EventRepository, "append_history", side_effect=PersistenceError("private")):
            with self.assertRaises(PersistenceError):
                persist_treasury(self.engine, sample(), NOW)
        for table in (events, event_versions, event_provenance, event_history):
            self.assertEqual(self.count(table), 0)

    def test_conflicting_version_key_rolls_back(self):
        original = persist_treasury(self.engine, sample(), NOW)
        inputs = adapt_treasury(sample("refunding:material"), NOW)["record"]
        inputs["version_key"] = original["version_key"]
        with self.assertRaises(IdentityConflict), transaction(self.engine) as session:
            EventRepository(session).record(**inputs, promotion_policy=treasury_promotion)
        self.assertEqual(self.count(event_versions), 1)

    def test_restart_deduplicates_from_database_not_memory(self):
        from persistence.treasury_shadow import TreasuryShadowWriter
        event = sample("yield:routine")
        for expected_duplicates in (0, 1):
            if self.engine.dialect.name == "sqlite":
                # In-memory SQLite is bound to this thread; each call is still a fresh repository.
                self.assertEqual(persist_treasury(self.engine, event, NOW, report=True)["duplicate"], bool(expected_duplicates))
                continue
            # Each writer is a fresh "process": no in-memory dedup state is shared.
            writer = TreasuryShadowWriter(engine_factory=lambda: self.engine)
            self.assertTrue(writer.submit(event))
            stats = writer.shutdown(timeout=5)["stats"]
            self.assertEqual((stats["persisted"], stats["duplicate"], stats["failed"]), (1, expected_duplicates, 0))
        self.assertEqual((self.count(events), self.count(event_versions)), (1, 1))
