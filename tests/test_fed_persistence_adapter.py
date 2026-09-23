"""Fed adapter fidelity, promotion, provenance, outcomes, reconciliation and the fixed replay corpus."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic import command

from persistence import reconciliation
from persistence.adapters.fed import adapt_fed, fed_promotion
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction, PersistenceError
from persistence.fed_shadow import persist_fed
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.reconciliation import reconcile_fed_event
from persistence.repository import EventRepository, IdentityConflict
from tests import fed_readiness_corpus as corpus
from tests.test_persistence import migration_config
from tests.test_fed_pipeline import deduplicator

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


@lru_cache(maxsize=1)
def _rows():
    return {row["label"]: row for row in corpus.load_corpus()[1]}


def sample(label="policy_statement_dry_run"):
    return deepcopy(_rows()[label]["event"])


def replay(engine):
    _, rows = corpus.load_corpus()
    duplicates, promotions = 0, Counter()
    for row in rows:
        result = persist_fed(engine, row["event"], datetime.fromisoformat(row["observed_at"]),
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
            results.append(dict(label=row["label"], **reconcile_fed_event(row["event"], repo, expect_current=row["expect_current"])))
        counts = {t.name: session.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
                  for t in (events, event_versions, event_provenance, event_history)}
    mismatches = [{"label": r["label"], "fields": r["mismatches"]} for r in results if r["mismatches"]]
    return dict(corpus_version=manifest["corpus_version"], observations=len(rows), matched=len(rows) - len(mismatches),
                mismatches=mismatches, duplicates=duplicates, counts=counts, results=results)


class FedAdapterTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        with self.engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        self.addCleanup(self.engine.dispose)

    def count(self, table):
        with transaction(self.engine) as session:
            return session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()

    def persist(self, event, observed_at=NOW, **kwargs):
        return persist_fed(self.engine, event, observed_at, report=True, **kwargs)

    def stored(self, version):
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            anchor = session.execute(sa.select(events).where(events.c.id == version["event_id"])).mappings().one()
            return dict(anchor), repo.current(version["event_id"]), repo.provenance(version["id"]), repo.history(version["id"])

    def current(self, version):
        with transaction(self.engine) as session:
            return EventRepository(session).current(version["event_id"])

    def reconcile(self, event, **kwargs):
        with transaction(self.engine) as session:
            return reconcile_fed_event(event, EventRepository(session), **kwargs)

    # ------------------------------------------------------------ adapter

    def test_category_roundtrips_preserve_collector_fingerprint(self):
        for label, category in (("policy_statement_dry_run", "policy_statement"), ("policy_action", "policy_action"),
                                ("economic_projections", "economic_projections"), ("minutes_ai_disabled", "minutes"),
                                ("policy_communication", "policy_communication"), ("symbol_mention", "policy_communication")):
            with self.subTest(label=label):
                original = sample(label)
                saved = deepcopy(original)
                version = self.persist(original)["version"]
                self.assertEqual(original, saved)  # Adapter never mutates the collector event.
                anchor, current, provenance, _ = self.stored(version)
                self.assertEqual((anchor["source_family"], anchor["identity_version"]), ("fed", "fed-v1"))
                self.assertEqual(anchor["event_key"], deduplicator.create_fingerprint(original))  # Collector-owned.
                self.assertEqual(current["id"], version["id"])
                for key in ("headline", "summary", "event_type", "market_scope"):
                    self.assertEqual(version[key], original[key])
                self.assertEqual((version["source_name"], version["publisher"], version["canonical_url"]),
                                 ("Federal Reserve", "Federal Reserve", original["url"]))
                self.assertEqual((version["stage"], version["attributes"]["fed_category"]), (category, category))
                for key in ("symbols", "direct_symbols", "related_symbols", "relevant"):
                    self.assertEqual(version["attributes"][key], original[key])
                self.assertFalse(any(k.startswith("ai_") for k in version["attributes"]))
                self.assertEqual(len(provenance), 1)

    def test_identity_is_required_never_recomputed(self):
        for change in (dict(fed_fingerprint=None), dict(fed_fingerprint="short"), dict(fed_fingerprint="Z" * 64),
                       dict(event_type="macro_release"), dict(url="")):
            event = sample()
            event.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                persist_fed(self.engine, event, NOW)
        self.assertEqual(self.count(events), 0)

    def test_precision_basis_and_symbols(self):
        dated = self.persist(sample())["version"]
        self.assertEqual((dated["timestamp_precision"], dated["publication_basis"]), ("second", "fed_rss_published_or_updated"))
        self.assertEqual(dated["published_at"], datetime(2026, 9, 16, 18, tzinfo=timezone.utc))
        self.assertEqual(dated["attributes"]["timestamp_precision_basis"], "parsed_feed_timestamp")
        undated = self.persist(sample("missing_date"), make_current=False)["version"]
        self.assertEqual((undated["timestamp_precision"], undated["published_at"], undated["publication_basis"]),
                         ("unknown", None, "unverified"))
        self.assertEqual(self.persist(sample("symbol_mention"))["version"]["attributes"]["direct_symbols"], ["NVDA"])
        self.assertEqual(dated["attributes"]["symbols"], [])  # No symbols invented.

    def test_provenance_fields_and_idempotency(self):
        event = sample()
        first = self.persist(event)
        self.assertTrue(self.persist(deepcopy(event))["duplicate"])
        [row] = self.stored(first["version"])[2]
        self.assertEqual((row["source_name"], row["canonical_url"], row["document_id"]), ("Federal Reserve", event["url"], None))
        self.assertEqual(row["attributes"]["feed_url"], "https://www.federalreserve.gov/feeds/press_monetary.xml")
        self.assertEqual(row["attributes"]["role"], "fed_monetary_policy_release")
        event["source_hash"] = "d" * 64  # New official evidence for the same version.
        evidence = self.persist(event)
        self.assertEqual((evidence["duplicate"], evidence["version"]["id"]), (False, first["version"]["id"]))
        self.assertEqual(self.count(event_provenance), 2)
        self.assertTrue(self.persist(event)["duplicate"])

    def test_scores_decisions_ai_present_and_absent(self):
        with_ai = self.persist(sample())["version"]
        history = {h["kind"]: h["attributes"] for h in self.stored(with_ai)[3]}
        self.assertEqual(set(history), {"score", "decision", "ai"})
        self.assertEqual((history["score"]["impact_score"], history["score"]["quality_adjustment"]), (85, 0))
        self.assertEqual(history["decision"]["alert_decision"], "ALERT")
        self.assertEqual(history["ai"], dict(corpus.AI))
        without = self.persist(sample("minutes_ai_disabled"))["version"]
        history = {h["kind"]: h["attributes"] for h in self.stored(without)[3]}
        self.assertEqual(set(history), {"score", "decision"})
        self.assertNotIn("quality_adjustment", history["score"])  # Deterministic Fed score sets none; not invented.

    def test_existing_ai_quality_penalty_is_recorded_not_rescored(self):
        penalized = corpus.run_collector([corpus.ENTRIES["policy_action"]], corpus.FedMemoryRedis())
        with patch.object(corpus, "AI", dict(corpus.AI, ai_event_type="opinion article")):
            _, submitted = corpus.run_collector([corpus.ENTRIES["policy_action"]], corpus.FedMemoryRedis())
        event = submitted[0][0]
        self.assertEqual((event["quality_adjustment"], event["impact_score"], event["original_impact_score"]), (-10, 75, 85))
        from tests.test_fed_pipeline import fed
        with patch.object(fed, "score_fed_event", side_effect=AssertionError("rescore")), \
             patch.object(fed, "evaluate_alert", side_effect=AssertionError("re-decide")), \
             patch.object(fed, "adjust_alert_quality", side_effect=AssertionError("re-adjust")):
            version = self.persist(event)["version"]
        score = next(h for h in self.stored(version)[3] if h["kind"] == "score")["attributes"]
        self.assertEqual((score["quality_adjustment"], score["impact_score"]), (-10, 75))
        self.assertTrue(penalized)

    def test_invalid_ai_not_persisted_and_cannot_change_facts(self):
        event = sample("policy_communication")
        event.update(corpus.AI, ai_sentiment="VERY_BULLISH")
        version = self.persist(event)["version"]
        self.assertNotIn("ai", {h["kind"] for h in self.stored(version)[3]})
        enriched = sample("policy_communication")
        enriched.update(corpus.AI)
        again = self.persist(enriched)
        self.assertEqual(again["version"]["id"], version["id"])  # AI is history, never a fact/version change.

    def test_rollback_and_conflicting_version_key(self):
        with patch.object(EventRepository, "append_history", side_effect=PersistenceError("private")):
            with self.assertRaises(PersistenceError):
                persist_fed(self.engine, sample(), NOW)
        for table in (events, event_versions, event_provenance, event_history):
            self.assertEqual(self.count(table), 0)
        original = persist_fed(self.engine, sample("policy_action"), NOW)
        inputs = adapt_fed(sample("synthetic_material_revision"), NOW)["record"]  # Same fingerprint.
        inputs["version_key"] = original["version_key"]
        with self.assertRaises(IdentityConflict), transaction(self.engine) as session:
            EventRepository(session).record(**inputs, promotion_policy=fed_promotion)
        self.assertEqual(self.count(event_versions), 1)

    # ---------------------------------------------------------- promotion

    def test_promotion_contract(self):
        base = self.persist(sample("policy_action"))["version"]
        newer = self.persist(sample("synthetic_material_revision"), NOW - timedelta(days=1))
        self.assertEqual(newer["promotion"], "newer_material")
        older = self.persist(sample("synthetic_older_observation"), NOW + timedelta(days=1))
        self.assertEqual(older["promotion"], "older")
        cosmetic = sample("synthetic_material_revision")
        cosmetic["summary"] = cosmetic["summary"].replace(" ", "  ")
        self.assertEqual(self.persist(cosmetic)["promotion"], "cosmetic")
        equal_time = sample("synthetic_material_revision")  # Same source time as the current version.
        equal_time["summary"] += " Unordered change."
        self.assertEqual(self.persist(equal_time)["promotion"], "ambiguous")
        undated = sample("synthetic_material_revision")
        undated.update(published_at=None, summary=undated["summary"] + " x")
        self.assertEqual(self.persist(undated)["promotion"], "ambiguous")
        recategorized = sample("synthetic_material_revision")
        recategorized.update(fed_category="minutes", summary=recategorized["summary"] + " y")
        recategorized["published_at"] = (datetime.fromisoformat(recategorized["published_at"]) + timedelta(hours=1)).isoformat()
        self.assertEqual(self.persist(recategorized)["promotion"], "ambiguous")
        self.assertEqual(self.current(base)["id"], newer["version"]["id"])

    def test_stale_rediscovery_and_missing_date_never_promote(self):
        current = self.persist(sample())["version"]
        rediscovered = self.persist(sample("stale_rediscovery"), NOW + timedelta(days=3), make_current=False)
        self.assertEqual((rediscovered["duplicate"], rediscovered["promotion"]), (True, "duplicate"))
        undated = sample()
        undated.update(published_at=None, summary=undated["summary"] + " changed")
        self.assertEqual(self.persist(undated, make_current=False)["promotion"], "caller_disabled")
        self.assertEqual(self.current(current)["id"], current["id"])
        with transaction(self.engine) as session:
            self.assertEqual(EventRepository(session).history(self.persist(sample("stale"), make_current=False)["version"]["id"]), [])

    # ------------------------------------------------ corpus/reconciliation

    def test_fixed_corpus_is_reproducible_and_covers_categories(self):
        manifest, rows = corpus.load_corpus()
        self.assertEqual((manifest, rows), corpus.load_corpus())
        self.assertEqual(len(rows), manifest["expected_observations"])
        self.assertEqual({r["event"]["fed_category"] for r in rows}, set(manifest["categories"]))
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), corpus.MANIFEST.with_suffix(".sha256").read_text().strip())

    def test_replay_reconciliation_and_repeat(self):
        manifest, rows = corpus.load_corpus()
        result = replay(self.engine)
        self.assertEqual(result["mismatches"], [])
        self.assertEqual((result["matched"], result["duplicates"]), (manifest["expected_observations"], manifest["expected_duplicates"]))
        self.assertEqual(result["counts"], manifest["expected_counts"])
        self.assertEqual(result["promotions"], manifest["expected_promotions"])
        again = replay(self.engine)
        self.assertEqual((again["mismatches"], again["duplicates"], again["counts"]), ([], len(rows), result["counts"]))
        print("PHASE2L_RECONCILIATION " + json.dumps({k: v for k, v in result.items() if k != "results"}, sort_keys=True))

    def test_reconciliation_success_mismatch_and_read_only(self):
        event = sample()
        self.assertFalse(self.reconcile(event)["event_found"])
        version = self.persist(event)["version"]
        self.assertEqual(self.reconcile(event)["mismatches"], [])
        with transaction(self.engine) as session:  # Deliberate fixture corruption; the audit only reads.
            session.execute(event_history.delete().where(event_history.c.event_version_id == version["id"]))
            session.execute(event_provenance.delete().where(event_provenance.c.event_version_id == version["id"]))
        with patch.dict(reconciliation._last_warning, clear=True), \
             self.assertLogs("macro_reconciliation", level="WARNING") as logs:
            result = self.reconcile(event)
            self.reconcile(event)
        for key in ("provenance_match", "score_match", "decision_match", "ai_match"):
            self.assertIn(key, result["mismatches"])
        self.assertEqual(logs.output, ["WARNING:macro_reconciliation:Fed shadow reconciliation mismatch"])
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            self.reconcile(event)
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        self.assertTrue(all(s.lstrip().upper().startswith(("SELECT", "BEGIN")) for s in statements), statements)

    def test_no_ai_is_not_applicable_and_restart_dedup(self):
        event = sample("minutes_ai_disabled")
        self.persist(event)
        self.assertIsNone(self.reconcile(event)["ai_match"])
        self.assertTrue(self.persist(event)["duplicate"])  # A fresh repository: dedup from DB state only.
