"""Phase 2G lifecycle equivalence: macro, Treasury and geopolitical module lifecycles behave identically.

Written and run against the pre-refactor modules first, then kept unchanged to
prove the shared lifecycle helper preserved observable behavior.
"""
from contextlib import ExitStack
import logging
import unittest
from unittest.mock import patch

from persistence import macro_shadow, treasury_shadow, geopolitical_shadow
from persistence.database import PersistenceError
from tests.test_macro_persistence_adapter import sample as macro_sample
from tests.test_treasury_persistence_adapter import sample as treasury_sample
from tests.test_geopolitical_persistence_adapter import sample as geopolitical_sample
from tests.test_treasury_shadow_persistence import FakeEngine

MODULES = (
    # module, submit, writer class, persist callback, label, logger, sample factory
    (macro_shadow, "submit_macro", "ShadowWriter", "persist_macro", "Macro", "macro_shadow", macro_sample),
    (treasury_shadow, "submit_treasury", "TreasuryShadowWriter", "persist_treasury", "Treasury", "treasury_shadow", treasury_sample),
    (geopolitical_shadow, "submit_geopolitical", "GeopoliticalShadowWriter", "persist_geopolitical", "Geopolitical",
     "geopolitical_shadow", geopolitical_sample),
)
SUBMISSION_KEYS = {"dropped_initializing", "failed_initializing", "rejected_shutdown", "last_failure_at"}
PUBLIC = {"get_persistence_stats", "shutdown", "close_shadow", "runtime_engine", "_after_fork"}


def fresh(module, stack, writer_class=None):
    """Isolate a module's lifecycle state exactly as the prior phase tests do."""
    stack.enter_context(patch.object(module, "_writer", None))
    stack.enter_context(patch.object(module, "_shutdown_requested", False))
    stack.enter_context(patch.object(module, "_submission_stats", dict(
        dropped_initializing=0, failed_initializing=0, rejected_shutdown=0, last_failure_at=None)))
    stack.enter_context(patch.object(module, "_submission_last_warning", float("-inf")))
    if writer_class is not None:
        stack.enter_context(patch.object(module, MODULES_BY[module][2], writer_class))


MODULES_BY = {m[0]: m for m in MODULES}


class RecordingWriter:
    created = []

    def __init__(self):
        RecordingWriter.created.append(self)
        self.submitted, self.shutdowns = [], []

    def submit(self, event, make_current=True):
        self.submitted.append((event, make_current))
        return True

    def get_persistence_stats(self):
        stats = macro_shadow.empty_stats()
        stats.update(queued=len(self.submitted), last_failure_at="2026-01-01T00:00:00+00:00")
        return stats

    def shutdown(self, drain=True, timeout=2):
        self.shutdowns.append((drain, timeout))
        return dict(stopped=True, timed_out=False, unprocessed=0, stats=self.get_persistence_stats())


def scrub(stats):
    return {k: v for k, v in stats.items() if k not in {"last_success_at", "last_failure_at"}}


