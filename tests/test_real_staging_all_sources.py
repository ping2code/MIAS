"""Phase 2S all-source runner: safety, cleanup, classification, growth attribution, reporting and live mini-runs.

The live class (opt-in: TEST_DATABASE_URL + MIAS_PHASE2J_REDIS_URL) runs real
Fed, SEC, News and geopolitical worker OS processes against one per-test
``mias_test_phase2s_t_*`` database and the disposable loopback Redis. It checks
restart, Redis loss, cross-family look-alikes, DB-unavailable classification,
the all-family audit and the credential scan. Synthetic fixtures only; no
network, Telegram or OpenAI.
"""
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import sqlalchemy as sa
from alembic import command

from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine
from tests import real_staging as staging
from tests import real_staging_all_sources as runner
from tests import real_staging_news as news_staging
from tests import real_staging_sec as sec_staging
from tests.test_persistence import migration_config
from tests.test_real_staging import closed_port, inspect

ROOT = staging.ROOT
KINDS = ("events", "versions", "provenance", "score_history", "decision_history", "ai_history")


def zero_counts():
    return {f: dict.fromkeys(KINDS, 0) for f in runner.FAMILIES}


def stats(**overrides):
    base = dict(queued=0, persisted=0, duplicate=0, failed=0, dropped_queue_full=0, queue_depth=0, in_flight=0)
    base.update(overrides)
    return base


class FakeWorker:
    def __init__(self, label, results, finish):
        self.label, self.results, self._finish = label, results, finish

    def send(self, op, **kwargs):
        return self.results[0]

    def finish(self, hard=False):
        return self._finish


def fake_run(tmp):
    run = runner.Run(tmp, ("contact@example.test",), cycles=1, cycle_seconds=10, deadline=None, faults=False)
    run.url = "postgresql://u@127.0.0.1:1/mias_test_phase2s_all"
    return run


def finish_result(mismatches, **shadow):
    return dict(reconciliation=dict(status="ok", checked=5, current_pointer_differs=0, integrity_mismatches=mismatches),
                shadow_stats=stats(**shadow), shadow_timestamps={}, telegram_calls=0, openai_calls=0,
                submissions=5, warnings=[], exit_mode="graceful", exit_code=0, http={})


