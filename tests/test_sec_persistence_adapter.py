"""SEC adapter fidelity, accession identity, provenance, outcomes, reconciliation and the fixed replay corpus."""
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic import command

from persistence import reconciliation
from persistence.adapters.sec import adapt_sec, filing_document, sec_promotion, FACTS
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction, PersistenceError
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.reconciliation import reconcile_sec_event
from persistence.repository import EventRepository, IdentityConflict
from persistence.sec_shadow import persist_sec
from tests import sec_readiness_corpus as corpus
from tests.test_persistence import migration_config
from tests.test_sec_pipeline import deduplicator

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
FORMS = {"meta_8k": ("8-K", 70, "HIGH", "ALERT"), "meta_10q": ("10-Q", 75, "HIGH", "ALERT"),
         "meta_10k": ("10-K", 80, "HIGH", "ALERT"), "meta_form4": ("4", 45, "MEDIUM", "DISPLAY_ONLY"),
         "meta_form144": ("144", 40, "MEDIUM", "DISPLAY_ONLY"), "meta_form3": ("3", 40, "MEDIUM", "DISPLAY_ONLY"),
         "meta_npx": ("N-PX", 35, "LOW", "IGNORE"), "meta_8ka": ("8-K/A", 35, "LOW", "IGNORE"),
         "nvda_8k": ("8-K", 70, "HIGH", "ALERT")}


@lru_cache(maxsize=1)
def _rows():
    return {row["label"]: row for row in corpus.load_corpus()[1]}


def sample(label="meta_8k"):
    return deepcopy(_rows()[label]["event"])


def collector_fingerprint(event):
    return deduplicator.create_fingerprint({k: v for k, v in event.items() if k != "sec_fingerprint"})


