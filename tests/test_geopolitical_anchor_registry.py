"""Phase 2H durable geopolitical anchor registry: registry, backfill, Redis-first lookup, acceptance."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import runpy
import subprocess
import sys
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from persistence import geopolitical_durable_identity as durable_identity
from persistence.database import PersistenceError, transaction
from persistence.geopolitical_audit import audit_geopolitical_identity_divergence
from persistence.geopolitical_registry import (
    AnchorRegistryRepository, backfill_geopolitical_anchor_registry, normalize_anchor,
)
from persistence.geopolitical_shadow import persist_geopolitical
from persistence.models import (
    events, event_versions, event_provenance, event_history, geopolitical_anchor_registry as registry,
)
from persistence.repository import EventRepository
from tests import geopolitical_readiness_corpus as corpus
from tests import test_geopolitical_persistence_adapter as adapter_tests
from tests.test_geopolitical_persistence_adapter import sample
from tests.test_geopolitical_pipeline import MemoryRedis, geo, identity

OBSERVED = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
STAGE = ("policy_action", "adopted", "original")
ROOT_A, ROOT_B = "a" * 64, "b" * 64
HISTORY = (events, event_versions, event_provenance, event_history)


class InlineLookup:
    """Same contract as DurableIdentityLookup, executed on the calling thread (SQLite-safe)."""

    def __init__(self, engine, fail=None):
        self.engine, self.fail, self.calls, self.bypass = engine, fail, [], 0

    def redis_hit(self):
        self.bypass += 1

    def lookup(self, anchors, stage):
        self.calls.append((list(anchors), tuple(stage)))
        if self.fail:
            raise self.fail
        with transaction(self.engine) as session:
            result = AnchorRegistryRepository(session).lookup(anchors=anchors, stage=stage)
        return result["policy_id"] if result["status"] == "hit" else None


class RecordingRedis(MemoryRedis):
    def __init__(self):
        super().__init__()
        self.calls, self.inside = [], False

    def get(self, key):
        if not self.inside:  # The double implements the Lua script via get(); count callers only.
            self.calls.append(("get", key))
        return super().get(key)

    def eval(self, script, n, *args):
        self.calls.append(("eval", args[:n]))
        self.inside = True
        try:
            return super().eval(script, n, *args)
        finally:
            self.inside = False


def collect(docs, redis, lookup=None):
    """Real collector pass; lookup=None means the switch is off (and must not be touched)."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(geo, "GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED", lookup is not None))
        stack.enter_context(patch.object(durable_identity, "get_durable_lookup",
                                         side_effect=AssertionError("durable lookup forbidden") if lookup is None
                                         else lambda timeout_ms: lookup))
        return corpus.run_collector(docs, redis)


FR_WITH_EO = dict(corpus.DOCS["fr_companion"], identity_anchors=["eo:99980"])


