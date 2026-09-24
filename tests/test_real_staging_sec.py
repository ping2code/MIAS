"""Phase 2P SEC real-process harness: safety, hermetic worker processes and live disposable-service scenarios.

The live class (opt-in: TEST_DATABASE_URL + MIAS_PHASE2J_REDIS_URL) runs real SEC
worker OS processes against a per-test ``mias_test_phase2p_t_*`` database and the
disposable loopback Redis: ON/OFF parity, restart, real Redis expiry, Redis loss,
DB unavailable then recovery, audits, reconciliation and the contact scan.
Synthetic filings only; no network, Telegram or OpenAI.
"""
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

from persistence.adapters.sec import filing_document
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine
from tests import real_staging as staging
from tests import real_staging_sec as sec_staging
from tests.test_persistence import migration_config
from tests.test_real_staging import closed_port, inspect
from tests.test_sec_pipeline import sec, deduplicator

ROOT = staging.ROOT
ALERTS = ["meta_8k", "meta_10q", "meta_10k", "nvda_8k"]


class SecHarnessSafetyTests(unittest.TestCase):
    def test_only_phase2p_disposable_services_are_accepted(self):
        with patch.object(staging, "docker", return_value=inspect(label="phase2p")):
            staging.verify_container("x", 55432, volume=True, label="phase2p")
        for bad in (inspect(label="phase2m"), inspect(label="phase2p", ip="0.0.0.0"), inspect(label="phase2p", mounts=[])):
            with self.subTest(bad=bad), patch.object(staging, "docker", return_value=bad), \
                 self.assertRaises(staging.StagingStop):
                staging.verify_container("x", 55432, volume=True, label="phase2p")
        with patch.object(staging, "docker", return_value=inspect()):  # Phase 2M default is unchanged.
            staging.verify_container("x", 55432, volume=True)

    def test_database_targets_are_phase2p_test_only(self):
        self.assertTrue(sec_staging.database_url("mias_test_phase2p_sec").endswith("/mias_test_phase2p_sec"))
        for name in ("mias_test_phase2m_staging", "mias", "production", "mias_test_other"):
            with self.subTest(name=name), self.assertRaises(Exception):
                sec_staging.database_url(name)
        self.assertTrue(staging.database_url("mias_test_phase2m_staging").endswith("/mias_test_phase2m_staging"))

    def test_contact_scan_detects_without_exposing(self):
        contact = sec_staging.load_contact()
        self.assertTrue(any("@" in token for token in contact), "configured contact token not found")
        self.assertEqual(sec_staging.contact_hits("healthy report", contact), 0)
        for token in contact:  # Full User-Agent and the contact inside it are each detected.
            self.assertGreaterEqual(sec_staging.contact_hits("x " + token + " y", contact), 1, "contact not detected")
        self.assertEqual(sec_staging.contact_hits("a@b c@d", ("a@b",)), 1)

    def test_synthetic_filings_cannot_match_real_filings(self):
        accessions = [f["accession_number"] for f in sec_staging.SYNTHETIC.values()]
        self.assertTrue(all(a.startswith("0000000000-") for a in accessions))
        joint = sec_staging.SYNTHETIC["joint_form4_nvda"]["accession_number"]
        self.assertEqual([a for a in set(accessions) if accessions.count(a) > 1], [joint])  # Intended shared accession.
        self.assertTrue(any("/" in f["primary_document"] for f in sec_staging.SYNTHETIC.values()))
        with patch.object(deduplicator, "redis_client") as client, patch.object(sec, "send_telegram_alert"), \
             patch("sys.stdout"), self.assertLogs("sec_collector", "INFO"):
            client.set.return_value = True
            events = sec.process_sec_filings(sec_staging.filings(*sec_staging.SYNTHETIC))
        self.assertEqual(len(events), len(sec_staging.SYNTHETIC))
        for event in events:
            self.assertEqual(filing_document(event)[0], event["accession_number"])
            self.assertIn("mias-staging-fixture-", event["url"])

    def test_parity_comparators(self):
        base = dict(input_count=1, processed=1, events=[1], events_digest="a", stdout_digest="b", stdout_lines=0,
                    collector_logs=[["sec_collector", "INFO", "x"]], would_send=0, would_send_digests=[],
                    shadow_logs=[["sec_collector", "WARNING", "SEC shadow submission failed"]], fetched=None)
        self.assertEqual(sec_staging.cycle_parity(base, dict(base, shadow_logs=[], submitted=5)), [])
        self.assertEqual(sec_staging.cycle_parity(base, dict(base, events=[2], would_send=1)), ["events", "would_send"])
        self.assertEqual(sec_staging.cycle_parity(dict(base, fetched=[1]), dict(base, fetched=[2])), ["fetched"])
        on = {"mias:sec:event:" + "a" * 64: dict(value="1", ttl=86390), "mias:sec:event:" + "b" * 64: dict(value="1", ttl=86400)}
        off = {"mias:sec:event:" + "a" * 64: dict(value="1", ttl=86399)}
        self.assertFalse(sec_staging.redis_parity(on, off)["identical_keys_and_values"])
        self.assertTrue(sec_staging.redis_parity(on, off, {"a" * 64})["identical_keys_and_values"])
        self.assertFalse(sec_staging.sec_redis({"mias:sec:event:x": dict(value="1", ttl=3600)})["ttl_within_contract"])

    def test_only_outage_window_losses_are_explained(self):
        lost = ["event_found", "version_found", "version_match", "provenance_match", "accession_match",
                "score_match", "decision_match"]
        self.assertTrue(sec_staging.outage_lost("synthetic 8-K (DB stopped)", lost))
        self.assertFalse(sec_staging.outage_lost("synthetic 8-K (DB up)", lost))  # Outside the declared window.
        self.assertFalse(sec_staging.outage_lost(None, lost))
        self.assertFalse(sec_staging.outage_lost("synthetic 8-K (DB stopped)", ["score_match"]))  # Found but wrong.
        self.assertFalse(sec_staging.outage_lost("synthetic 8-K (DB stopped)", ["event_found", "relevance_match"]))

    def test_worker_environment_is_explicit_only(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "y", "DATABASE_URL": "prod"}):
            env = staging.base_env(db_url="postgresql://u@127.0.0.1:1/mias_test_phase2p_sec", redis_port=1,
                                   SEC_PERSISTENCE_SHADOW_ENABLED=True)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertEqual(env["SEC_PERSISTENCE_SHADOW_ENABLED"], "true")


