"""News durable identity through the real collector: Redis TTL expiry, restart, URL identity cases, real Redis."""
from datetime import timedelta
import os
import time
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic import command

from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.models import events, event_versions, event_provenance, event_history
from persistence.news_shadow import persist_news
from persistence.repository import EventRepository
from tests import news_readiness_corpus as corpus
from tests.test_persistence import migration_config

E, NOW, TTL = corpus.ENTRIES, corpus.NOW, corpus.TTL
news, deduplicator = corpus.news, corpus.deduplicator


def sqlite_engine(case):
    engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
    case.addCleanup(engine.dispose)
    return engine


def counts(engine):
    with transaction(engine) as session:
        return tuple(session.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
                     for t in (events, event_versions, event_provenance, event_history))


class NewsDurableIdentityTests(unittest.TestCase):
    """Collector submissions are persisted synchronously so each assertion sees committed state."""

    def setUp(self):
        self.engine = sqlite_engine(self)
        self.redis = corpus.NewsMemoryRedis()

    def run_pass(self, names, *, at=NOW, feed="google_meta", redis=None):
        results = []
        def submit(event, **kwargs):
            results.append(persist_news(self.engine, event, at, report=True, **kwargs))
        outputs, _ = corpus.run_collector(feed, [E[n] for n in names], redis or self.redis, now=at, submit=submit)
        return outputs, results

    def test_same_article_first_within_ttl_after_expiry_and_restart_is_one_durable_event(self):
        outputs, first = self.run_pass(["meta_reuters"])
        self.assertEqual(([r["promotion"] for r in first], len(outputs["telegram"]), outputs["ai_calls"]), (["first"], 1, 1))
        keys = [k for k, _ in self.redis.live()]
        self.assertEqual(sorted(k.rsplit(":", 1)[0] for k in keys), ["mias:news:event", "mias:news:headline"])
        self.assertEqual(counts(self.engine)[:3], (1, 1, 1))

        outputs, within = self.run_pass(["meta_reuters"], at=NOW + timedelta(minutes=30))
        self.assertEqual((outputs["stats"]["duplicates"], within, outputs["ai_calls"]), (1, [], 0))  # Redis dedup only.

        outputs, expired = self.run_pass(["meta_reuters"], at=NOW + TTL + timedelta(minutes=1))
        # Existing collector semantics: reprocessed, re-analyzed and re-sent after expiry (not suppressed here).
        self.assertEqual((outputs["stats"]["processed"], outputs["ai_calls"], len(outputs["telegram"])), (1, 1, 1))
        self.assertEqual([r["promotion"] for r in expired], ["duplicate"])

        restarted = corpus.NewsMemoryRedis()  # Fresh process whose Redis state was lost.
        _, again = self.run_pass(["meta_reuters"], at=NOW + TTL + timedelta(hours=2), redis=restarted)
        self.assertEqual([r["promotion"] for r in again], ["duplicate"])
        self.assertEqual(counts(self.engine)[:3], (1, 1, 1))  # No second event, version or provenance row.
        self.assertEqual({r["version"]["id"] for r in first + expired + again}, {first[0]["version"]["id"]})
        with transaction(self.engine) as session:
            scores = [h["attributes"]["impact_score"] for h in EventRepository(session).history(first[0]["version"]["id"])
                      if h["kind"] == "score"]
        self.assertEqual(scores, [90, 80])  # Recency bonus decayed; each distinct computed score is kept once.

    def test_same_headline_different_url_separate_events(self):
        _, results = self.run_pass(["meta_reuters", "same_headline_same_publisher", "same_headline_other_publisher"])
        self.assertEqual(len({r["version"]["event_id"] for r in results}), 3)
        with transaction(self.engine) as session:
            outcomes = [[h["attributes"]["collector_outcome"] for h in EventRepository(session).history(r["version"]["id"])
                         if h["kind"] == "decision"] for r in results]
        self.assertEqual(outcomes, [["processed"], ["near_duplicate_suppressed"], ["near_duplicate_suppressed"]])

    def test_same_url_different_headline_one_event_new_versions(self):
        _, base = self.run_pass(["meta_reuters"])
        _, edit = self.run_pass(["headline_small_edit"], at=NOW + timedelta(hours=1))  # Suppressed as near-duplicate.
        _, yahoo = self.run_pass(["nvda_yahoo"], feed="yahoo")
        _, rewrite = self.run_pass(["headline_rewrite"], at=NOW + timedelta(hours=1), feed="yahoo")  # Processed.
        self.assertEqual((edit[0]["version"]["event_id"], rewrite[0]["version"]["event_id"]),
                         (base[0]["version"]["event_id"], yahoo[0]["version"]["event_id"]))
        self.assertEqual((edit[0]["promotion"], rewrite[0]["promotion"]), ("ambiguous", "ambiguous"))
        self.assertEqual(counts(self.engine)[:2], (2, 4))
        with transaction(self.engine) as session:
            repo = EventRepository(session)
            self.assertEqual(repo.current(base[0]["version"]["event_id"])["headline"], E["meta_reuters"]["title"])
            self.assertEqual(repo.current(yahoo[0]["version"]["event_id"])["headline"], E["nvda_yahoo"]["title"])

    def test_near_duplicates_are_advisory_never_identity(self):
        outputs, above = self.run_pass(["near_above_a", "near_above_b"])
        self.assertEqual((outputs["stats"]["processed"], outputs["stats"]["duplicates"]), (1, 1))
        outputs, below = self.run_pass(["near_below_a", "near_below_b"], feed="google_nvda")
        self.assertEqual((outputs["stats"]["processed"], outputs["stats"]["duplicates"]), (2, 0))
        self.assertEqual(len({r["version"]["event_id"] for r in above + below}), 4)

    def test_redis_fail_open_repeats_are_idempotent_in_database(self):
        outputs, results = self.run_pass(["low_quality", "low_quality"], feed="google_nvda", redis=corpus.FailingRedis())
        self.assertEqual(outputs["stats"]["processed"], 2)  # Fail-open processes both, as before.
        self.assertEqual([r["promotion"] for r in results], ["first", "duplicate"])
        self.assertEqual(counts(self.engine)[:3], (1, 1, 1))


