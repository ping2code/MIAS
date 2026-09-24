"""Phase 2R News real-process harness: safety, credential scans, hermetic workers and live disposable-service scenarios.

The live class (opt-in: TEST_DATABASE_URL + MIAS_PHASE2J_REDIS_URL) runs real News
worker OS processes against a per-test ``mias_test_phase2r_t_*`` database and the
disposable loopback Redis. It covers ON/OFF parity (including AI attempts and
would-be Telegram sends), restart, real Redis expiry, Redis loss, same-URL headline
changes, near-duplicate and link-less identity, DB outage then recovery, audits,
reconciliation after restart and the credential scan. Synthetic items only: no
network, Telegram or OpenAI.
"""
from datetime import datetime, timedelta, timezone
from itertools import combinations
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import sqlalchemy as sa
from alembic import command

from analyzer.headline_similarity import headline_similarity
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine
from tests import real_staging as staging
from tests import real_staging_news as news_staging
from tests.test_persistence import migration_config
from tests.test_real_staging import closed_port, inspect

ROOT = staging.ROOT
CLOCK = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
N = news_staging


class NewsHarnessSafetyTests(unittest.TestCase):
    def test_only_phase2r_disposable_services_are_accepted(self):
        with patch.object(staging, "docker", return_value=inspect(label="phase2r")):
            staging.verify_container("x", 55432, volume=True, label="phase2r")
        for bad in (inspect(label="phase2p"), inspect(label="phase2r", ip="0.0.0.0"), inspect(label="phase2r", mounts=[])):
            with self.subTest(bad=bad), patch.object(staging, "docker", return_value=bad), \
                 self.assertRaises(staging.StagingStop):
                staging.verify_container("x", 55432, volume=True, label="phase2r")

    def test_database_targets_are_phase2r_test_only(self):
        self.assertTrue(N.database_url("mias_test_phase2r_news").endswith("/mias_test_phase2r_news"))
        for name in ("mias_test_phase2p_sec", "mias", "production", "mias_test_other"):
            with self.subTest(name=name), self.assertRaises(Exception):
                N.database_url(name)

    def test_credential_scan_detects_patterns_and_canaries_without_echoing(self):
        self.assertEqual(N.credential_hits("Meta shares rise; a secret project and password reset story"), {})
        samples = {"openai_key": "key sk-" + "A" * 24, "telegram_token": "1234567890:" + "b" * 35,
                   "telegram_api": "https://api.telegram.org/bot123/send", "aws_key": "AKIA" + "Z" * 16,
                   "bearer": "Bearer " + "c" * 24, "url_password": "postgresql://user:pw@127.0.0.1/db",
                   "env_assignment": "TELEGRAM_BOT_TOKEN=x"}
        for name, text in samples.items():
            with self.subTest(name=name):
                self.assertIn(name, N.credential_hits(text))
        for value in N.CANARIES.values():
            self.assertIn("canaries", N.credential_hits(f"x {value} y"))
        with self.assertRaises(staging.StagingStop) as stop:
            N.scan("leak " + N.CANARIES["OPENAI_API_KEY"], "unit")
        self.assertNotIn(N.CANARIES["OPENAI_API_KEY"], str(stop.exception))
        self.assertNotIn("sk-", str(stop.exception))

    def test_synthetic_fixtures_are_labelled_and_only_intended_pairs_are_near_duplicates(self):
        entries = [e for f in N.base_feeds(CLOCK) for e in f["entries"]]
        links = [e["link"] for e in entries if "link" in e]
        self.assertTrue(all("mias-staging-fixture" in link for link in links))
        self.assertEqual(len(entries) - len(links), 2)  # Two link-less items.
        variants = [urlsplit(link) for link in links if "?" in link]
        self.assertEqual(len({(v.hostname, v.path) for v in variants}), 1)
        titles = sorted({e["title"] for e in entries} | set(N.OUTAGE_TITLES.values()))
        near = [(a, b) for a, b in combinations(titles, 2) if headline_similarity(a, b) >= 0.80]
        self.assertEqual(near, [("Meta faces staging fixture EU fine over data transfer rules",
                                 "Meta faces staging fixture EU fine over data transfers")])
        self.assertEqual(sum(e["title"] == "Meta launches staging fixture AI model for creators" for e in entries), 3)

    def test_parity_comparator_and_outage_classification(self):
        feed = dict(label="f", stats={"processed": 1}, returned=1, alert_candidates=0, http=None)
        base = dict(processed=1, events_digest="a", stdout_digest="b", stdout_lines=0, collector_logs=[], ai_attempts=1,
                    ai_attempts_digest="c", would_send=1, would_send_digests=["d"], feeds=[feed], fetched=None,
                    shadow_logs=[["collector", "WARNING", "News shadow submission failed"]], submitted=3)
        self.assertEqual(N.cycle_parity(base, dict(base, shadow_logs=[], submitted=0)), [])
        self.assertEqual(N.cycle_parity(base, dict(base, ai_attempts=2, would_send=0)), ["ai_attempts", "would_send"])
        self.assertEqual(N.cycle_parity(base, dict(base, feeds=[dict(feed, alert_candidates=1)])), ["feeds"])
        lost = ["event_found", "version_found", "version_match", "provenance_match", "score_match", "decision_match"]
        self.assertTrue(N.outage_lost("synthetic outage item B (DB stopped)", lost + ["ai_match"]))
        self.assertFalse(N.outage_lost("synthetic outage item A (DB up)", lost))
        self.assertFalse(N.outage_lost("synthetic outage item B (DB stopped)", ["score_match"]))


