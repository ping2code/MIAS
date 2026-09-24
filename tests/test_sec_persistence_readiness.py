"""SEC durable identity through the real collector: Redis TTL expiry, restart, native accession matrix, real Redis."""
from datetime import timedelta
import os
import time
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic import command

from persistence import sec_shadow as shadow
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.repository import EventRepository
from persistence.sec_shadow import persist_sec
from tests import sec_readiness_corpus as corpus
from tests.test_persistence import migration_config
from tests.test_sec_pipeline import sec, deduplicator

F = corpus.FILINGS
NOW, TTL = corpus.NOW, corpus.TTL


class SecDurableIdentityTests(unittest.TestCase):
    """Collector submissions are persisted synchronously so each assertion sees committed state."""

    def setUp(self):
        self.engine = self.make_engine()
        self.redis = corpus.SecMemoryRedis()

    def make_engine(self):
        engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        self.addCleanup(engine.dispose)
        return engine

    def run_pass(self, names, *, at=NOW, redis=None, telegram_ok=True):
        results = []
        def submit(event, **kwargs):
            results.append(persist_sec(self.engine, event, at, report=True, **kwargs))
        outputs, _ = corpus.run_collector([F[n] for n in names], redis or self.redis, now=at,
                                          submit=submit, telegram_ok=telegram_ok)
        return outputs, results

    def counts(self):
        with transaction(self.engine) as session:
            return tuple(session.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
                         for t in (events, event_versions, event_provenance, event_history))

    def test_same_filing_first_within_ttl_after_expiry_and_restart_is_one_durable_event(self):
        outputs, first = self.run_pass(["meta_8k"])
        self.assertEqual(([r["promotion"] for r in first], len(outputs["telegram"])), (["first"], 1))
        key = "mias:sec:event:" + deduplicator.create_fingerprint(outputs["events"][0])
        self.assertEqual(self.counts(), (1, 1, 1, 2))

        outputs, within = self.run_pass(["meta_8k"], at=NOW + timedelta(hours=1))
        self.assertEqual((outputs["events"], within, len(outputs["telegram"])), ([], [], 0))  # Redis dedup only.

        outputs, expired = self.run_pass(["meta_8k"], at=NOW + TTL + timedelta(minutes=1))
        self.assertEqual(len(outputs["telegram"]), 1)  # Existing collector behavior: re-alert after TTL.
        self.assertEqual([(r["duplicate"], r["promotion"]) for r in expired], [(True, "duplicate")])

        restarted = corpus.SecMemoryRedis()  # Fresh process whose Redis state was lost.
        outputs, again = self.run_pass(["meta_8k"], at=NOW + TTL + timedelta(hours=2), redis=restarted)
        self.assertEqual([(r["duplicate"], r["promotion"]) for r in again], [(True, "duplicate")])
        self.assertEqual(self.counts(), (1, 1, 1, 2))  # No second durable event, version, provenance or history.
        self.assertEqual({k for k, _ in restarted.data.items()}, {key})
        self.assertEqual({r["version"]["id"] for r in first + expired + again}, {first[0]["version"]["id"]})

    def test_native_accession_matrix(self):
        self.run_pass(["meta_8k", "meta_8k_same_day", "meta_8ka", "nvda_8k", "meta_form4", "meta_form4_same_day"])
        with transaction(self.engine) as session:
            rows = session.execute(sa.select(events.c.event_key, event_provenance.c.document_id,
                                             event_versions.c.attributes)
                                   .join(event_versions, event_versions.c.event_id == events.c.id)
                                   .join(event_provenance, event_provenance.c.event_version_id == event_versions.c.id)).all()
        by_accession = {row.document_id: row for row in rows}
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({row.event_key for row in rows}), 6)
        # Same form/company/date, different accession: separate events.
        self.assertNotEqual(by_accession["0001326801-26-000101"].event_key, by_accession["0001326801-26-000103"].event_key)
        self.assertNotEqual(by_accession["0001209191-26-054321"].event_key, by_accession["0001209191-26-054322"].event_key)
        # An amendment is its own accession and event; no relation to the original is inferred.
        amended = by_accession["0001326801-26-000102"].attributes
        self.assertEqual(amended["sec_form"], "8-K/A")
        self.assertFalse({"amends", "original_accession", "amendment"} & set(amended))
        # Different companies: separate events.
        self.assertEqual(by_accession["0001045810-26-000201"].attributes["symbols"], ["NVDA"])

    def test_collector_identity_is_authoritative_for_shared_accession(self):
        """Same accession under two watchlist issuers: the collector keeps two identities; so does persistence."""
        joint = dict(F["meta_form4"], symbol="NVDA")
        outputs, results = corpus.run_collector([F["meta_form4"], joint], self.redis,
                                                submit=lambda e, **k: persist_sec(self.engine, e, NOW, report=True))
        self.assertEqual(len(outputs["events"]), 2)  # Unchanged collector semantics: two fingerprints.
        self.assertEqual(self.counts()[0], 2)  # Never merged by accession, never deduplicated by headline.
        with transaction(self.engine) as session:
            documents = session.execute(sa.select(event_provenance.c.document_id)).scalars().all()
        self.assertEqual(documents, ["0001209191-26-054321"] * 2)  # Queryable by accession for audit.

    def test_redis_fail_open_repeats_are_idempotent_in_database(self):
        outputs, results = self.run_pass(["meta_10q", "meta_10q"], redis=corpus.FailingRedis())
        self.assertEqual(len(outputs["events"]), 2)  # Fail-open processes both, as before.
        self.assertEqual([name for name, _ in outputs["logs"]].count("dedup_error"), 2)
        self.assertEqual([r["promotion"] for r in results], ["first", "duplicate"])
        self.assertEqual(self.counts(), (1, 1, 1, 2))

    def test_telegram_failure_is_not_delivery_state(self):
        outputs, results = self.run_pass(["meta_8k_telegram_failure"], telegram_ok=False)
        self.assertIn(("error", ("Telegram SEC alert failed: %s", "telegram unavailable")), outputs["logs"])
        with transaction(self.engine) as session:
            history = EventRepository(session).history(results[0]["version"]["id"])
        self.assertEqual({h["kind"] for h in history}, {"score", "decision"})
        self.assertEqual(next(h for h in history if h["kind"] == "decision")["attributes"]["alert_decision"], "ALERT")


