"""Geopolitical readiness: fixed replay, stage-safe promotion, alias-TTL durability, audit."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from persistence import geopolitical_shadow as shadow
from persistence import reconciliation
from persistence.database import transaction
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.reconciliation import reconcile_geopolitical_event
from persistence.repository import EventRepository
from tests import geopolitical_readiness_corpus as corpus
from tests import test_geopolitical_persistence_adapter as adapter
from tests.test_geopolitical_persistence_adapter import sample, NOW
from tests.test_geopolitical_pipeline import MemoryRedis, identity, NOW as COLLECTOR_NOW


def replay(engine):
    _, rows = corpus.load_corpus()
    duplicates, promotions = 0, Counter()
    for row in rows:
        result = shadow.persist_geopolitical(engine, row["event"], datetime.fromisoformat(row["observed_at"]),
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
            result = reconcile_geopolitical_event(row["event"], repo, expect_current=row["expect_current"])
            results.append(dict(label=row["label"], **result))
        counts = {table.name: session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
                  for table in (events, event_versions, event_provenance, event_history)}
    mismatches = [{"label": r["label"], "fields": r["mismatches"]} for r in results if r["mismatches"]]
    return dict(corpus_version=manifest["corpus_version"], observations=len(rows),
                matched=len(rows) - len(mismatches), mismatches=mismatches,
                duplicates=duplicates, counts=counts, results=results)


class GeopoliticalReadinessTests(unittest.TestCase):
    setUp = adapter.GeopoliticalAdapterTests.setUp
    count = adapter.GeopoliticalAdapterTests.count

    def persist(self, event, observed_at=NOW, **kwargs):
        return shadow.persist_geopolitical(self.engine, event, observed_at, report=True, **kwargs)

    def current(self, version):
        with transaction(self.engine) as session:
            return EventRepository(session).current(version["event_id"])

    def reconcile(self, event, **kwargs):
        with transaction(self.engine) as session:
            return reconcile_geopolitical_event(event, EventRepository(session), **kwargs)

    def test_fixed_corpus_is_reproducible_and_covers_families(self):
        manifest, rows = corpus.load_corpus()
        self.assertEqual((manifest, rows), corpus.load_corpus())
        self.assertEqual(len(rows), manifest["expected_observations"])
        self.assertEqual({r["event"]["event_type"] for r in rows}, set(manifest["families"]))
        labels = {r["label"] for r in rows}
        self.assertFalse(labels & set(manifest["expected_not_persisted"]))  # No identity invented.
        self.assertTrue(all(r["event"]["quality_adjustment"] == 0 for r in rows if "impact_score" in r["event"]))
        self.assertTrue(all(r["event"]["symbols"] for r in rows))  # Only relevant, resolved events.
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), corpus.MANIFEST.with_suffix(".sha256").read_text().strip())

    def test_replay_full_read_only_reconciliation_and_repeat(self):
        manifest, rows = corpus.load_corpus()
        result = replay(self.engine)
        self.assertEqual(result["mismatches"], [])
        self.assertEqual(result["matched"], manifest["expected_observations"])
        self.assertEqual(result["duplicates"], manifest["expected_duplicates"])
        self.assertEqual(result["counts"], manifest["expected_counts"])
        self.assertEqual(result["promotions"], manifest["expected_promotions"])
        again = replay(self.engine)
        self.assertEqual((again["mismatches"], again["duplicates"], again["counts"]), ([], len(rows), result["counts"]))
        self.assertEqual(again["promotions"], {"duplicate": len(rows)})
        print("PHASE2F_RECONCILIATION " + json.dumps({k: v for k, v in result.items() if k != "results"}, sort_keys=True))

    def test_expected_outcome_relevance_and_relationship_checks_per_row(self):
        replay(self.engine)
        _, rows = corpus.load_corpus()
        results = {r["label"]: r for r in reconcile_corpus(self.engine)["results"]}
        for row in rows:
            event, result = row["event"], results[row["label"]]
            self.assertTrue(result["relevance_match"] and result["relationship_match"], row["label"])
            self.assertEqual(result["score_match"], True if "impact_score" in event else None, row["label"])
            self.assertEqual(result["ai_match"], True if "ai_summary" in event else None, row["label"])

    def test_stages_are_distinct_logical_events(self):
        labels = ("bis_final", "clarification", "amendment", "proposal", "final_of_proposal", "correction")
        ids = {label: self.persist(sample(label))["version"]["event_id"] for label in labels}
        self.assertEqual(len(set(ids.values())), len(labels))
        self.assertEqual(self.count(events), len(labels))
        # Same instrument anchor, different substantive stage: separate collector identities.
        self.assertIn("fr:2026-99901", sample("clarification")["identity_anchors"])
        self.assertEqual(sample("clarification")["policy_stage"], "amended")

    def test_newer_comparable_material_version_promotes(self):
        first = self.persist(sample("trade"))
        newer = sample("trade")
        newer["summary"] += " Synthetic substantive revision."
        newer["published_at"] = (datetime.fromisoformat(newer["published_at"]) + timedelta(hours=1)).isoformat()
        result = self.persist(newer, NOW - timedelta(days=1))
        self.assertEqual(result["promotion"], "newer_material")
        self.assertEqual(self.current(first["version"])["id"], result["version"]["id"])

    def test_older_late_observation_and_earlier_disclosure_held(self):
        current = self.persist(sample("trade"))
        earlier = self.persist(sample("earlier_companion_disclosure"), NOW + timedelta(days=1))
        self.assertEqual(earlier["promotion"], "older")
        self.assertEqual(self.current(current["version"])["id"], current["version"]["id"])

    def test_cosmetic_and_stale_repost_do_not_refresh(self):
        current = self.persist(sample("bis_final"))
        cosmetic = sample("bis_final")
        cosmetic["headline"] += " (layout)"
        cosmetic["summary"] = cosmetic["summary"].replace(" ", "  ")
        self.assertEqual(self.persist(cosmetic, NOW + timedelta(days=1))["promotion"], "cosmetic")
        stale = self.persist(sample("stale_companion"), NOW + timedelta(days=3), make_current=False)
        self.assertEqual(stale["promotion"], "caller_disabled")
        self.assertEqual(self.current(current["version"])["id"], current["version"]["id"])
        with transaction(self.engine) as session:
            self.assertEqual(EventRepository(session).history(stale["version"]["id"]), [])  # Unscored skip.

    def test_equal_time_or_changed_identity_facts_held_ambiguous(self):
        current = self.persist(sample("bis_final"))
        same_time = sample("bis_final")
        same_time["summary"] += " Unordered change."
        self.assertEqual(self.persist(same_time)["promotion"], "ambiguous")
        changed = deepcopy(same_time)
        changed["legal_status"] = "withdrawn"
        changed["published_at"] = (datetime.fromisoformat(changed["published_at"]) + timedelta(hours=1)).isoformat()
        self.assertEqual(self.persist(changed)["promotion"], "ambiguous")
        self.assertEqual(self.current(current["version"])["id"], current["version"]["id"])

    def test_alias_policy_mapping_durable_across_redis_expiry(self):
        redis = MemoryRedis()
        outputs, submitted = corpus.run_collector([corpus.DOCS["bis_final"], corpus.DOCS["fr_companion"]], redis)
        for event, kwargs in submitted:
            self.persist(event, **kwargs)
        mapped = submitted[-1][0]
        before = {t.name: self.count(t) for t in (events, event_versions, event_provenance, event_history)}
        aliases = [k for k in redis.data if k.startswith(identity.PREFIX + ("alias:"))]
        self.assertTrue(aliases and any(k.startswith(identity.PREFIX + "policy:") for k in redis.data))
        corpus.expire(redis, "expire_all")  # Redis TTL expiry of alias, policy and processing state.
        self.assertEqual(redis.data, {})
        after = {t.name: self.count(t) for t in (events, event_versions, event_provenance, event_history)}
        self.assertEqual(after, before)
        self.assertEqual(self.reconcile(mapped)["mismatches"], [])  # Historical links intact.
        _, again = corpus.run_collector([corpus.DOCS["bis_final"]], redis)
        self.assertEqual(again[0][0]["event_id"], mapped["event_id"])  # Collector still resolves it.
        self.assertEqual(len(again[0][0]["provenance"]), 1)  # Redis lost the companion...
        result = self.persist(again[0][0])
        self.assertTrue(result["duplicate"])
        self.assertEqual(self.count(events), 1)  # ...no duplicate durable logical event...
        with transaction(self.engine) as session:
            documents = {p["document_id"] for p in EventRepository(session).provenance(result["version"]["id"])}
        self.assertEqual(documents, {"bis:synthetic-chip-rule", "fr:2026-99901"})  # ...and history keeps it.
        self.assertFalse(any(k.startswith(identity.PREFIX + "policy:") and "fr:2026-99901" in v[0]
                             for k, v in redis.data.items()))  # No reverse repair into Redis.

    def test_post_expiry_divergent_runtime_root_is_not_merged(self):
        """Documented limit: after expiry the collector may choose another root; persistence records, never merges."""
        redis = MemoryRedis()
        bis = dict(corpus.DOCS["bis_final"])
        fr_with_eo = dict(corpus.DOCS["fr_companion"], identity_anchors=["eo:99980"])
        _, first = corpus.run_collector([bis, fr_with_eo], redis)
        self.assertEqual(first[0][0]["event_id"], first[1][0]["event_id"])
        for event, kwargs in first:
            self.persist(event, **kwargs)
        corpus.expire(redis, "expire_all")
        _, later = corpus.run_collector([fr_with_eo], redis)
        self.assertNotEqual(later[0][0]["event_id"], first[0][0]["event_id"])  # Runtime behavior unchanged.
        self.persist(later[0][0])
        self.assertEqual(self.count(events), 2)
        self.assertEqual(self.reconcile(first[1][0])["mismatches"], [])
        with transaction(self.engine) as session:
            anchors = [v["attributes"]["identity_anchors"] for v in
                       (EventRepository(session).current(r["id"]) for r in session.execute(sa.select(events)).mappings())]
        self.assertTrue(all("fr:2026-99901" in a for a in anchors))  # Shared anchor remains auditable.

    def test_persistence_never_touches_redis(self):
        redis = MemoryRedis()
        _, submitted = corpus.run_collector([corpus.DOCS["bis_final"]], redis)
        snapshot = deepcopy(redis.data)
        with patch("analyzer.deduplicator.redis_client", side_effect=AssertionError("Redis")):
            self.persist(submitted[0][0])
            self.reconcile(submitted[0][0])
        self.assertEqual(redis.data, snapshot)

    def test_reconciliation_success_and_mismatches(self):
        event = sample("fr_companion")
        self.assertFalse(self.reconcile(event)["event_found"])
        version = self.persist(event)["version"]
        self.assertEqual(self.reconcile(event)["mismatches"], [])
        tampered = deepcopy(event)
        tampered["related_symbols"] = ["META"]
        result = self.reconcile(tampered)
        self.assertIn("version_found", result["mismatches"])
        with transaction(self.engine) as session:  # Deliberate fixture corruption; audit only reads.
            session.execute(event_provenance.delete().where(event_provenance.c.document_id == "fr:2026-99901"))
            session.execute(event_history.delete().where(event_history.c.event_version_id == version["id"],
                                                         event_history.c.kind == "ai"))
        with patch.dict(reconciliation._last_warning, clear=True), \
             self.assertLogs("macro_reconciliation", level="WARNING") as logs:
            result = self.reconcile(event)
            self.reconcile(event)
        for key in ("relationship_match", "provenance_match", "ai_match"):
            self.assertIn(key, result["mismatches"])
        self.assertTrue(result["relevance_match"] and result["score_match"])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("Geopolitical shadow reconciliation mismatch", logs.output[0])

    def test_relevance_mismatch_detected_on_stored_version(self):
        event = sample("platform_remedy")
        version = self.persist(event)["version"]
        attrs = dict(version["attributes"], direct_symbols=[], evidence=[])
        with transaction(self.engine) as session:  # Fixture corruption of the stored version.
            session.execute(event_versions.update().where(event_versions.c.id == version["id"]).values(attributes=attrs))
        result = self.reconcile(event)
        self.assertIn("relevance_match", result["mismatches"])
        self.assertIn("version_match", result["mismatches"])

    def test_historical_current_expectation_and_read_only(self):
        self.persist(sample("trade"))
        self.persist(sample("earlier_companion_disclosure"))
        self.assertIn("current_version_match", self.reconcile(sample("earlier_companion_disclosure"))["mismatches"])
        self.assertEqual(self.reconcile(sample("earlier_companion_disclosure"), expect_current=False)["mismatches"], [])
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            self.reconcile(sample("trade"))
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        self.assertTrue(statements)
        self.assertTrue(all(s.lstrip().upper().startswith(("SELECT", "BEGIN")) for s in statements), statements)