class HermeticNewsWorkerTests(unittest.TestCase):
    """Real separate News worker processes with no network, database or Redis (dedup fails open)."""

    def run_worker(self, commands, **switches):
        holder, port = closed_port()
        self.addCleanup(holder.close)
        env = staging.base_env(db_url="postgresql://mias_test_user@127.0.0.1:1/mias_test_phase2r_news", redis_port=port,
                               **N.CANARIES, **switches)
        result = subprocess.run([sys.executable, "-m", "tests.real_staging_worker", "--collector", "news"], cwd=ROOT,
                                env=env, input="".join(json.dumps(c) + "\n" for c in commands),
                                capture_output=True, text=True, timeout=120)
        return result, [json.loads(line) for line in result.stdout.splitlines()]

    def test_controlled_cycle_ai_and_telegram_stubs_graceful_and_hard_exit(self):
        feeds = N.base_feeds(CLOCK)[:1]
        for exit_op in ("exit", "hard_exit"):
            with self.subTest(exit_op=exit_op):
                result, lines = self.run_worker([
                    {"op": "controlled", "label": "synthetic", "feeds": feeds, "ai": N.AI, "clock": CLOCK.isoformat()},
                    {"op": exit_op}])
                self.assertEqual(result.returncode, 0, N.scan(result.stderr[-300:], "stderr"))
                cycle, finish = lines  # Collector ALERT printing never reaches the protocol channel.
                # Redis unreachable: exact and near-duplicate checks fail open, so every item is processed.
                self.assertEqual((cycle["processed"], cycle["submitted"]), (9, 0))
                self.assertEqual((cycle["ai_attempts"], cycle["would_send"]), (4, 3))  # Penalty flips one ALERT.
                self.assertEqual((finish["telegram_calls"], finish["openai_calls"]), (0, 0))
                self.assertEqual((finish["ai_attempts_total"], finish["would_send_total"]), (4, 3))
                self.assertEqual(finish["shadow_stats"]["worker_started"], 0)  # Disabled: no writer.
                self.assertEqual(N.credential_hits(result.stdout + result.stderr), {})

    def test_news_worker_never_reads_dotenv(self):
        code = ("import dotenv\ncalls=[]\ndotenv.load_dotenv=lambda *a,**k: calls.append(1)\n"
                "import tests.real_staging_worker as w\nw.NewsProcess(w.Guard('t'), w.Guard('o'), [])\nprint(len(calls))")
        env = staging.base_env(db_url="postgresql://u@127.0.0.1:1/mias_test_phase2r_news", redis_port=1)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "0", "load_dotenv was called")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL") and os.environ.get("MIAS_PHASE2J_REDIS_URL"),
                     "Disposable TEST_DATABASE_URL and loopback Redis opt-in required")