@unittest.skipUnless(os.environ.get("MIAS_PHASE2J_REDIS_URL"), "Disposable loopback Redis opt-in required")
class SecRealRedisTests(unittest.TestCase):
    """Existing ``sec:event`` dedup against a real, disposable, loopback Redis (never ``mias-redis``)."""

    def setUp(self):
        import redis
        from tests import staging_rollout
        url = os.environ["MIAS_PHASE2J_REDIS_URL"]
        self.client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
        self.addCleanup(self.client.close)
        staging_rollout.verify_redis(self.client, url)  # Loopback and empty, or refuse.
        self.addCleanup(lambda: [self.client.delete(k) for k in self.client.scan_iter(match="mias:*")])
        self.engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        with self.engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        self.addCleanup(self.engine.dispose)

    def run_pass(self, names, *, enabled=True, submit=None):
        results = []
        def persist(event, **kwargs):
            if submit:
                return submit(event, **kwargs)
            results.append(persist_sec(self.engine, event, NOW, report=True, **kwargs))
        with patch.object(deduplicator, "redis_client", self.client), \
             patch.object(sec, "SEC_PERSISTENCE_SHADOW_ENABLED", enabled), \
             patch.object(shadow, "submit_sec", side_effect=persist), \
             patch.object(sec, "send_telegram_alert", return_value={"result": {"message_id": 1}}) as send, \
             patch("sys.stdout"), self.assertLogs("sec_collector", "INFO"):
            processed = sec.process_sec_filings([F[n] for n in names])
        return processed, results, send.call_count

    def state(self):
        return sorted((key, self.client.get(key), self.client.ttl(key)) for key in self.client.scan_iter(match="mias:*"))

    def test_namespace_ttl_persistence_neutrality_expiry_and_fail_open(self):
        processed, results, sends = self.run_pass(["meta_8k", "meta_form4"])
        fingerprints = [deduplicator.create_fingerprint(e) for e in processed]
        state = self.state()
        self.assertEqual([k for k, _, _ in state], sorted("mias:sec:event:" + f for f in fingerprints))
        self.assertTrue(all(value == "1" and 86390 <= ttl <= 86400 for _, value, ttl in state))
        self.assertEqual((len(results), sends), (2, 1))

        # Persistence success, failure or absence never changes Redis.
        for enabled, submit in ((False, None), (True, None), (True, lambda e, **k: (_ for _ in ()).throw(RuntimeError()))):
            with self.subTest(enabled=enabled, failing=submit is not None):
                with patch.object(sec, "_shadow_last_failure", float("-inf")):
                    processed, _, sends = self.run_pass(["meta_8k", "meta_form4"], enabled=enabled, submit=submit)
                self.assertEqual((processed, sends), ([], 0))
                self.assertEqual([k for k, _, _ in self.state()], [k for k, _, _ in state])

        # Real expiry of the dedup key: the collector reprocesses; PostgreSQL-side identity stays one event.
        key = "mias:sec:event:" + fingerprints[0]
        self.client.pexpire(key, 1)
        for _ in range(200):  # Wait (bounded) for Redis itself to expire the key.
            if not self.client.exists(key):
                break
            time.sleep(0.01)
        self.assertFalse(self.client.exists(key))
        processed, results, sends = self.run_pass(["meta_8k"])
        self.assertEqual((len(processed), sends, [r["promotion"] for r in results]), (1, 1, ["duplicate"]))
        self.assertTrue(86390 <= self.client.ttl(key) <= 86400)
        with transaction(self.engine) as session:
            self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(events)).scalar_one(), 2)

        # Fail-open against an unreachable Redis is unchanged and still durable-idempotent.
        import redis
        unreachable = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
        with patch.object(self, "client", unreachable), self.assertLogs("deduplicator", "ERROR"):
            processed, results, _ = self.run_pass(["meta_8k"])
        self.assertEqual((len(processed), [r["promotion"] for r in results]), (1, ["duplicate"]))


if __name__ == "__main__":
    unittest.main()