@unittest.skipUnless(os.environ.get("MIAS_PHASE2J_REDIS_URL"), "Disposable loopback Redis opt-in required")
class NewsRealRedisTests(unittest.TestCase):
    """Existing exact and near-duplicate dedup against a real, disposable, loopback Redis (never ``mias-redis``)."""

    def setUp(self):
        import redis
        from tests import staging_rollout
        url = os.environ["MIAS_PHASE2J_REDIS_URL"]
        self.client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
        self.addCleanup(self.client.close)
        staging_rollout.verify_redis(self.client, url)  # Loopback and empty, or refuse.
        self.addCleanup(self.clear)
        self.engine = sqlite_engine(self)

    def clear(self):
        for key in self.client.scan_iter(match="mias:*"):
            self.client.delete(key)

    def run_pass(self, names, *, enabled=True, submit=None, client=None):
        results = []
        def persist(event, **kwargs):
            if submit:
                return submit(event, **kwargs)
            results.append(persist_news(self.engine, event, NOW, report=True, **kwargs))
        outputs, _ = corpus.run_collector("google_meta", [E[n] for n in names], client or _RealRedis(self.client),
                                          enabled=enabled, submit=persist)
        return outputs, results

    def state(self):
        return sorted((k, self.client.get(k)) for k in self.client.scan_iter(match="mias:*"))

    def test_exact_and_near_duplicate_semantics_expiry_and_neutrality(self):
        names = ["meta_reuters", "near_above_a", "near_above_b", "same_headline_same_publisher"]
        off, _ = self.run_pass(names, enabled=False)
        off_state = self.state()
        self.clear()
        on, results = self.run_pass(names)
        self.assertEqual({k: v for k, v in on.items() if k != "redis"}, {k: v for k, v in off.items() if k != "redis"})
        self.assertEqual(self.state(), off_state)  # Same keys and values with persistence on.
        self.assertEqual(sorted({k.rsplit(":", 1)[0] for k, _ in off_state}), ["mias:news:event", "mias:news:headline"])
        self.assertTrue(all(86390 <= self.client.ttl(k) <= 86400 for k, _ in off_state))
        self.assertEqual((on["stats"]["processed"], on["stats"]["duplicates"]), (2, 2))  # Real near-duplicate scan.
        self.assertEqual([r["promotion"] for r in results], ["first"] * 4)  # Four URLs, four durable articles.

        # Persistence failure leaves Redis identical.
        before = self.state()
        with patch.object(news, "_shadow_last_failure", float("-inf")):
            self.run_pass(names, submit=lambda e, **k: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(self.state(), before)

        # Real expiry of the article's exact and headline keys: reprocessed, durable duplicate.
        fingerprint = deduplicator.create_fingerprint(dict(headline=E["meta_reuters"]["title"], url=E["meta_reuters"]["link"]))
        headline_keys = [k for k, v in before if k.startswith("mias:news:headline:") and v.startswith("meta launches")]
        for key in [f"mias:news:event:{fingerprint}", *headline_keys]:
            self.assertTrue(self.client.pexpire(key, 50))
        for _ in range(200):
            if not self.client.exists(f"mias:news:event:{fingerprint}", *headline_keys):
                break
            time.sleep(0.01)
        outputs, again = self.run_pass(["meta_reuters"])
        self.assertEqual((outputs["stats"]["processed"], [r["promotion"] for r in again]), (1, ["duplicate"]))
        with transaction(self.engine) as session:
            self.assertEqual(session.execute(sa.select(sa.func.count()).select_from(events)).scalar_one(), 4)

        # Fail-open against an unreachable Redis is unchanged and still durable-idempotent.
        import redis
        unreachable = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
        outputs, again = self.run_pass(["meta_reuters"], client=_RealRedis(unreachable))
        self.assertEqual((outputs["stats"]["processed"], [r["promotion"] for r in again]), (1, ["duplicate"]))
        self.assertEqual(sum(1 for name, _ in outputs["logs"] if name == "dedup_error"), 2)


class _RealRedis:
    """Pass-through to a real client; ``now`` and ``live`` exist only for the corpus runner's interface."""

    def __init__(self, client):
        self.client, self.now = client, None

    def __getattr__(self, name):
        return getattr(self.client, name)

    def live(self):
        return None


if __name__ == "__main__":
    unittest.main()