class LifecycleEquivalenceTests(unittest.TestCase):
    def setUp(self):
        RecordingWriter.created = []

    def each(self):
        for entry in MODULES:
            with self.subTest(module=entry[4]):
                yield entry

    def test_public_lifecycle_surface_is_unchanged(self):
        for module, submit, writer, persist, *_ in self.each():
            for name in PUBLIC | {submit, writer, persist, "logger", "_writer", "_lock", "_shutdown_requested",
                                  "_submission_lock", "_submission_stats", "_submission_last_warning"}:
                self.assertTrue(hasattr(module, name), name)

    def test_stats_without_writer_never_initialize(self):
        for module, _, writer, *_ in self.each():
            with ExitStack() as stack:
                fresh(module, stack)
                stack.enter_context(patch.object(module, writer, side_effect=AssertionError("worker")))
                stats = module.get_persistence_stats()
                self.assertEqual(set(stats), set(macro_shadow.empty_stats()) | SUBMISSION_KEYS)
                self.assertEqual(stats["worker_started"], 0)
                self.assertIsNone(module._writer)

    def test_lazy_singleton_reuse_and_argument_passthrough(self):
        for module, submit, *_rest, sample in self.each():
            RecordingWriter.created = []
            with ExitStack() as stack:
                fresh(module, stack, RecordingWriter)
                self.assertEqual(RecordingWriter.created, [])
                self.assertTrue(getattr(module, submit)(sample()))
                self.assertTrue(getattr(module, submit)(sample(), make_current=False))
                self.assertEqual(len(RecordingWriter.created), 1)
                writer = RecordingWriter.created[0]
                self.assertIs(module._writer, writer)
                self.assertEqual([flag for _, flag in writer.submitted], [True, False])
                self.assertEqual(module.get_persistence_stats()["queued"], 2)

    def test_submission_failures_counted_and_logged_once(self):
        for module, submit, writer, _, label, logger, sample in self.each():
            with ExitStack() as stack:
                fresh(module, stack)
                stack.enter_context(patch.object(module, writer, side_effect=RuntimeError("secret")))
                with self.assertLogs(logger, level="WARNING") as logs:
                    self.assertFalse(getattr(module, submit)(sample()))
                    with module._lock:
                        self.assertFalse(getattr(module, submit)(sample()))
                    module.shutdown()
                    self.assertFalse(getattr(module, submit)(sample()))
                self.assertEqual(logs.output, [f"WARNING:{logger}:{label} shadow submission unavailable; snapshot dropped"])
                stats = module.get_persistence_stats()
                self.assertEqual((stats["failed_initializing"], stats["dropped_initializing"], stats["rejected_shutdown"]), (1, 1, 1))
                self.assertIsNotNone(stats["last_failure_at"])
                self.assertEqual(stats["worker_started"], 0)

    def test_disabled_shutdown_shape_idempotence_and_validation(self):
        for module, _, writer, *_ in self.each():
            with ExitStack() as stack:
                fresh(module, stack)
                stack.enter_context(patch.object(module, writer, side_effect=AssertionError("worker")))
                first = module.shutdown()
                self.assertEqual(set(first), {"stopped", "timed_out", "unprocessed", "stats"})
                self.assertEqual((first["stopped"], first["timed_out"], first["unprocessed"]), (True, False, 0))
                self.assertEqual(module.shutdown(drain=False, timeout=0), first)
                self.assertTrue(module.close_shadow()["stopped"])
                for timeout in (None, -1, 31, float("nan")):
                    with self.assertRaises(ValueError):
                        module.shutdown(timeout=timeout)
                with self.assertRaises(ValueError):
                    module.shutdown(drain="yes")

    def test_shutdown_delegates_and_merges_submission_stats(self):
        for module, submit, *_rest, sample in self.each():
            RecordingWriter.created = []
            with ExitStack() as stack:
                fresh(module, stack, RecordingWriter)
                getattr(module, submit)(sample())
                with module._lock:
                    getattr(module, submit)(sample())  # One dropped_initializing.
                result = module.shutdown(drain=False, timeout=1.5)
                self.assertEqual(RecordingWriter.created[0].shutdowns, [(False, 1.5)])
                self.assertEqual((result["stats"]["queued"], result["stats"]["dropped_initializing"]), (1, 1))
                self.assertGreater(result["stats"]["last_failure_at"], "2026-01-01T00:00:00+00:00")
                self.assertFalse(getattr(module, submit)(sample()))
                self.assertEqual(module.get_persistence_stats()["rejected_shutdown"], 1)

    def test_fork_reset_restores_fresh_state(self):
        for module, *_ in self.each():
            with ExitStack() as stack:
                fresh(module, stack)
                stack.enter_context(patch.object(module, "_lock", module._lock))
                stack.enter_context(patch.object(module, "_submission_lock", module._submission_lock))
                old_lock = module._lock
                module._shutdown_requested = True
                module._submission_stats["failed_initializing"] = 5
                module._after_fork()
                self.assertIsNone(module._writer)
                self.assertFalse(module._shutdown_requested)
                self.assertEqual(module._submission_stats["failed_initializing"], 0)
                self.assertIsNot(module._lock, old_lock)
                self.assertEqual(module._submission_last_warning, float("-inf"))

    def test_real_writer_outage_queue_and_restart_stats_identical_across_modules(self):
        """Same script against each real writer class: counters must match exactly."""
        results = []
        for module, _, writer_name, persist, label, logger, sample in self.each():
            with patch.object(module, persist, side_effect=PersistenceError("private")), \
                 self.assertLogs(logger, level="WARNING") as logs:
                writer = getattr(module, writer_name)(engine_factory=FakeEngine, capacity=2)
                for _ in range(2): writer.submit(sample())
                stopped = writer.shutdown(timeout=5)
                self.assertFalse(writer.submit(sample()))
                stopped["stats"] = writer.get_persistence_stats()  # Includes the rejected submission.
                restarted = getattr(module, writer_name)(engine_factory=FakeEngine, capacity=2)
                restarted.submit(sample())
                again = restarted.shutdown(timeout=5)
            self.assertTrue(all(f"{label} shadow" in line for line in logs.output))
            self.assertTrue(all(line.startswith("WARNING:" + logger) for line in logs.output))
            results.append((scrub(stopped["stats"]), scrub(again["stats"]), stopped["stopped"], again["stopped"]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])
        self.assertEqual((results[0][0]["failed"], results[0][0]["rejected_shutdown"]), (2, 1))

    def test_writer_message_catalogues_and_severity_equivalent(self):
        base = macro_shadow._MESSAGES
        for module, _, writer_name, _, label, logger, _ in self.each():
            writer = getattr(module, writer_name)
            self.assertEqual(writer.messages, {k: v.replace("Macro shadow", f"{label} shadow") for k, v in base.items()})
            self.assertEqual(writer.logger.name, logger)
            self.assertEqual(writer.thread_name, f"mias-{label.lower()}-shadow")
            self.assertTrue(logging.getLogger(logger) is writer.logger)