def replay(engine):
    _, rows = corpus.load_corpus()
    duplicates, promotions = 0, Counter()
    for row in rows:
        result = persist_sec(engine, row["event"], datetime.fromisoformat(row["observed_at"]),
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
            results.append(dict(label=row["label"], **reconcile_sec_event(row["event"], repo, expect_current=row["expect_current"])))
        counts = {t.name: session.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
                  for t in (events, event_versions, event_provenance, event_history)}
    mismatches = [{"label": r["label"], "fields": r["mismatches"]} for r in results if r["mismatches"]]
    return dict(corpus_version=manifest["corpus_version"], observations=len(rows), matched=len(rows) - len(mismatches),
                mismatches=mismatches, duplicates=duplicates, counts=counts, results=results)


class SecAdapterTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        with self.engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        self.addCleanup(self.engine.dispose)

    def count(self, table):
        with transaction(self.engine) as session:
            return session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()

    def persist(self, event, observed_at=NOW, **kwargs):
        return persist_sec(self.engine, event, observed_at, report=True, **kwargs)

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
            return reconcile_sec_event(event, EventRepository(session), **kwargs)

    # ------------------------------------------------------------ adapter

    def test_form_roundtrips_preserve_collector_fingerprint_and_facts(self):
        for label, (form, score, level, decision) in FORMS.items():
            with self.subTest(label=label):
                original = sample(label)
                saved = deepcopy(original)
                version = self.persist(original)["version"]
                self.assertEqual(original, saved)  # Adapter never mutates the collector event.
                anchor, current, provenance, history = self.stored(version)
                self.assertEqual((anchor["source_family"], anchor["identity_version"]), ("sec", "sec-v1"))
                self.assertEqual(anchor["event_key"], collector_fingerprint(original))  # Collector-owned.
                self.assertEqual(current["id"], version["id"])
                self.assertEqual((version["headline"], version["summary"], version["canonical_url"], version["event_type"]),
                                 (original["headline"], original["summary"], original["url"], "sec_filing"))
                self.assertEqual((version["source_name"], version["publisher"]), ("SEC EDGAR", "SEC"))
                self.assertEqual((version["timestamp_precision"], version["publication_basis"], version["published_at"]),
                                 ("date", "sec_submissions_filing_date", None))
                self.assertEqual(version["publication_date"], date.fromisoformat(original["published_at"]))
                self.assertEqual((version["stage"], version["revision_key"], version["market_scope"]), (None, None, None))
                for key in FACTS:
                    self.assertEqual(version["attributes"][key], original[key])
                self.assertEqual(version["attributes"]["sec_form"], form)
                self.assertEqual(len(provenance), 1)
                self.assertEqual({h["kind"]: h["attributes"] for h in history}, {
                    "score": dict(impact_score=score, impact_level=level, score_reasons=original["score_reasons"]),
                    "decision": dict(alert_decision=decision, score_snapshot=dict(
                        impact_score=score, impact_level=level, score_reasons=original["score_reasons"]))})

    def test_accession_extraction_is_deterministic_and_fails_closed(self):
        for label, row in _rows().items():
            with self.subTest(label=label):
                event = row["event"]
                self.assertEqual(filing_document(event)[0], event["accession_number"])
        self.assertEqual(filing_document(sample("meta_form4")),
                         ("0001209191-26-054321", "xslF345X05/wk-form4_1758220000.xml"))
        self.assertEqual(filing_document(sample("missing_optional_metadata")), ("0001209191-26-054323", None))
        bad = [dict(accession_number="0001326801-26-000999"),  # URL belongs to another accession.
               dict(accession_number="000132680126000101"), dict(accession_number=None),
               dict(url="https://example.com/Archives/edgar/data/1326801/000132680126000101/x.htm"),
               dict(url="https://www.sec.gov/Archives/edgar/data/1326801/0001326801-26-000101/x.htm"),
               dict(url=None)]
        for change in bad:
            event = sample()
            event.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                persist_sec(self.engine, event, NOW)
        self.assertEqual(self.count(events), 0)

    def test_identity_is_required_never_recomputed(self):
        for change in (dict(sec_fingerprint=None), dict(sec_fingerprint="short"), dict(sec_fingerprint="Z" * 64),
                       dict(event_type="fed_policy"), dict(headline=""), dict(sec_form="")):
            event = sample()
            event.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                persist_sec(self.engine, event, NOW)
        self.assertEqual(self.count(events), 0)
        supplied = sample()
        supplied["sec_fingerprint"] = "a" * 64  # Persistence trusts the collector; it never rehashes.
        version = self.persist(supplied)["version"]
        self.assertEqual(self.stored(version)[0]["event_key"], "a" * 64)

    def test_missing_optional_metadata_and_invalid_dates(self):
        version = self.persist(sample("missing_optional_metadata"))["version"]
        self.assertEqual((version["timestamp_precision"], version["publication_basis"], version["publication_date"]),
                         ("unknown", "unverified", None))
        self.assertIsNone(version["attributes"]["primary_document"])
        provenance = self.stored(version)[2][0]
        self.assertEqual((provenance["attributes"]["filing_date"], provenance["attributes"]["primary_document"]), (None, None))
        for value in ("2026-9-1", "2026-09-21T00:00:00Z", "yesterday", 20260921):
            event = sample()
            event["published_at"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                persist_sec(self.engine, event, NOW)

    def test_no_invented_fields_and_no_ai_history(self):
        event = sample()
        event.update(ai_summary="Not produced by SEC", ai_sentiment="NEUTRAL", ai_confidence=50,
                     ai_why_it_matters="x", ai_event_type="filing", quality_adjustment=-5)
        version = self.persist(event)["version"]
        _, _, provenance, history = self.stored(version)
        self.assertEqual(set(version["attributes"]), {*FACTS, "primary_document"})
        self.assertEqual({h["kind"] for h in history}, {"score", "decision"})  # Never an AI record.
        self.assertEqual(next(h for h in history if h["kind"] == "score")["attributes"]["quality_adjustment"], -5)
        stored = json.dumps([version["attributes"], provenance[0]["attributes"]], default=str).lower()
        for invented in ("cik", "report_date", "item", "amend", "category", "ai_"):
            self.assertNotIn(invented, stored)
        self.assertNotIn("quality_adjustment", self.stored(self.persist(sample("meta_10q"))["version"])[3][0]["attributes"])

    def test_provenance_fields_and_idempotency(self):
        first = self.persist(sample("meta_form4"))
        provenance = self.stored(first["version"])[2]
        self.assertEqual(len(provenance), 1)
        row = provenance[0]
        self.assertEqual((row["source_name"], row["document_id"], row["canonical_url"]),
                         ("SEC EDGAR", "0001209191-26-054321", sample("meta_form4")["url"]))
        self.assertEqual(row["attributes"], dict(
            role="sec_filing", publisher="SEC", accession_number="0001209191-26-054321", sec_form="4",
            filing_date="2026-09-18", primary_document="xslF345X05/wk-form4_1758220000.xml",
            timestamp_precision="date", publication_basis="sec_submissions_filing_date",
            retrieval_basis="shadow_observation"))
        again = self.persist(sample("meta_form4"), NOW + timedelta(days=2))
        self.assertEqual((again["duplicate"], again["promotion"], again["version"]["id"]),
                         (True, "duplicate", first["version"]["id"]))
        for table, expected in ((events, 1), (event_versions, 1), (event_provenance, 1), (event_history, 2)):
            self.assertEqual(self.count(table), expected)

    def test_rollback_and_conflicting_version_key(self):
        with patch.object(EventRepository, "append_history", side_effect=PersistenceError("private")):
            with self.assertRaises(PersistenceError):
                persist_sec(self.engine, sample(), NOW)
        for table in (events, event_versions, event_provenance, event_history):
            self.assertEqual(self.count(table), 0)
        original = persist_sec(self.engine, sample(), NOW)
        changed = sample()
        changed["summary"] += " (synthetic normalizer change)"
        inputs = adapt_sec(changed, NOW)["record"]  # Same fingerprint, different content.
        inputs["version_key"] = original["version_key"]
        with self.assertRaises(IdentityConflict), transaction(self.engine) as session:
            EventRepository(session).record(**inputs, promotion_policy=sec_promotion)
        self.assertEqual(self.count(event_versions), 1)

    # ---------------------------------------------------------- versions

    def test_one_filing_one_version_and_conservative_promotion(self):
        base = self.persist(sample())["version"]
        for observed in (NOW + timedelta(hours=1), NOW + timedelta(days=2)):
            self.assertEqual(self.persist(sample(), observed)["promotion"], "duplicate")
        # Only a changed normalizer output could add a version for the same identity; it is held.
        prose = sample()
        prose["summary"] += " (synthetic normalizer change)"
        self.assertEqual(self.persist(prose)["promotion"], "ambiguous")  # Same filing date: unordered.
        cosmetic = sample()
        cosmetic["summary"] = cosmetic["summary"].replace(" ", "  ")
        self.assertEqual(self.persist(cosmetic)["promotion"], "cosmetic")
        refiled = sample()
        refiled.update(accession_number="0001326801-26-000999")
        refiled["url"] = refiled["url"].replace("000132680126000101", "000132680126000999")
        self.assertEqual(self.persist(refiled)["promotion"], "ambiguous")  # Changed accession never merges.
        self.assertEqual(self.current(base)["id"], base["id"])
        self.assertEqual(self.count(events), 1)
        # The source-order contract is unchanged: a strictly later filing date would promote.
        later = sample()
        later.update(published_at="2026-09-22", summary=later["summary"] + " later")
        self.assertEqual(self.persist(later)["promotion"], "newer_material")

    # ------------------------------------------------ corpus/reconciliation

    def test_fixed_corpus_is_reproducible_and_covers_forms(self):
        manifest, rows = corpus.load_corpus()
        self.assertEqual((manifest, rows), corpus.load_corpus())
        self.assertEqual(len(rows), manifest["expected_observations"])
        self.assertEqual({r["event"]["sec_form"] for r in rows}, set(manifest["forms"]))
        self.assertEqual(dict(Counter(r["event"]["alert_decision"] for r in rows)), manifest["expected_decisions"])
        self.assertEqual(corpus.build_rows()[1], manifest["expected_collector_outcomes"])
        for row in rows:
            self.assertEqual(row["event"]["sec_fingerprint"], collector_fingerprint(row["event"]))
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), corpus.MANIFEST.with_suffix(".sha256").read_text().strip())

    def test_replay_reconciliation_and_repeat(self):
        manifest, rows = corpus.load_corpus()
        result = replay(self.engine)
        self.assertEqual(result["mismatches"], [])
        self.assertEqual((result["matched"], result["duplicates"]), (manifest["expected_observations"], manifest["expected_duplicates"]))
        self.assertEqual(result["counts"], manifest["expected_counts"])
        self.assertEqual(result["promotions"], manifest["expected_promotions"])
        self.assertTrue(all(r["ai_match"] is None and r["accession_match"] for r in result["results"]))
        again = replay(self.engine)
        self.assertEqual((again["mismatches"], again["duplicates"], again["counts"]), ([], len(rows), result["counts"]))
        print("PHASE2OB_RECONCILIATION " + json.dumps({k: v for k, v in result.items() if k != "results"}, sort_keys=True))

    def test_reconciliation_success_mismatch_and_read_only(self):
        event = sample()
        self.assertFalse(self.reconcile(event)["event_found"])
        version = self.persist(event)["version"]
        result = self.reconcile(event)
        self.assertEqual((result["mismatches"], result["ai_match"], result["accession_match"]), ([], None, True))
        with patch.dict(reconciliation._last_warning, clear=True), \
             self.assertLogs("macro_reconciliation", level="WARNING"):
            self.assertIn("score_match", self.reconcile(dict(event, impact_score=99))["mismatches"])
            self.assertIn("decision_match", self.reconcile(dict(event, alert_decision="IGNORE"))["mismatches"])
        with transaction(self.engine) as session:  # Deliberate fixture corruption; the audit only reads.
            session.execute(event_provenance.update().where(event_provenance.c.event_version_id == version["id"])
                            .values(document_id="0000000000-00-000000"))
            session.execute(event_history.delete().where(event_history.c.event_version_id == version["id"]))
        with patch.dict(reconciliation._last_warning, clear=True), \
             self.assertLogs("macro_reconciliation", level="WARNING") as logs:
            result = self.reconcile(event)
            self.reconcile(event)
        for key in ("provenance_match", "accession_match", "score_match", "decision_match"):
            self.assertIn(key, result["mismatches"])
        self.assertNotIn("ai_match", result["mismatches"])
        self.assertEqual(logs.output, ["WARNING:macro_reconciliation:SEC shadow reconciliation mismatch"])
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            self.reconcile(event)
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        self.assertTrue(all(s.lstrip().upper().startswith(("SELECT", "BEGIN")) for s in statements), statements)

    def test_restart_dedup_from_database_state(self):
        event = sample("nvda_8k")
        self.persist(event)
        self.assertTrue(self.persist(event, NOW + timedelta(days=5))["duplicate"])  # Fresh repository.
        self.assertEqual(self.reconcile(event)["mismatches"], [])


if __name__ == "__main__":
    unittest.main()