class RunnerSafetyAndCleanupTests(unittest.TestCase):
    def test_only_phase2s_labelled_loopback_services_are_accepted(self):
        with patch.object(staging, "docker", return_value=inspect(label="phase2s")):
            staging.verify_container("x", 55432, volume=True, label="phase2s")
        for bad in (inspect(label="phase2r"), inspect(label="phase2s", ip="0.0.0.0"), inspect(label="phase2s", mounts=[])):
            with self.subTest(bad=bad), patch.object(staging, "docker", return_value=bad), \
                 self.assertRaises(staging.StagingStop):
                staging.verify_container("x", 55432, volume=True, label="phase2s")
        self.assertTrue(runner.database_url("mias_test_phase2s_all").endswith("/mias_test_phase2s_all"))
        for name in ("mias_test_phase2r_news", "mias", "mias_test_other"):
            with self.subTest(name=name), self.assertRaises(Exception):
                runner.database_url(name)

    def test_existing_containers_are_refused_and_never_removed(self):
        with patch.object(staging, "docker", return_value="mias-redis\nmias-test-phase2s-postgres\n") as docker, \
             self.assertRaises(staging.StagingStop):
            runner.ensure_absent()
        self.assertEqual([c.args[0] for c in docker.call_args_list], ["ps"])
        with patch.object(runner, "ensure_absent", side_effect=staging.StagingStop("exists")), \
             patch.object(runner, "remove_containers") as remove, patch.object(runner.sec_staging, "load_contact",
                                                                                 return_value=("x@y",)), \
             patch("sys.stderr"), tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(runner.main(["--report", f"{tmp}/r.md"]), 1)
        remove.assert_not_called()

    def test_created_containers_are_removed_on_failure(self):
        with patch.object(runner, "ensure_absent"), patch.object(runner, "start_containers"), \
             patch.object(runner.Run, "run", side_effect=staging.StagingStop("injected")), \
             patch.object(runner, "remove_containers", return_value=[]) as remove, \
             patch.object(runner.sec_staging, "load_contact", return_value=("x@y",)), patch("sys.stderr") as err, \
             tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(runner.main(["--report", f"{tmp}/r.md"]), 1)
            self.assertFalse(os.path.exists(f"{tmp}/r.md"))
        remove.assert_called_once()

    def test_remove_containers_only_touches_labelled_phase2s_containers(self):
        calls = []
        def fake(cmd, **kwargs):
            calls.append(cmd)
            if cmd[1] == "inspect":
                return SimpleNamespace(returncode=0, stdout="phase2s\n" if cmd[-1] == runner.PG_CONTAINER else "other\n")
            return SimpleNamespace(returncode=0, stdout="")
        with patch.object(runner.subprocess, "run", side_effect=fake):
            self.assertEqual(runner.remove_containers(), [runner.PG_CONTAINER])
        self.assertEqual([c for c in calls if c[1] == "rm"], [["docker", "rm", "-f", "-v", runner.PG_CONTAINER]])

    def test_cli_bounds(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            runner.main(["--report", "r.md", "--cycle-seconds", "1"])


class ClassificationAndReportingTests(unittest.TestCase):
    def test_credential_scan_includes_sec_contact_canaries_and_patterns(self):
        contact = ("MIAS test agent person@example.test", "person@example.test")
        self.assertEqual(runner.credential_hits("healthy counts only", contact), {})
        self.assertIn("sec_contact", runner.credential_hits("ua person@example.test", contact))
        self.assertIn("canaries", runner.credential_hits(news_staging.CANARIES["TELEGRAM_BOT_TOKEN"], contact))
        self.assertIn("openai_key", runner.credential_hits("sk-" + "x" * 30, contact))

    def test_outage_and_not_found_classification(self):
        lost = ["event_found", "version_found", "version_match", "provenance_match", "score_match"]
        self.assertTrue(runner.not_found(lost + ["accession_match", "relationship_match"]))
        self.assertFalse(runner.not_found(["score_match"]))
        self.assertTrue(runner.outage_lost("fixture: minutes (DB stopped)", lost))
        self.assertFalse(runner.outage_lost("live", lost))

    def test_queue_full_drops_explain_only_up_to_the_counted_number(self):
        lost = ["event_found", "version_found"]
        with tempfile.TemporaryDirectory() as tmp:
            run = fake_run(tmp)
            for dropped, expect_stop in ((2, False), (1, True)):
                with self.subTest(dropped=dropped):
                    label = f"W{dropped}"
                    run.evidence["processes"].append(dict(label=label, family="treasury"))
                    worker = FakeWorker(label, [dict(resources=dict(rss_kb=1, os_threads=1))],
                                        finish_result([["live", lost], ["live", lost]], dropped_queue_full=dropped))
                    if expect_stop:
                        with self.assertRaises(staging.StagingStop):
                            run.finish(worker)
                    else:
                        run.finish(worker)
                        entry = run.evidence["processes"][-1]["reconciliation"]
                        self.assertEqual((len(entry["explained_queue_full_drop"]), entry["integrity_mismatches"]), (2, []))
            other = FakeWorker("W3", [dict(resources={})], finish_result([["live", ["score_match"]]], dropped_queue_full=5))
            run.evidence["processes"].append(dict(label="W3", family="fed"))
            with self.assertRaises(staging.StagingStop):  # A contradicting mismatch is never explained by drops.
                run.finish(other)

    def test_growth_attribution_uses_durably_stored_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = fake_run(tmp)
            before, after = zero_counts(), zero_counts()
            after["sec"]["events"] = 2
            results = [dict(events=[dict(fingerprint=k) for k in ("a", "b", "c")])]
            def make_worker(family, label, **kwargs):
                run.evidence["processes"].append(dict(label=label, family=family))
                return FakeWorker(label, results, None)
            def finish(worker, hard=False):
                entry = run.evidence["processes"][-1]
                entry.update(pid=1, exit_mode="graceful", submissions=3, shadow_stats=stats(dropped_queue_full=1),
                             reconciliation=dict(status="ok"))
            with patch.object(run, "worker", side_effect=make_worker), patch.object(run, "finish", side_effect=finish), \
                 patch.object(runner, "counts", side_effect=[before, after]), \
                 patch.object(runner, "existing_keys", return_value={"a", "b"}):
                run.one("sec", "S1", extra=[("live", {})])
            row = run.evidence["restart_matrix"][-1]
            self.assertEqual((row["growth_explained"], row["newly_observed_identities"], row["identities_not_written"]),
                             (True, 2, 1))
            self.assertEqual(run.durable_keys["sec"], {"a", "b"})
            with patch.object(run, "worker", side_effect=make_worker), patch.object(run, "finish", side_effect=finish), \
                 patch.object(runner, "counts", side_effect=[after, dict(after, sec=dict(after["sec"], events=4))]), \
                 patch.object(runner, "existing_keys", return_value={"a", "b", "c"}):
                run.one("sec", "S2", extra=[("live", {})])  # Two events added but only one new identity stored.
            self.assertEqual(run.unexplained_growth[-1]["process"], "S2")

    def test_growth_resource_sanity_and_http_views(self):
        before, after = zero_counts(), zero_counts()
        after["news"].update(events=3, score_history=5)
        self.assertEqual(runner.growth(before, after)["news"], dict(events=3, versions=0, provenance=0, score_history=5,
                                                                    decision_history=0, ai_history=0))
        self.assertEqual(runner.sanity({"a": dict(end_rss_kb=100, max_threads=4),
                                        "b": dict(end_rss_kb=runner.RSS_LIMIT_KB + 1, max_threads=4),
                                        "c": dict(end_rss_kb=1, max_threads=runner.THREAD_LIMIT + 1)}), ["b", "c"])
        view = runner._http_view({"www.bis.gov/news-updates/press-release-123": dict(requests=2, statuses={"200": 2},
                                                                                   content_types=["text/html"]),
                                  "www.bis.gov/news-updates/press-release-456": dict(requests=1, statuses={"404": 1},
                                                                                   content_types=["text/html"])})
        self.assertEqual(view, {"www.bis.gov/news-updates/*": dict(requests=3, statuses={"200": 2, "404": 1},
                                                                   content_types=["text/html"])})

    def test_per_family_views_exclude_persistence_details(self):
        self.assertEqual(runner.submitted_keys("geopolitical", dict(resolved=["a", None, "a"])), {"a"})
        self.assertEqual(runner.submitted_keys("news", dict(submitted_keys=[dict(identity_key="k")])), {"k"})
        self.assertEqual(runner.submitted_keys("macro", dict(keys=["m"])), {"m"})
        on = dict(stats={"processed": 1}, processed_events=1, submitted=4, submitted_historical=1)
        self.assertEqual(runner.outcome_view("treasury", on), runner.outcome_view("treasury", dict(on, submitted=0)))
        news = dict({k: 0 for k in news_staging.PARITY_FIELDS}, feeds=[], fetched=None, shadow_logs=["x"], submitted=9)
        self.assertEqual(runner.outcome_view("news", news), runner.outcome_view("news", dict(news, shadow_logs=[], submitted=0)))

    def test_healthy_queue_full_drops_and_wrong_capacities_fail_readiness(self):
        healthy = acceptance_evidence()
        self.assertEqual(runner.acceptance_problems(healthy), [])
        prefix = acceptance_evidence(queue_full_drops={"C1-treasury": 50})  # The pre-2S-A evidence no longer passes.
        self.assertEqual(len(runner.acceptance_problems(prefix)), 1)
        self.assertIn("queue-full drops in healthy cycles", runner.acceptance_problems(prefix)[0])
        wrong = acceptance_evidence(capacities=dict(treasury=[64], fed=[64]))
        self.assertIn("unexpected writer capacities", runner.acceptance_problems(wrong)[0])
        other = acceptance_evidence(capacities=dict(treasury=[256], news=[256]))  # Another family must keep 64.
        self.assertIn("news", runner.acceptance_problems(other)[0])

    def test_report_labels_bounded_validation_and_lists_decisions(self):
        text = runner.render(minimal_evidence())
        self.assertIn("bounded Phase 2S validation", text)
        self.assertIn("has **not** been performed", text)
        rows = [line for line in text.splitlines() if line.startswith("| ") and line.split("|")[1].strip().isdigit()]
        self.assertEqual(len(rows), len(runner.DECISIONS))
        self.assertGreaterEqual(len(runner.DECISIONS), 11)
        self.assertEqual(runner.credential_hits(text, ("x@y",)), {})


def acceptance_evidence(queue_full_drops=None, capacities=None):
    return dict(unexplained_growth=[], writer_summary=dict(unexplained_failures=[], queue_full_drops=queue_full_drops or {}),
                writer_capacity=dict(observed_by_family=capacities or dict(treasury=[256], fed=[64], macro=[64], sec=[64],
                                                                              news=[64], geopolitical=[64])),
                reconciliation=dict(unexplained=0), resource_flags=[], guards=dict(telegram_transport_calls=0, openai_calls=0),
                credential_scan=dict(artifacts={"evidence": dict(hits=0)}))


def minimal_evidence():
    fam = {f: dict(identical=True, redis_identical=True) for f in runner.FAMILIES}
    counts = {f: dict.fromkeys(KINDS, 0) for f in runner.FAMILIES}
    return dict(started_utc="t", postgresql_version="16", redis_version="7", migration_head=runner.HEAD, acceptance="PASS",
                parity=fam, soak=dict(cycles_completed=3, label="bounded Phase 2S validation"), unexplained_growth=[],
                restart_matrix=[], outage=dict(collector_differing={}, failures_counted={}, outage_only_items_persisted={}),
                redis_loss={f: dict(added=dict(events=0)) for f in runner.FAMILIES}, cross_family={},
                redis_namespaces=dict(staging={}, unexpected=[]), guards=dict(telegram_transport_calls=0, openai_calls=0,
                                                                                would_send_stub_total=0, ai_stub_attempts_total=0),
                audits=dict(family=dict(integrity_findings=0, pointer_rule=dict(violation_count=0, versions_evaluated=0)),
                            sec=dict(repeated_events=0), news=dict(repeated_events=0, variant_groups=0)),
                reconciliation=dict(unexplained=0, explained_outage_lost=0, explained_queue_full_drop=0),
                credential_scan=dict(artifacts={}), writer_summary=dict(queue_full_drops={}, queue_full_drops_by_family={}),
                writer_capacity=dict(observed_by_family={}),
                live=dict(sec=dict(forms=[]), news={}), history_growth_soak=counts, history_growth_total=counts)


class HermeticMacroTreasuryWorkerTests(unittest.TestCase):
    """Real separate Macro/Treasury worker processes (start, exit; no network, database or Redis)."""

    def run_worker(self, collector, op):
        holder, port = closed_port()
        self.addCleanup(holder.close)
        env = staging.base_env(db_url="postgresql://mias_test_user@127.0.0.1:1/mias_test_phase2s_all", redis_port=port,
                               **news_staging.CANARIES)
        result = subprocess.run([sys.executable, "-m", "tests.real_staging_worker", "--collector", collector], cwd=ROOT,
                                env=env, input=json.dumps(dict(op=op)) + "\n", capture_output=True, text=True, timeout=120)
        return result, [json.loads(line) for line in result.stdout.splitlines()]

    def test_start_and_exit_with_tripwires_and_resources(self):
        for collector in ("macro", "treasury"):
            for op in ("exit", "hard_exit"):
                with self.subTest(collector=collector, op=op):
                    result, [finish] = self.run_worker(collector, op)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual((finish["telegram_calls"], finish["openai_calls"]), (0, 0))
                    self.assertEqual(finish["shadow_stats"]["worker_started"], 0)  # Persistence switch off.
                    self.assertGreater(finish["resources"]["rss_kb"], 0)
                    self.assertEqual(runner.credential_hits(result.stdout + result.stderr, ("x@y",)), {})

    def test_macro_and_treasury_workers_never_read_dotenv(self):
        for name in ("MacroProcess", "TreasuryProcess"):
            code = ("import dotenv\ncalls=[]\ndotenv.load_dotenv=lambda *a,**k: calls.append(1)\n"
                    f"import tests.real_staging_worker as w\nw.{name}(w.Guard('t'), w.Guard('o'), [])\nprint(len(calls))")
            env = staging.base_env(db_url="postgresql://u@127.0.0.1:1/mias_test_phase2s_all", redis_port=1)
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.stdout.strip(), "0", f"{name} called load_dotenv")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL") and os.environ.get("MIAS_PHASE2J_REDIS_URL"),
                     "Disposable TEST_DATABASE_URL and loopback Redis opt-in required")