@unittest.skipUnless(__import__("os").environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLModuleLifecycleTests(unittest.TestCase):
    """Refactored module lifecycles, end to end on real PostgreSQL transactions."""

    def setUp(self):
        from tests import test_persistence_postgres as foundation
        foundation.PostgreSQLTests.setUp(self)

    def cleanup_schema(self):
        from tests import test_persistence_postgres as foundation
        foundation.PostgreSQLTests.cleanup_schema(self)

    def test_module_submit_drain_restart_dedup_and_reconcile(self):
        from persistence import reconciliation
        from persistence.database import transaction
        from persistence.repository import EventRepository
        audits = {"Macro": reconciliation.reconcile_macro_event, "Treasury": reconciliation.reconcile_treasury_event,
                  "Geopolitical": reconciliation.reconcile_geopolitical_event}
        for module, submit, writer_name, _, label, _, sample in MODULES:
            with self.subTest(module=label):
                real = getattr(module, writer_name)
                for expected_duplicates in (0, 1):  # Second pass is a fresh "process".
                    with ExitStack() as stack:
                        fresh(module, stack)
                        stack.enter_context(patch.object(module, writer_name, lambda: real(engine_factory=lambda: self.engine)))
                        self.assertTrue(getattr(module, submit)(sample()))
                        result = module.shutdown(timeout=10)
                    self.assertTrue(result["stopped"])
                    stats = result["stats"]
                    self.assertEqual((stats["persisted"], stats["duplicate"], stats["failed"], stats["worker_started"],
                                      stats["worker_stopped"]), (1, expected_duplicates, 0, 1, 1))
                with transaction(self.engine) as session:
                    self.assertEqual(audits[label](sample(), EventRepository(session))["mismatches"], [])