class LiveNewsProcessTests(unittest.TestCase):
    """Real News worker OS processes against disposable PostgreSQL and Redis."""

    def setUp(self):
        import redis
        from tests import staging_rollout
        redis_url = os.environ["MIAS_PHASE2J_REDIS_URL"]
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
        self.addCleanup(self.redis.close)
        staging_rollout.verify_redis(self.redis, redis_url)  # Loopback and empty, or refuse.
        self.addCleanup(self.clear)
        self.redis_port = urlsplit(redis_url).port
        admin = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.admin = make_engine(admin)
        self.addCleanup(self.admin.dispose)
        name = "mias_test_phase2r_t_" + uuid4().hex[:16]
        with self.admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        self.addCleanup(self.drop, name)
        self.url = admin.url.set(database=name).render_as_string(hide_password=False)
        engine = make_engine(DatabaseSettings(url=self.url))
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        engine.dispose()
        self.logdir = self.enterContext(tempfile.TemporaryDirectory(prefix="mias-phase2r-test-"))
        self.workers = []

    def clear(self):
        for key in self.redis.scan_iter(match="mias:*"):
            self.redis.delete(key)

    def drop(self, name):
        with self.admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

    def one_pass(self, label, feeds, *, clock=CLOCK, shadow=True, db=None):
        env = staging.base_env(db_url=db or self.url, redis_port=self.redis_port,
                               NEWS_PERSISTENCE_SHADOW_ENABLED=shadow, **N.CANARIES)
        w = N.NewsWorker("news", env, label, self.logdir)
        self.workers.append(w)
        self.addCleanup(lambda: w.process.poll() is None and w.process.kill())
        cycle = w.send("controlled", label=label, feeds=feeds, ai=N.AI, clock=clock.isoformat())
        return cycle, w.finish()

    def rows(self):
        counts = N.news_db(self.url)
        return {k: counts[k] for k in ("events", "event_versions", "event_provenance")}

    def news_keys(self):
        return {k: self.redis.get(k) for k in self.redis.scan_iter(match="mias:news:*")}

    def audit(self, name):
        env = staging.base_env(db_url=self.url, redis_port=self.redis_port)
        result = subprocess.run([sys.executable, "-m", "persistence.news_audit", name, "--json"], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-300:])
        return json.loads(result.stdout)

    def assert_reconciled(self, finish):
        self.assertEqual((finish["reconciliation"]["status"], finish["reconciliation"]["integrity_mismatches"]), ("ok", []))

    def test_parity_restart_expiry_loss_identity_audits_and_scan(self):
        feeds = N.base_feeds(CLOCK)
        # Persistence OFF then ON over the same (reset) Redis: collector-visible behavior identical.
        off, _ = self.one_pass("off", feeds, shadow=False)
        off_keys = self.news_keys()
        self.clear()
        on, on_finish = self.one_pass("on", feeds)
        self.assertEqual(N.cycle_parity(on, off), [])
        self.assertEqual(self.news_keys(), off_keys)
        self.assertTrue(all(86400 - 60 <= self.redis.ttl(k) <= 86400 for k in off_keys))
        # ALERT candidates: model launch (enriched, sent), prediction article (penalty -> DISPLAY_ONLY, not sent) and
        # the chip article (AI failure path, sent). The same-headline copies are suppressed before scoring.
        self.assertEqual((on["ai_attempts"], on["would_send"], on["submitted"]), (3, 2, 16))
        self.assertEqual(on["submitted_outcomes"].count("near_duplicate_suppressed"), 3)
        self.assertEqual((on_finish["shadow_stats"]["persisted"], on_finish["shadow_stats"]["failed"]), (16, 0))
        self.assert_reconciled(on_finish)
        baseline = self.rows()
        self.assertEqual(baseline, dict(events=16, event_versions=16, event_provenance=16))

        # Fresh OS process, Redis intact: exact duplicates skipped; durable rows unchanged.
        cycle, finish = self.one_pass("restart", feeds)
        self.assertEqual((cycle["processed"], cycle["submitted"], cycle["ai_attempts"], cycle["would_send"]), (0, 0, 0, 0))
        self.assertNotEqual(finish["pid"], on_finish["pid"])
        self.assertEqual(self.rows(), baseline)

        # Real Redis expiry (exact + headline keys) of the ALERT article and both link-less items.
        chosen = {e["fingerprint"] for f in on["feeds"] for e in f["events"]
                  if e["ai_event_type"] == "product launch" or e["identity"] == "news-fingerprint-v1"}
        self.assertEqual(len(chosen), 3)
        N.expire(self.redis, N.keys_for(on, lambda e: e["fingerprint"] in chosen))
        cycle, finish = self.one_pass("after-expiry", feeds, clock=CLOCK + timedelta(hours=25))
        self.assertEqual({e["fingerprint"] for f in cycle["feeds"] for e in f["events"]}, chosen)
        self.assertEqual(self.rows(), baseline)  # Same news-url-v1 / fallback events; no new version or provenance.
        self.assertEqual(finish["shadow_stats"]["failed"], 0)
        self.assert_reconciled(finish)  # Reconciliation after restart.

        # Redis loss: every item reprocessed and re-mapped to its existing durable event.
        self.clear()
        cycle, finish = self.one_pass("after-loss", feeds, clock=CLOCK + timedelta(hours=26))
        self.assertEqual(cycle["submitted"], 16)
        self.assertEqual(self.rows(), baseline)
        self.assert_reconciled(finish)

        # Same URL, changed headline in fresh processes: same time held, strictly later promoted.
        keys = []
        for label, title, hours in (("m1", "Meta staging fixture same-URL story first headline", 3),
                                    ("m2", "Quarterly staging fixture review reshapes Meta outlook", 3),
                                    ("m3", "Regulators question Meta staging fixture ad terms", 2)):
            cycle, _ = self.one_pass(label, N.same_url(CLOCK, title, hours))
            keys.append(cycle["submitted_keys"][0]["identity_key"])
        self.assertEqual(len(set(keys)), 1)
        view = N.event_view(self.url, keys[0])
        self.assertEqual((len({r["id"] for r in view}), len(view), [r["current"] for r in view]), (1, 3, [False, False, True]))

        # Near-duplicates and link-less items stayed separate durable events.
        submitted = {tuple(sorted(k.items())) for k in on["submitted_keys"]}
        self.assertEqual(len({dict(k)["identity_key"] for k in submitted}), 16)
        self.assertEqual(sum(dict(k)["identity"] == "news-fingerprint-v1" for k in submitted), 2)

        variants = self.audit("url-variants")
        self.assertEqual([(g["count"], g["host"]) for g in variants["groups"]], [(2, "www.example-news.test")])
        repeats = self.audit("repeats")
        self.assertEqual(repeats["repeated_events"], 17)  # 16 re-observed after loss + the same-URL story.
        self.assertTrue(all(e["observation_count"] is None for e in repeats["events"]))

        texts = [json.dumps([w.results for w in self.workers]), "".join(w.logfile.read_text() for w in self.workers),
                 N.dump_rows(self.url), json.dumps(variants), json.dumps(repeats)]
        self.assertEqual({k: v for t in texts for k, v in N.credential_hits(t).items()}, {})
        self.assertEqual(sum(r["telegram_calls"] + r["openai_calls"] for w in self.workers for r in w.results), 0)

    def test_db_unavailable_at_start_then_recovery_without_replay(self):
        holder, port = closed_port()
        self.addCleanup(holder.close)
        down = self.url.replace(urlsplit(self.url).netloc, f"{urlsplit(self.url).username}@127.0.0.1:{port}")
        control, _ = self.one_pass("control", N.outage_item("D1", CLOCK), shadow=False)
        self.clear()
        down_cycle, finish = self.one_pass("db-down", N.outage_item("D1", CLOCK), db=down)
        self.assertEqual(N.cycle_parity(down_cycle, control), [])  # Collector, AI and Telegram unaffected by the outage.
        self.assertEqual((finish["shadow_stats"]["failed"], finish["shadow_stats"]["persisted"]), (1, 0))
        self.assertEqual(finish["reconciliation"]["status"], "database_unavailable")
        feeds = [dict(label="Google News NVDA", entries=N.outage_item("D1", CLOCK)[0]["entries"]
                      + N.outage_item("E", CLOCK)[0]["entries"])]
        cycle, finish = self.one_pass("recovered", feeds)  # D1 still in Redis: not replayed; E is new work.
        self.assertEqual((cycle["processed"], finish["shadow_stats"]["persisted"], finish["shadow_stats"]["failed"]), (1, 1, 0))
        outage_key = down_cycle["feeds"][0]["events"][0]["identity_key"]
        self.assertFalse(N.event_view(self.url, outage_key))  # The outage-only write was never replayed.
        self.assertEqual(self.rows()["events"], 1)
        self.assert_reconciled(finish)


if __name__ == "__main__":
    unittest.main()