class LiveCombinedMiniRunTests(unittest.TestCase):
    """Real Fed, SEC, News and geopolitical worker OS processes sharing one database."""

    def setUp(self):
        import redis
        from tests import staging_rollout
        redis_url = os.environ["MIAS_PHASE2J_REDIS_URL"]
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
        self.addCleanup(self.redis.close)
        staging_rollout.verify_redis(self.redis, redis_url)
        self.addCleanup(self.clear)
        self.redis_port = urlsplit(redis_url).port
        admin = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.admin = make_engine(admin)
        self.addCleanup(self.admin.dispose)
        name = "mias_test_phase2s_t_" + uuid4().hex[:16]
        with self.admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        self.addCleanup(self.drop, name)
        self.url = admin.url.set(database=name).render_as_string(hide_password=False)
        engine = make_engine(DatabaseSettings(url=self.url))
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        engine.dispose()
        self.logdir = self.enterContext(tempfile.TemporaryDirectory(prefix="mias-phase2s-test-"))
        self.contact = sec_staging.load_contact()
        self.workers = []

    def clear(self):
        for key in self.redis.scan_iter(match="mias:*"):
            self.redis.delete(key)

    def drop(self, name):
        with self.admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

    def process(self, family, operations, *, db=None):
        env = staging.base_env(db_url=db or self.url, redis_port=self.redis_port, **runner.SWITCHES[family],
                               **news_staging.CANARIES)
        w = runner.AllWorker(runner.COLLECTOR[family], env, f"{family}-{len(self.workers)}", self.logdir)
        w.contact = self.contact
        self.workers.append(w)
        self.addCleanup(lambda: w.process.poll() is None and w.process.kill())
        results = [w.send(op, **args) for op, args in operations]
        return results, w.finish()

    def counts(self):
        return {f: c["events"] for f, c in runner.counts(self.url).items()}

    def fixtures(self):
        clock = datetime.fromisoformat(runner.FED_CLOCK)
        lookalike = news_staging.item("Federal Reserve issues FOMC statement as Nvidia rallies", runner.FED_FIXTURE_URL,
                                      clock, publisher="Reuters")
        sec_like = news_staging.item("META filed SEC Form 8-K", runner.SEC_FIXTURE_URL, clock, publisher="Reuters")
        return dict(
            fed=[("controlled", dict(label="fixture: FOMC statement", docs=["policy_statement"], clock=runner.FED_CLOCK))],
            sec=[("controlled", dict(label="synthetic META 8-K", filings=sec_staging.filings("meta_8k")))],
            news=[("controlled", dict(label="synthetic look-alikes", clock=runner.FED_CLOCK,
                                      feeds=[dict(label="Google News META", entries=[lookalike, sec_like])]))],
            geopolitical=[("controlled", dict(label="fixture: BIS release", docs=["bis_final"], clock=runner.GEO_CLOCK))])

    def test_families_coexist_restart_redis_loss_and_isolation(self):
        fixtures = self.fixtures()
        for family, operations in fixtures.items():
            _, finish = self.process(family, operations)
            self.assertEqual((finish["reconciliation"]["status"], finish["reconciliation"]["integrity_mismatches"]), ("ok", []))
            self.assertEqual((finish["shadow_stats"]["failed"], finish["shadow_stats"]["dropped_queue_full"]), (0, 0))
        baseline = self.counts()
        self.assertEqual({f: n for f, n in baseline.items() if n}, dict(fed=1, sec=1, news=2, geopolitical=1))
        for family, operations in fixtures.items():  # Fresh processes, Redis intact.
            self.process(family, operations)
        self.assertEqual(self.counts(), baseline)
        self.clear()  # Redis loss: every family reprocesses; durable identities hold.
        for family, operations in fixtures.items():
            _, finish = self.process(family, operations)
            self.assertEqual(finish["reconciliation"]["integrity_mismatches"], [])
        self.assertEqual(self.counts(), baseline)
        result = runner.audit(self.url)
        self.assertEqual((result["integrity_findings"], result["pointer_rule"]["violation_count"]), (0, 0))
        engine = make_engine(DatabaseSettings(url=self.url))
        try:
            with engine.connect() as connection:
                shared = connection.execute(sa.text(
                    "SELECT count(DISTINCT e.source_family), count(DISTINCT e.id) FROM events e JOIN event_versions v "
                    "ON v.event_id = e.id WHERE v.canonical_url IN (:a, :b) GROUP BY v.canonical_url"),
                    dict(a=runner.FED_FIXTURE_URL, b=runner.SEC_FIXTURE_URL)).all()
        finally:
            engine.dispose()
        self.assertEqual(sorted(shared), [(2, 2), (2, 2)])  # Same URL in two families: two separate events each.
        texts = [json.dumps([w.results for w in self.workers]), "".join(w.logfile.read_text() for w in self.workers),
                 news_staging.dump_rows(self.url)]
        self.assertEqual({k: v for t in texts for k, v in runner.credential_hits(t, self.contact).items()}, {})

    def test_database_unavailable_is_counted_and_not_replayed(self):
        holder, port = closed_port()
        self.addCleanup(holder.close)
        down = self.url.replace(urlsplit(self.url).netloc, f"{urlsplit(self.url).username}@127.0.0.1:{port}")
        outage = [("controlled", dict(label="fixture: minutes (DB stopped)", docs=["minutes"], clock=runner.FED_CLOCK))]
        results, finish = self.process("fed", outage, db=down)
        self.assertEqual((results[0]["submitted"], finish["shadow_stats"]["failed"], finish["reconciliation"]["status"]),
                         (1, 1, "database_unavailable"))
        # Recovery: a fresh process persists new work; the outage-window item is never written without re-observation.
        self.process("fed", [("controlled", dict(label="fixture: symbol mention", docs=["symbol_mention"],
                                                 clock=runner.FED_CLOCK))])
        self.assertEqual(self.counts()["fed"], 1)
        # Re-observing the item re-submits the Fed collector's cached processed result (its existing design), which
        # then persists: a new observation by the collector, not a replay by persistence.
        results, _ = self.process("fed", [("controlled", dict(label="fixture: minutes again", docs=["minutes"],
                                                              clock=runner.FED_CLOCK))])
        self.assertEqual((results[0]["stats"]["duplicates"], results[0]["submitted"], self.counts()["fed"]), (1, 1, 2))

if __name__ == "__main__":
    unittest.main()