class AnchorRegistryTests(unittest.TestCase):
    setUp = adapter_tests.GeopoliticalAdapterTests.setUp
    count = adapter_tests.GeopoliticalAdapterTests.count

    def repo_call(self, method, **kwargs):
        with transaction(self.engine) as session:
            return getattr(AnchorRegistryRepository(session), method)(**kwargs)

    def register(self, anchors, root=ROOT_A, stage=STAGE, observed=OBSERVED):
        return self.repo_call("register", anchors=anchors, stage=stage, policy_id=root,
                              event_key="event-" + root[:4], observed_at=observed)

    def dump(self, tables=HISTORY):
        with transaction(self.engine) as session:
            return {t.name: sorted(json.dumps(dict(r), sort_keys=True, default=str)
                                   for r in session.execute(sa.select(t)).mappings()) for t in tables}

    def audit(self):
        with transaction(self.engine) as session:
            return audit_geopolitical_identity_divergence(EventRepository(session))

    def persist(self, submitted):
        for event, kwargs in submitted:
            persist_geopolitical(self.engine, event, OBSERVED, **kwargs)

    # ---------------------------------------------------------------- registry

    def test_authoritative_anchor_normalization(self):
        for raw, expected in (("fr:2026-99901", ("fr", "2026-99901")), (" FR:2026-99901 ", ("fr", "2026-99901")),
                              ("eo:14100", ("eo", "14100")), ("ofac:20260922_1", ("ofac", "20260922_1")),
                              ("ftc-case:2026001", ("ftc-case", "2026001")), ("moea:99903", ("moea", "99903"))):
            self.assertEqual(normalize_anchor(raw), expected, raw)
        for raw in ("Commerce adopts export controls", "https://www.bis.gov/press-release/x", "fr:abc",
                    "rin:0694-AJ00", "docket:BIS-2026-1", "bis:synthetic-chip-rule", "eo:", None, 7, "x" * 400):
            self.assertIsNone(normalize_anchor(raw), raw)

    def test_insert_duplicate_last_seen_hit_and_miss(self):
        self.assertEqual(self.register(["fr:2026-99901", "headline text"])["inserted"], 1)
        later = self.register(["fr:2026-99901"], observed=OBSERVED + timedelta(days=1))
        self.assertEqual((later["inserted"], later["already_present"], later["conflicts"]), (0, 1, 0))
        self.assertEqual(self.count(registry), 1)
        with transaction(self.engine) as session:
            row = session.execute(sa.select(registry)).mappings().one()
        self.assertEqual((row["first_seen_at"], row["last_seen_at"], row["status"]),
                         (OBSERVED, OBSERVED + timedelta(days=1), "active"))
        hit = self.repo_call("lookup", anchors=["eo:99980", "fr:2026-99901"], stage=STAGE)
        self.assertEqual((hit["status"], hit["policy_id"], hit["anchors"]), ("hit", ROOT_A, ["fr:2026-99901"]))
        for anchors, stage in ((["fr:2026-99901"], ("policy_action", "amended", "original")),  # stage-scoped
                               (["fr:2026-00001"], STAGE), (["headline text"], STAGE), ([], STAGE)):
            self.assertEqual(self.repo_call("lookup", anchors=anchors, stage=stage)["status"], "miss")

    def test_conflicting_root_never_overwrites_and_lookup_fails_closed(self):
        self.register(["fr:2026-99901"], ROOT_A)
        for _ in range(2):  # Idempotent conflict recording.
            result = self.register(["fr:2026-99901"], ROOT_B, observed=OBSERVED + timedelta(days=1))
            self.assertEqual(result["conflicts"], 1)
        [row] = self.repo_call("conflicts")
        self.assertEqual((row["policy_id"], row["status"]), (ROOT_A, "conflicted"))  # Original preserved.
        self.assertEqual(row["attributes"]["conflicting_policy_ids"], [ROOT_B])
        self.assertEqual(self.count(registry), 1)
        self.assertEqual(self.repo_call("lookup", anchors=["fr:2026-99901"], stage=STAGE)["status"], "conflict")
        # Re-registering the original root does not re-activate a conflicted anchor.
        self.register(["fr:2026-99901"], ROOT_A)
        self.assertEqual(self.repo_call("lookup", anchors=["fr:2026-99901"], stage=STAGE)["status"], "conflict")

    def test_two_anchors_to_two_roots_is_conflict_not_choice(self):
        self.register(["fr:2026-99901"], ROOT_A)
        self.register(["eo:99980"], ROOT_B)
        result = self.repo_call("lookup", anchors=["eo:99980", "fr:2026-99901"], stage=STAGE)
        self.assertEqual((result["status"], result["policy_id"]), ("conflict", None))

    def test_invalid_registration_inputs(self):
        for kwargs in (dict(policy_id="short"), dict(stage=("policy_action", "", "original")),
                       dict(observed_at=datetime(2026, 9, 23))):
            values = dict(anchors=["fr:2026-99901"], stage=STAGE, policy_id=ROOT_A, event_key="e", observed_at=OBSERVED)
            values.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.repo_call("register", **values)
        self.assertEqual(self.count(registry), 0)

    def test_lookup_reads_only_the_registry(self):
        self.register(["fr:2026-99901"])
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            self.repo_call("lookup", anchors=["fr:2026-99901", "eo:99980"], stage=STAGE)
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        queries = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(queries), 1)
        self.assertIn("geopolitical_anchor_registry", queries[0])
        for table in ("event_versions", "event_provenance", "event_history", "FROM events"):
            self.assertNotIn(table, queries[0])

    # ------------------------------------------------ shadow registration/backfill

    def test_shadow_persist_registers_resolved_anchors(self):
        before = durable_identity.get_durable_identity_stats()
        result = persist_geopolitical(self.engine, sample("whitehouse_fr_companion"), OBSERVED, report=True)
        self.assertEqual(result["registry"]["inserted"], 1)  # Cached WH analysis: eo:99960 only.
        again = persist_geopolitical(self.engine, sample("whitehouse_fr_companion"), OBSERVED, report=True)
        self.assertEqual(again["registry"]["already_present"], 1)
        after = durable_identity.get_durable_identity_stats()
        self.assertEqual((after["registry_inserted"] - before["registry_inserted"],
                          after["registry_existing"] - before["registry_existing"]), (1, 1))
        hit = self.repo_call("lookup", anchors=["eo:99960"], stage=("trade_action", "adopted", "original"))
        self.assertEqual((hit["status"], hit["event_key"]), ("hit", sample("whitehouse_action")["event_id"]))

    def test_registry_failure_keeps_history_and_is_counted(self):
        before = durable_identity.get_durable_identity_stats()["registry_error"]
        with patch.object(AnchorRegistryRepository, "register", side_effect=RuntimeError("private")):
            result = persist_geopolitical(self.engine, sample("sanctions"), OBSERVED, report=True)
        self.assertIsNone(result["registry"])
        self.assertEqual((self.count(events), self.count(registry)), (1, 0))
        self.assertEqual(durable_identity.get_durable_identity_stats()["registry_error"], before + 1)

    def test_backfill_deterministic_idempotent_and_history_untouched(self):
        for row in corpus.load_corpus()[1]:
            persist_geopolitical(self.engine, row["event"], datetime.fromisoformat(row["observed_at"]),
                                 make_current=row["make_current"])
        live = self.dump((registry,))
        with transaction(self.engine) as session:
            session.execute(registry.delete())  # Simulate history persisted before Phase 2H.
        history = self.dump()
        with transaction(self.engine) as session:
            first = backfill_geopolitical_anchor_registry(session)
        with transaction(self.engine) as session:
            second = backfill_geopolitical_anchor_registry(session)
        self.assertEqual(self.dump(), history)
        self.assertEqual((first["events_scanned"], first["versions_scanned"], first["conflicts"],
                          first["skipped_non_authoritative"], first["truncated"]), (14, 16, 0, 0, False))
        self.assertEqual(first["inserted"], self.count(registry))
        self.assertEqual(first["inserted"] + first["already_present"], first["anchors_seen"])
        self.assertEqual((second["inserted"], second["conflicts"]), (0, 0))
        self.assertEqual(second["already_present"], first["anchors_seen"])
        rebuilt = {r["anchor_type"] + ":" + r["anchor_value"] + ":" + r["policy_stage"] for r in
                   (json.loads(x) for x in self.dump((registry,))["geopolitical_anchor_registry"])}
        original = {r["anchor_type"] + ":" + r["anchor_value"] + ":" + r["policy_stage"] for r in
                    (json.loads(x) for x in live["geopolitical_anchor_registry"])}
        self.assertEqual(rebuilt, original)  # Backfill reproduces live registration.
        print("PHASE2H_BACKFILL " + json.dumps(first, sort_keys=True))

    def test_backfill_reports_historical_divergence_conflict_and_non_authoritative(self):
        redis = MemoryRedis()
        _, joined = collect([corpus.DOCS["bis_final"], FR_WITH_EO], redis)
        corpus.expire(redis, "expire_all")
        _, alone = collect([FR_WITH_EO], redis)
        forged = deepcopy(sample("sanctions"))
        forged["identity_anchors"] = ["ofac:20260922", "rin:0694-AJ00"]
        self.persist(joined + alone + [(forged, {})])
        with transaction(self.engine) as session:
            session.execute(registry.delete())
            summary = backfill_geopolitical_anchor_registry(session)
        self.assertEqual((summary["conflicts"], summary["skipped_non_authoritative"]), (1, 1))
        [conflicted] = self.repo_call("conflicts")
        self.assertEqual(conflicted["anchor_value"], "2026-99901")
        self.assertEqual(self.count(events), 3)  # No merge.

    def test_backfill_bounds(self):
        for bad in (0, True, 2_000_000):
            with self.assertRaises(ValueError), transaction(self.engine) as session:
                backfill_geopolitical_anchor_registry(session, max_events=bad)

    # ---------------------------------------------------------- resolver order

    def resolve(self, event, redis, durable=None):
        return identity.resolve_identity(deepcopy(event), redis, 365 * 86400, durable=durable)

    def test_disabled_resolver_is_unchanged(self):
        base, new = RecordingRedis(), RecordingRedis()
        doc_event = geo.detect_relevance(geo.normalize_document(deepcopy(FR_WITH_EO)))
        with patch.object(identity, "_durable_candidate", side_effect=AssertionError("not used")):
            first = identity.resolve_identity(deepcopy(doc_event), base, 365 * 86400)
        second = identity.resolve_identity(deepcopy(doc_event), new, 365 * 86400, durable=None)
        self.assertEqual(first, second)
        self.assertEqual(base.calls, new.calls)
        self.assertEqual([c[0] for c in new.calls], ["eval"])  # No pre-check reads when disabled.
        enabled = RecordingRedis()
        self.resolve(doc_event, enabled, InlineLookup(self.engine))
        self.assertEqual([c[0] for c in enabled.calls], ["get", "get", "get", "eval"])  # Pre-check reads only.

    def test_redis_hit_bypasses_durable_lookup(self):
        redis = MemoryRedis()
        doc_event = geo.detect_relevance(geo.normalize_document(deepcopy(FR_WITH_EO)))
        first = self.resolve(doc_event, redis)
        lookup = InlineLookup(self.engine, fail=AssertionError("PostgreSQL must not be consulted"))
        again = self.resolve(doc_event, redis, lookup)
        self.assertEqual(again["event_id"], first["event_id"])
        self.assertEqual((lookup.calls, lookup.bypass), ([], 1))

    def test_redis_miss_durable_hit_miss_and_failure(self):
        doc_event = geo.detect_relevance(geo.normalize_document(deepcopy(FR_WITH_EO)))
        today = self.resolve(doc_event, MemoryRedis())["event_id"]
        miss = InlineLookup(self.engine)
        self.assertEqual(self.resolve(doc_event, MemoryRedis(), miss)["event_id"], today)
        self.assertEqual(len(miss.calls), 1)
        failing = InlineLookup(self.engine, fail=PersistenceError("down"))
        self.assertEqual(self.resolve(doc_event, MemoryRedis(), failing)["event_id"], today)
        class Garbage:
            def redis_hit(self): pass
            def lookup(self, anchors, stage): return "not-a-root"
        self.assertEqual(self.resolve(doc_event, MemoryRedis(), Garbage())["event_id"], today)
        self.register(["fr:2026-99901"], ROOT_A)
        hit = self.resolve(doc_event, MemoryRedis(), InlineLookup(self.engine))
        self.assertEqual(hit["policy_id"], ROOT_A)
        self.assertEqual(hit["event_id"], identity.digest(["event-v1", ROOT_A]))

    # --------------------------------------------------- collector acceptance

    def divergence_setup(self):
        redis = MemoryRedis()
        _, joined = collect([corpus.DOCS["bis_final"], FR_WITH_EO], redis)
        self.persist(joined)
        corpus.expire(redis, "expire_all")  # Redis alias/policy expiry.
        return redis, joined

    def test_acceptance_divergence_prevented_with_durable_lookup(self):
        redis, joined = self.divergence_setup()
        lookup = InlineLookup(self.engine)
        _, alone = collect([FR_WITH_EO], redis, lookup)
        self.assertEqual(alone[0][0]["event_id"], joined[0][0]["event_id"])  # Existing durable root reused.
        self.assertEqual(len(lookup.calls), 1)
        self.persist(alone)
        self.assertEqual(self.count(events), 1)  # No second logical event.
        self.assertEqual(self.audit()["summary"]["exact_authoritative_anchor"], 0)

    def test_disabled_mode_reproduces_old_divergence(self):
        redis, joined = self.divergence_setup()
        _, alone = collect([FR_WITH_EO], redis)
        self.assertNotEqual(alone[0][0]["event_id"], joined[0][0]["event_id"])
        self.persist(alone)
        self.assertEqual(self.count(events), 2)
        self.assertEqual(self.audit()["summary"]["exact_authoritative_anchor"], 1)  # Still detectable.

    def test_historical_divergence_still_reported_and_conflict_fails_closed(self):
        redis, joined = self.divergence_setup()
        _, alone = collect([FR_WITH_EO], redis)  # Pre-2H divergence (disabled).
        self.persist(alone)                       # Registers fr:2026-99901 -> second root: conflict.
        before = self.audit()
        corpus.expire(redis, "expire_all")
        lookup = InlineLookup(self.engine)
        _, again = collect([FR_WITH_EO], redis, lookup)
        self.assertEqual(again[0][0]["event_id"], alone[0][0]["event_id"])  # Today's resolver, no choice made.
        self.persist(again)
        self.assertEqual(self.count(events), 2)  # No merge, no new event.
        after = self.audit()
        self.assertEqual(after, before)  # Historical group unchanged and still reported once.
        self.assertEqual(after["summary"]["exact_authoritative_anchor"], 1)

    def test_enabled_with_empty_registry_matches_disabled_across_corpus(self):
        def replay(lookup):
            redis, outputs = MemoryRedis(), []
            for label, name, clock, change in corpus.STEPS:
                redis.now = clock
                if change:
                    corpus.expire(redis, change)
                outputs.append((label, collect([corpus.DOCS[name]], redis, lookup)[0]))
            return outputs
        disabled = replay(None)
        empty = InlineLookup(self.engine)
        self.assertEqual(replay(empty), disabled)  # Events, stats, Redis state, Telegram, AI identical.
        self.assertTrue(empty.calls and empty.bypass)

    def test_db_unavailable_or_slow_does_not_block_collector(self):
        redis, joined = self.divergence_setup()
        baseline_redis = deepcopy(redis)
        baseline, _ = collect([FR_WITH_EO], baseline_redis)
        down = durable_identity.DurableIdentityLookup(timeout_ms=50,
            engine_factory=lambda: (_ for _ in ()).throw(RuntimeError("password=secret")))
        release = Event()
        slow = durable_identity.DurableIdentityLookup(timeout_ms=50)
        self.addCleanup(down.close)
        self.addCleanup(slow.close)
        self.addCleanup(release.set)
        with patch.object(slow, "_query", side_effect=lambda *a: release.wait(5)):
            for lookup, counter in ((down, "lookup_error"), (slow, "lookup_timeout")):
                before = durable_identity.get_durable_identity_stats()[counter]
                started = monotonic()
                with self.assertLogs("geopolitical_durable_identity", level="WARNING") as logs, \
                     patch.dict(durable_identity._last_log, clear=True):
                    outputs, _ = collect([FR_WITH_EO], deepcopy(redis), lookup)
                self.assertLess(monotonic() - started, 1.0)
                self.assertEqual(outputs[:2], baseline[:2])  # Same events and stats as today's resolver.
                self.assertEqual(outputs[3:], baseline[3:])  # Same Telegram and AI calls.
                self.assertEqual(durable_identity.get_durable_identity_stats()[counter], before + 1)
                self.assertNotIn("secret", str(logs.output))

    def test_redis_hit_with_db_unavailable_never_needs_db(self):
        redis = MemoryRedis()
        broken = durable_identity.DurableIdentityLookup(timeout_ms=50,
            engine_factory=lambda: (_ for _ in ()).throw(AssertionError("PostgreSQL consulted")))
        self.addCleanup(broken.close)
        _, first = collect([corpus.DOCS["bis_final"]], redis)
        before = durable_identity.get_durable_identity_stats()
        outputs, companion = collect([FR_WITH_EO], redis, broken)
        after = durable_identity.get_durable_identity_stats()
        self.assertEqual(companion[0][0]["event_id"], first[0][0]["event_id"])
        self.assertEqual(after["redis_hit_bypass"] - before["redis_hit_bypass"], 1)
        self.assertEqual((after["lookup_attempted"], after["lookup_error"]),
                         (before["lookup_attempted"], before["lookup_error"]))
        self.assertEqual(outputs[1]["duplicates"], 1)