class HermeticSecWorkerTests(unittest.TestCase):
    """Real separate SEC worker processes with no network, database or Redis."""

    def run_worker(self, commands, **switches):
        holder, port = closed_port()
        self.addCleanup(holder.close)
        env = staging.base_env(db_url="postgresql://mias_test_user@127.0.0.1:1/mias_test_phase2p_sec", redis_port=port,
                               **switches)
        result = subprocess.run([sys.executable, "-m", "tests.real_staging_worker", "--collector", "sec"], cwd=ROOT,
                                env=env, input="".join(json.dumps(c) + "\n" for c in commands),
                                capture_output=True, text=True, timeout=120)
        return result, [json.loads(line) for line in result.stdout.splitlines()]

    def test_controlled_cycle_graceful_and_hard_exit(self):
        contact = sec_staging.load_contact()
        for exit_op in ("exit", "hard_exit"):
            with self.subTest(exit_op=exit_op):
                result, lines = self.run_worker([
                    {"op": "controlled", "label": "synthetic", "filings": sec_staging.filings("meta_8k", "meta_npx")},
                    {"op": exit_op}])
                self.assertEqual(result.returncode, 0, result.stderr[-300:].replace(contact[0], "<contact>"))
                cycle, finish = lines  # Collector ALERT printing never reaches the protocol channel.
                # Redis unreachable: SEC dedup fails open and both filings are processed.
                self.assertEqual((cycle["processed"], cycle["would_send"], cycle["submitted"]), (2, 1, 0))
                self.assertEqual([e["decision"] for e in cycle["events"]], ["ALERT", "IGNORE"])
                self.assertEqual(cycle["stdout_lines"], len(sec.format_alert(dict(symbols=["META"])).splitlines()))
                self.assertEqual((finish["telegram_calls"], finish["openai_calls"]), (0, 0))
                self.assertEqual(finish["shadow_stats"]["worker_started"], 0)  # Disabled: no writer.
                self.assertEqual(cycle["pid"], finish["pid"])
                staging.clean(result.stdout + result.stderr)
                self.assertEqual(sec_staging.contact_hits(result.stdout + result.stderr, contact), 0)

    def test_sec_worker_never_reads_dotenv(self):
        code = ("import dotenv\ncalls=[]\ndotenv.load_dotenv=lambda *a,**k: calls.append(1)\n"
                "import tests.real_staging_worker as w\nw.SecProcess(w.Guard('t'), w.Guard('o'), [])\nprint(len(calls))")
        env = staging.base_env(db_url="postgresql://u@127.0.0.1:1/mias_test_phase2p_sec", redis_port=1)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "0", "load_dotenv was called")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL") and os.environ.get("MIAS_PHASE2J_REDIS_URL"),
                     "Disposable TEST_DATABASE_URL and loopback Redis opt-in required")
