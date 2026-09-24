"""News adapter fidelity, exact URL identity, versions, provenance, outcomes, reconciliation and the replay corpus."""
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
from persistence.adapters.news import adapt_news, article_identity, news_promotion, publication
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction, PersistenceError
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.news_shadow import persist_news
from persistence.reconciliation import reconcile_news_event
from persistence.repository import EventRepository, IdentityConflict
from tests import news_readiness_corpus as corpus

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


@lru_cache(maxsize=1)
def _rows():
    return {row["label"]: row for row in corpus.load_corpus()[1]}


def sample(label="meta_reuters"):
    return deepcopy(_rows()[label]["event"])


def url_key(url):
    return hashlib.sha256(f"news-url-v1|{url.strip().lower()}".encode()).hexdigest()


def replay(engine):
    _, rows = corpus.load_corpus()
    duplicates, promotions = 0, Counter()
    for row in rows:
        result = persist_news(engine, row["event"], datetime.fromisoformat(row["observed_at"]),
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
            results.append(dict(label=row["label"], **reconcile_news_event(row["event"], repo, expect_current=row["expect_current"])))
        counts = {t.name: session.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
                  for t in (events, event_versions, event_provenance, event_history)}
    mismatches = [{"label": r["label"], "fields": r["mismatches"]} for r in results if r["mismatches"]]
    return dict(corpus_version=manifest["corpus_version"], observations=len(rows), matched=len(rows) - len(mismatches),
                mismatches=mismatches, duplicates=duplicates, counts=counts, results=results)


class NewsAdapterTests(unittest.TestCase):
    def setUp(self):
        from tests.test_persistence import migration_config
        self.engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        with self.engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        self.addCleanup(self.engine.dispose)

    def count(self, table):
        with transaction(self.engine) as session:
            return session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()

    def persist(self, event, observed_at=NOW, **kwargs):
        return persist_news(self.engine, event, observed_at, report=True, **kwargs)

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
            return reconcile_news_event(event, EventRepository(session), **kwargs)

    # ------------------------------------------------------------ adapter

    def test_processed_alert_roundtrip(self):
        original = sample()
        saved = deepcopy(original)
        version = self.persist(original)["version"]
        self.assertEqual(original, saved)  # Adapter never mutates the collector event.
        anchor, current, provenance, history = self.stored(version)
        self.assertEqual((anchor["source_family"], anchor["identity_version"], anchor["event_key"]),
                         ("news", "news-url-v1", url_key(original["url"])))
        self.assertEqual(current["id"], version["id"])
        self.assertEqual((version["headline"], version["summary"], version["canonical_url"]),
                         (original["headline"], original["summary"], original["url"]))
        self.assertEqual((version["source_name"], version["publisher"]), ("Reuters", "Reuters"))
        self.assertEqual((version["published_at"], version["timestamp_precision"], version["publication_basis"]),
                         (datetime.fromisoformat(original["published_at"]), "second", "rss_published"))
        self.assertEqual(version["attributes"], dict(symbols=["META"], direct_symbols=["META"], related_symbols=[],
                                                     relevant=True))
        self.assertEqual(len(provenance), 1)
        row = provenance[0]
        self.assertEqual((row["source_name"], row["canonical_url"], row["document_id"]),
                         ("Google News META", original["url"], None))
        self.assertEqual((row["attributes"]["feed"], row["attributes"]["publisher"], row["attributes"]["collector_fingerprint"],
                          row["attributes"]["identity"]), ("Google News META", "Reuters", original["news_fingerprint"], "news-url-v1"))
        kinds = {h["kind"]: h["attributes"] for h in history}
        self.assertEqual(kinds["score"], dict(impact_score=90, original_impact_score=90, impact_level="HIGH",
                                              quality_adjustment=0, score_reasons=original["score_reasons"]))
        self.assertEqual((kinds["decision"]["alert_decision"], kinds["decision"]["collector_outcome"]), ("ALERT", "processed"))
        self.assertEqual(kinds["ai"], dict(ai_summary="Meta released a creator model.", ai_sentiment="BULLISH",
                                           ai_confidence=78, ai_why_it_matters="Creator tools may lift engagement.",
                                           ai_event_type="product launch"))

    def test_exact_identity_is_the_collector_normalized_url_only(self):
        base = sample()
        self.assertEqual(article_identity(base), ("news-url-v1", url_key(base["url"])))
        variants = [dict(base, url="  " + base["url"].upper() + " "), dict(base, headline="Totally different words"),
                    dict(base, publisher="Other", source="Other feed"), dict(base, ai_event_type="opinion"),
                    dict(base, summary="changed")]
        for variant in variants:  # Case/whitespace as the collector normalizes; nothing else participates.
            with self.subTest(variant=variant):
                self.assertEqual(article_identity(variant), article_identity(base))
        self.assertNotEqual(article_identity(dict(base, url=base["url"] + "-2")), article_identity(base))
        missing = sample("no_link")
        self.assertEqual(missing["url"], "N/A")
        self.assertEqual(article_identity(missing), ("news-fingerprint-v1", missing["news_fingerprint"]))
        for url in ("N/A", "", None, "ftp://x.example/a", "https://", "javascript:void(0)"):
            with self.subTest(url=url):
                self.assertEqual(article_identity(dict(base, url=url))[0], "news-fingerprint-v1")
        version = self.persist(missing)["version"]
        self.assertIsNone(version["canonical_url"])
        self.assertEqual(self.stored(version)[2][0]["canonical_url"], "N/A")  # Provenance keeps what was exposed.

    def test_same_headline_different_url_never_merges(self):
        results = [self.persist(sample(label)) for label in
                   ("meta_reuters", "same_headline_same_publisher", "same_headline_other_publisher")]
        self.assertEqual([r["promotion"] for r in results], ["first"] * 3)
        self.assertEqual(len({r["version"]["event_id"] for r in results}), 3)
        for result in results[1:]:
            history = self.stored(result["version"])[3]
            self.assertEqual([(h["kind"], h["attributes"]) for h in history],
                             [("decision", dict(collector_outcome="near_duplicate_suppressed"))])

    def test_same_url_headline_change_is_a_held_version(self):
        base = self.persist(sample())["version"]
        edited = self.persist(sample("headline_small_edit"))  # Near-duplicate suppressed by the collector.
        self.assertEqual((edited["version"]["event_id"], edited["promotion"]), (base["event_id"], "ambiguous"))
        rewrite_base = self.persist(sample("nvda_yahoo"))["version"]
        rewrite = self.persist(sample("headline_rewrite"))  # Processed by the collector (dissimilar headline).
        self.assertEqual((rewrite["version"]["event_id"], rewrite["promotion"]), (rewrite_base["event_id"], "ambiguous"))
        self.assertEqual((self.current(base)["id"], self.current(rewrite_base)["id"]), (base["id"], rewrite_base["id"]))
        self.assertEqual((self.count(events), self.count(event_versions)), (2, 4))
        cosmetic = sample()
        cosmetic["headline"] = cosmetic["headline"].replace(" ", "  ")
        self.assertEqual(self.persist(cosmetic)["promotion"], "cosmetic")
        later = sample("headline_rewrite")
        later["published_at"] = (datetime.fromisoformat(later["published_at"]) + timedelta(hours=1)).isoformat()
        self.assertEqual(self.persist(later)["promotion"], "newer_material")  # Strictly later source time only.
        self.assertEqual(self.count(events), 2)

    def test_publication_precision_never_guessed(self):
        self.assertEqual(publication(None)[1:], ("unknown", "no_feed_date", None))
        self.assertEqual(publication("yesterday afternoon")[1:], ("unknown", "unparsed_feed_date", "yesterday afternoon"))
        self.assertEqual(publication("2026-09-24T10:00:00")[1:], ("unknown", "unparsed_feed_date", "2026-09-24T10:00:00"))
        missing = self.persist(sample("missing_timestamp"))["version"]
        self.assertEqual((missing["published_at"], missing["timestamp_precision"], missing["publication_basis"]),
                         (None, "unknown", "no_feed_date"))
        unparsed = self.persist(sample("unparsed_timestamp"))["version"]
        self.assertEqual((unparsed["timestamp_precision"], unparsed["attributes"]["published_at_raw"]),
                         ("unknown", "yesterday afternoon"))
        stale = self.persist(sample("stale"))["version"]
        self.assertEqual((stale["timestamp_precision"], stale["published_at"].isoformat()), ("second", "2026-09-21T12:00:00+00:00"))

    def test_scores_decisions_and_ai_exactly_as_computed(self):
        cases = {"ai_penalty": (65, 80, -15, "MEDIUM", "DISPLAY_ONLY", True),
                 "ai_failure": (80, None, None, "HIGH", "ALERT", False),
                 "nvda_yahoo": (68, None, None, "MEDIUM", "DISPLAY_ONLY", False),
                 "near_duplicate_below": (70, 80, -10, "HIGH", "ALERT", True)}
        for label, (score, original, adjustment, level, decision, has_ai) in cases.items():
            with self.subTest(label=label):
                event = sample(label)
                history = {h["kind"]: h["attributes"] for h in self.stored(self.persist(event)["version"])[3]}
                self.assertEqual((history["score"]["impact_score"], history["score"].get("original_impact_score"),
                                  history["score"].get("quality_adjustment"), history["score"]["impact_level"]),
                                 (score, original, adjustment, level))
                self.assertEqual(history["score"]["score_reasons"], event["score_reasons"])
                self.assertEqual(history["decision"]["alert_decision"], decision)
                self.assertEqual("ai" in history, has_ai)
        partial = sample("low_quality")
        partial.update(ai_summary="partial", ai_sentiment="SIDEWAYS")  # Not validated: never persisted.
        history = self.stored(self.persist(partial)["version"])[3]
        self.assertNotIn("ai", {h["kind"] for h in history})

    def test_ai_and_outcomes_never_change_facts_or_identity(self):
        plain = sample("ai_failure")
        first = self.persist(plain)["version"]
        enriched = dict(plain, ai_summary="x", ai_sentiment="NEUTRAL", ai_confidence=5, ai_why_it_matters="y",
                        ai_event_type="z")
        again = self.persist(enriched)
        self.assertEqual((again["version"]["id"], again["promotion"]), (first["id"], "duplicate"))
        suppressed = {k: v for k, v in plain.items() if k not in (
            "impact_score", "impact_level", "score_reasons", "alert_decision")}
        suppressed["news_collector_outcome"] = "near_duplicate_suppressed"
        self.assertEqual(self.persist(suppressed)["version"]["id"], first["id"])
        self.assertEqual({h["kind"] for h in self.stored(first)[3]}, {"score", "decision", "ai"})

    def test_invalid_snapshots_fail_closed(self):
        base = sample()
        bad = [dict(base, news_fingerprint=None), dict(base, news_fingerprint="Z" * 64), dict(base, relevant=False),
               dict(base, news_collector_outcome="duplicate"), dict(base, headline=None),
               {k: v for k, v in base.items() if k != "alert_decision"},
               dict(base, news_collector_outcome="near_duplicate_suppressed")]
        for index, event in enumerate(bad):
            with self.subTest(case=index), self.assertRaises(ValueError):
                persist_news(self.engine, event, NOW)
        self.assertEqual(self.count(events), 0)

    def test_provenance_idempotent_and_per_feed(self):
        first = self.persist(sample("related_cnbc"))
        again = self.persist(sample("related_cnbc"), NOW + timedelta(days=1))
        self.assertEqual((again["version"]["id"], again["duplicate"]), (first["version"]["id"], True))
        other_feed = self.persist(sample("cross_feed_after_expiry"), NOW + timedelta(days=1, hours=2))
        self.assertEqual((other_feed["version"]["id"], other_feed["promotion"]), (first["version"]["id"], "duplicate"))
        feeds = sorted(p["source_name"] for p in self.stored(first["version"])[2])
        self.assertEqual(feeds, ["Google News META", "Google News NVDA"])  # Where it was seen; one article version.

    def test_rollback_and_conflicting_version_key(self):
        with patch.object(EventRepository, "append_history", side_effect=PersistenceError("private")):
            with self.assertRaises(PersistenceError):
                persist_news(self.engine, sample(), NOW)
        for table in (events, event_versions, event_provenance, event_history):
            self.assertEqual(self.count(table), 0)
        original = persist_news(self.engine, sample(), NOW)
        inputs = adapt_news(sample("headline_small_edit"), NOW)["record"]  # Same URL identity.
        inputs["version_key"] = original["version_key"]
        with self.assertRaises(IdentityConflict), transaction(self.engine) as session:
            EventRepository(session).record(**inputs, promotion_policy=news_promotion)
        self.assertEqual(self.count(event_versions), 1)

    # ------------------------------------------------ corpus/reconciliation

    def test_fixed_corpus_is_reproducible(self):
        manifest, rows = corpus.load_corpus()
        self.assertEqual((manifest, rows), corpus.load_corpus())
        self.assertEqual(len(rows), manifest["expected_observations"])
        self.assertEqual(dict(Counter(str(r["event"].get("alert_decision")) for r in rows)), manifest["expected_decisions"])
        self.assertEqual(dict(Counter(r["event"]["news_collector_outcome"] for r in rows)), manifest["expected_outcomes"])
        self.assertEqual(corpus.build_rows()[1], manifest["expected_collector_outcomes"])
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), corpus.MANIFEST.with_suffix(".sha256").read_text().strip())

    def test_replay_reconciliation_and_repeat(self):
        manifest, rows = corpus.load_corpus()
        result = replay(self.engine)
        self.assertEqual(result["mismatches"], [])
        self.assertEqual((result["matched"], result["duplicates"]), (manifest["expected_observations"], manifest["expected_duplicates"]))
        self.assertEqual(result["counts"], manifest["expected_counts"])
        self.assertEqual(result["promotions"], manifest["expected_promotions"])
        with transaction(self.engine) as session:
            identities = dict(session.execute(sa.select(events.c.identity_version, sa.func.count())
                                              .group_by(events.c.identity_version)).all())
        self.assertEqual(identities, manifest["expected_identities"])
        again = replay(self.engine)
        self.assertEqual((again["mismatches"], again["duplicates"], again["counts"]), ([], len(rows), result["counts"]))
        print("PHASE2Q_RECONCILIATION " + json.dumps({k: v for k, v in result.items() if k != "results"}, sort_keys=True))

    def test_reconciliation_success_mismatch_and_read_only(self):
        event = sample()
        with patch.dict(reconciliation._last_warning, clear=True), \
             self.assertLogs("macro_reconciliation", level="WARNING"):
            self.assertFalse(self.reconcile(event)["event_found"])
        version = self.persist(event)["version"]
        result = self.reconcile(event)
        self.assertEqual((result["mismatches"], result["ai_match"]), ([], True))
        suppressed = sample("same_headline_same_publisher")
        self.persist(suppressed)
        self.assertEqual(self.reconcile(suppressed)["mismatches"], [])
        with patch.dict(reconciliation._last_warning, clear=True), \
             self.assertLogs("macro_reconciliation", level="WARNING"):
            self.assertIn("decision_match", self.reconcile(dict(suppressed, news_collector_outcome="processed",
                                                                impact_score=1, impact_level="LOW", score_reasons=[],
                                                                alert_decision="IGNORE"))["mismatches"])
            self.assertIn("ai_match", self.reconcile(dict(event, ai_confidence=1))["mismatches"])
        with transaction(self.engine) as session:  # Deliberate fixture corruption; the audit only reads.
            session.execute(event_history.delete().where(event_history.c.event_version_id == version["id"]))
            session.execute(event_provenance.delete().where(event_provenance.c.event_version_id == version["id"]))
        with patch.dict(reconciliation._last_warning, clear=True), \
             self.assertLogs("macro_reconciliation", level="WARNING") as logs:
            result = self.reconcile(event)
            self.reconcile(event)
        for key in ("provenance_match", "score_match", "decision_match", "ai_match"):
            self.assertIn(key, result["mismatches"])
        self.assertEqual(logs.output, ["WARNING:macro_reconciliation:News shadow reconciliation mismatch"])
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
        event = sample("stale")
        self.persist(event)
        self.assertIsNone(self.reconcile(event)["ai_match"])
        self.assertTrue(self.persist(event, NOW + timedelta(days=5))["duplicate"])  # Fresh repository: DB state only.


if __name__ == "__main__":
    unittest.main()