class DurableLookupServiceTests(unittest.TestCase):
    def service(self, **kwargs):
        lookup = durable_identity.DurableIdentityLookup(**kwargs)
        self.addCleanup(lookup.close)
        return lookup

    def delta(self, before):
        after = durable_identity.get_durable_identity_stats()
        return {k: after[k] - before[k] for k in durable_identity.COUNTERS if after[k] != before[k]}

    def test_hit_miss_conflict_and_invalid_root(self):
        lookup = self.service(timeout_ms=500)
        for result, expected, counter in (
                (dict(status="hit", policy_id=ROOT_A, event_key="e", anchors=["fr:1"]), ROOT_A, "lookup_hit"),
                (dict(status="miss", policy_id=None, event_key=None, anchors=[]), None, "lookup_miss"),
                (dict(status="conflict", policy_id=None, event_key=None, anchors=["fr:1"]), None, "lookup_conflict"),
                (dict(status="hit", policy_id="bad", event_key="e", anchors=[]), None, "lookup_miss")):
            before = durable_identity.get_durable_identity_stats()
            with patch.object(lookup, "_query", return_value=result), patch.dict(durable_identity._last_log, clear=True):
                self.assertEqual(lookup.lookup(["fr:2026-99901"], STAGE), expected)
            self.assertEqual(self.delta(before), {"lookup_attempted": 1, counter: 1})

    def test_timeout_is_bounded_and_busy_lookups_skip(self):
        lookup = self.service(timeout_ms=30)
        release = Event()
        self.addCleanup(release.set)
        before = durable_identity.get_durable_identity_stats()
        with patch.object(lookup, "_query", side_effect=lambda *a: release.wait(5)):
            started = monotonic()
            self.assertIsNone(lookup.lookup(["fr:2026-99901"], STAGE))
            self.assertIsNone(lookup.lookup(["fr:2026-99901"], STAGE))  # Still running: skip, never queue.
            self.assertLess(monotonic() - started, 0.5)
        self.assertEqual(self.delta(before), {"lookup_attempted": 2, "lookup_timeout": 1, "lookup_skipped_busy": 1})
        release.set()

    def test_no_engine_until_first_lookup_and_engine_recreated_after_error(self):
        created = []
        def factory():
            created.append(1)
            raise PersistenceError("down")
        lookup = self.service(timeout_ms=500, engine_factory=factory)
        self.assertEqual(created, [])
        for _ in range(2):
            self.assertIsNone(lookup.lookup(["fr:2026-99901"], STAGE))
        self.assertEqual(len(created), 2)

    def test_runtime_engine_caps_and_no_credentials(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:secret@localhost/mias_test"}), \
             patch.object(durable_identity, "make_engine") as make:
            durable_identity.runtime_engine(250)
        settings = make.call_args.args[0]
        self.assertEqual((settings.pool_size, settings.max_overflow, settings.connect_timeout_seconds,
                          settings.statement_timeout_ms, settings.lock_timeout_ms, settings.application_name),
                         (1, 0, 2, 250, 250, "mias_geopolitical_identity"))
        self.assertNotIn("secret", repr(settings))
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "DB_ALLOW_SQLITE": "true"}), self.assertRaises(ValueError):
            durable_identity.runtime_engine(250)
        for bad in (0, 9, 5001, 250.0, True):
            with self.assertRaises(ValueError):
                durable_identity.DurableIdentityLookup(timeout_ms=bad)

    def test_config_defaults_and_validation(self):
        for env, enabled, timeout in (({}, False, 250), ({"GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED": "true",
                                                          "GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS": "100"}, True, 100),
                                      ({"GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED": "yes"}, False, 250)):
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, env, clear=True):
                config = runpy.run_path("shared/config.py")
            self.assertEqual((config["GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED"],
                              config["GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS"]), (enabled, timeout))
            self.assertIs(config["GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED"], False)
        for bad in ("5", "6000"):
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, {"GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS": bad},
                                                         clear=True), self.assertRaises(ValueError):
                runpy.run_path("shared/config.py")

    def test_import_opens_no_connection_and_reads_no_dotenv(self):
        code = ("import sys; import persistence.geopolitical_durable_identity as d, persistence.geopolitical_registry; "
                "print(d._lookup is None, 'dotenv' in sys.modules, 'shared.config' in sys.modules, 'psycopg' in sys.modules)")
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), env=env)
        self.assertEqual(result.stdout.split(), ["True", "False", "False", "False"], result.stderr)