class LiveSecProcessTests(unittest.TestCase):
    """Real SEC worker OS processes against disposable PostgreSQL and Redis."""

    def setUp(self):
        import redis
        from tests import staging_rollout
        redis_url = os.environ["MIAS_PHASE2J_REDIS_URL"]
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
        self.addCleanup(self.redis.close)
        staging_rollout.verify_redis(self.redis, redis_url)  # Loopback and empty, or refuse.
        self.addCleanup(lambda: [self.redis.delete(k) for k in self.redis.scan_iter(match="mias:*")])
        self.redis_port = urlsplit(redis_url).port
        admin = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.admin = make_engine(admin)
        self.addCleanup(self.admin.dispose)
        name = "mias_test_phase2p_t_" + uuid4().hex[:16]
        with self.admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        self.addCleanup(self.drop, name)
        self.url = admin.url.set(database=name).render_as_string(hide_password=False)
        engine = make_engine(DatabaseSettings(url=self.url))
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        engine.dispose()
        self.logdir = self.enterContext(tempfile.TemporaryDirectory(prefix="mias-phase2p-test-"))
        self.workers = []

    def drop(self, name):
        with self.admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

    def worker(self, label, *, shadow=True, db=None):
        env = staging.base_env(db_url=db or self.url, redis_port=self.redis_port, SEC_PERSISTENCE_SHADOW_ENABLED=shadow)
        w = staging.Worker("sec", env, label, self.logdir)
        self.workers.append(w)
        self.addCleanup(lambda: w.process.poll() is None and w.process.kill())
        return w

    def one_pass(self, label, names, **kwargs):
        w = self.worker(label, **kwargs)
        cycle = w.send("controlled", label=label, filings=sec_staging.filings(*names))
        return cycle, w.finish()

    def counts(self):
        return staging.db_counts(self.url)

    def sec_keys(self):
        return {k: (self.redis.get(k), self.redis.ttl(k)) for k in self.redis.scan_iter(match="mias:sec:*")}

    def audit(self, name):
        env = staging.base_env(db_url=self.url, redis_port=self.redis_port)
        result = subprocess.run([sys.executable, "-m", "persistence.sec_audit", name, "--json"], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-300:])
        return json.loads(result.stdout)

    def assert_reconciled(self, finish):
        self.assertEqual((finish["reconciliation"]["status"], finish["reconciliation"]["integrity_mismatches"]), ("ok", []))

    def test_parity_restart_expiry_loss_audits_and_contact(self):
        base = sec_staging.BASE_SET
        # Persistence OFF then ON over the same Redis (reset between): collector-visible behavior identical.
        off_cycle, off_finish = self.one_pass("off", base, shadow=False)
        off_keys = self.sec_keys()
        for key in off_keys:
            self.redis.delete(key)
        on_cycle, on_finish = self.one_pass("on", base)
        self.assertEqual(sec_staging.cycle_parity(on_cycle, off_cycle), [])
        self.assertEqual({k: v for k, (v, _) in self.sec_keys().items()}, {k: v for k, (v, _) in off_keys.items()})
        self.assertTrue(all(86400 - 60 <= ttl <= 86400 for _, ttl in self.sec_keys().values()))
        self.assertEqual((on_finish["shadow_stats"]["persisted"], on_finish["shadow_stats"]["failed"]), (len(base), 0))
        self.assertEqual(on_cycle["would_send"], len(ALERTS))
        self.assert_reconciled(on_finish)
        baseline = self.counts()
        self.assertEqual(baseline["sec"], len(base))

        # Fresh OS process, Redis intact: the collector skips everything; durable state unchanged.
        cycle, finish = self.one_pass("restart", base)
        self.assertEqual((cycle["processed"], cycle["submitted"], cycle["would_send"]), (0, 0, 0))
        self.assertNotEqual(finish["pid"], on_finish["pid"])
        self.assertEqual(self.counts(), baseline)

        # Real Redis expiry of two keys: exactly those are reprocessed and recognized as durable duplicates.
        expired = [e["fingerprint"] for e in on_cycle["events"] if e["form"] in ("8-K", "N-PX") and e["symbol"] == "META"]
        sec_staging.expire_keys(self.redis, expired)
        cycle, finish = self.one_pass("after-expiry", base)
        self.assertEqual(sorted(e["fingerprint"] for e in cycle["events"]), sorted(expired))
        self.assertEqual((finish["shadow_stats"]["duplicate"], cycle["would_send"]), (2, 1))  # Repeat send unchanged.
        self.assertEqual(self.counts(), baseline)
        self.assert_reconciled(finish)

        # Redis loss (all SEC state gone): everything is reprocessed; still no new durable rows.
        for key in self.sec_keys():
            self.redis.delete(key)
        cycle, finish = self.one_pass("after-loss", base)
        self.assertEqual((cycle["processed"], finish["shadow_stats"]["duplicate"], cycle["would_send"]),
                         (len(base), len(base), len(ALERTS)))
        self.assertEqual(self.counts(), baseline)
        self.assert_reconciled(finish)

        shared = self.audit("shared-accessions")
        self.assertEqual([g["accession"] for g in shared["groups"]], [sec_staging.SYNTHETIC["joint_form4_nvda"]["accession_number"]])
        repeats = self.audit("repeats")
        self.assertEqual(repeats["repeated_events"], len(base))
        self.assertTrue(all(e["observation_count"] is None for e in repeats["events"]))

        contact = sec_staging.load_contact()
        texts = [json.dumps([w.results for w in self.workers]), "".join(w.logfile.read_text() for w in self.workers),
                 sec_staging.dump_rows(self.url), json.dumps(shared), json.dumps(repeats)]
        self.assertEqual(sum(sec_staging.contact_hits(t, contact) for t in texts), 0, "contact exposed")
        self.assertEqual(sum(r["telegram_calls"] + r["openai_calls"] for w in self.workers for r in w.results), 0)

    def test_db_unavailable_at_start_then_recovery_without_replay(self):
        holder, port = closed_port()
        self.addCleanup(holder.close)
        down = self.url.replace(urlsplit(self.url).netloc, f"{urlsplit(self.url).username}@127.0.0.1:{port}")
        off_cycle, _ = self.one_pass("control", ["outage_d1"], shadow=False)
        for key in self.sec_keys():
            self.redis.delete(key)
        cycle, finish = self.one_pass("db-down", ["outage_d1"], db=down)
        self.assertEqual(sec_staging.cycle_parity(cycle, off_cycle), [])  # Collector unaffected by the outage.
        self.assertEqual((finish["shadow_stats"]["failed"], finish["shadow_stats"]["persisted"]), (1, 0))
        self.assertEqual(finish["reconciliation"]["status"], "database_unavailable")
        # Recovery in a fresh process: the outage-only filing is not replayed (Redis still remembers it);
        # new work persists.
        cycle, finish = self.one_pass("recovered", ["outage_d1", "after_recovery_e"])
        self.assertEqual([e["accession"] for e in cycle["events"]], [sec_staging.SYNTHETIC["after_recovery_e"]["accession_number"]])
        self.assertEqual((finish["shadow_stats"]["persisted"], finish["shadow_stats"]["failed"]), (1, 0))
        self.assertFalse(staging.event_exists(self.url, off_cycle["events"][0]["fingerprint"]))
        self.assertEqual(self.counts()["sec"], 1)
        self.assert_reconciled(finish)


if __name__ == "__main__":
    unittest.main()
