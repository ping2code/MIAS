"""Best-effort bounded shadow persistence of technical snapshots (Phase 4C).

This reuses the generic bounded worker from ``persistence.macro_shadow.ShadowWriter``:

- a bounded queue, and non-blocking submission (a full queue drops and counts);
- one worker thread with a lazy engine;
- failure accounting and bounded drain/shutdown;
- no retry and no hidden replay.

Only the persist callback and labels are technical-specific. The worker's event
semantics (promotion, ``make_current``) are not used: a technical row is immutable
and stored through ``TechnicalSnapshotRepository``.

Outcome accounting on top of the shared counters:

- ``persisted``: inserted or idempotent duplicate (``duplicate`` counts the latter);
- ``conflict``: the identity exists with different content. The row is untouched,
  the rejection is recorded, and it is also counted in ``failed`` (the write did
  not happen);
- ``failed``: database or task failure; ``dropped_*`` and ``rejected_shutdown``: the
  snapshot was never attempted.

A database failure never changes technical output and never fails the runner.
"""
from threading import Lock

from persistence import macro_shadow
from persistence.shadow_lifecycle import ShadowLifecycle
from persistence.technical_snapshot_repository import SnapshotRowError, persist_technical_snapshot
from shared.logger import get_logger

logger = get_logger("technical_shadow")


def runtime_engine():
    return macro_shadow.runtime_engine(application_name="mias_technical_shadow")


_MESSAGES = {key: value.replace("Macro shadow", "Technical snapshot shadow")
             for key, value in macro_shadow._MESSAGES.items()}
_MESSAGES["conflict"] = "Technical snapshot shadow conflict: existing snapshot kept; rejection recorded"


class SnapshotConflict(RuntimeError):
    """Raised inside the worker so the shared worker counts the write as not persisted."""


class TechnicalSnapshotWriter(macro_shadow.ShadowWriter):
    logger = logger
    messages = _MESSAGES
    thread_name = "mias-technical-shadow"

    def __init__(self, engine_factory=runtime_engine, capacity=256):
        self._conflict_lock, self._conflicts, self._invalid_rows = Lock(), 0, 0
        super().__init__(engine_factory=engine_factory, capacity=capacity)

    def _persist(self, engine, row, observed_at, make_current):
        try:
            result = persist_technical_snapshot(engine, row)
        except SnapshotRowError:
            with self._conflict_lock:
                self._invalid_rows += 1
            raise
        if result["outcome"] == "conflict":
            with self._conflict_lock:
                self._conflicts += 1
            self._log("conflict")
            raise SnapshotConflict("technical snapshot identity conflict")
        return dict(duplicate=result["outcome"] == "duplicate")

    def get_persistence_stats(self):
        stats = super().get_persistence_stats()
        with self._conflict_lock:
            stats.update(conflict=self._conflicts, invalid_row=self._invalid_rows)
        return stats

    def shutdown(self, drain=True, timeout=2):
        result = super().shutdown(drain=drain, timeout=timeout)
        with self._conflict_lock:
            result["stats"].update(conflict=self._conflicts, invalid_row=self._invalid_rows)
        return result


def _configured_writer():
    import os
    from persistence.technical_settings import load_technical_persistence_settings
    return TechnicalSnapshotWriter(capacity=load_technical_persistence_settings(os.environ).queue_size)


# Shared lifecycle; state stays in this module's namespace (see shadow_lifecycle).
_lifecycle = ShadowLifecycle(globals(), writer="_configured_writer", label="Technical snapshot")
submit_snapshot = _lifecycle.submit
get_persistence_stats = _lifecycle.get_persistence_stats
shutdown = _lifecycle.shutdown
close_shadow = _lifecycle.close
_after_fork = _lifecycle.reset
