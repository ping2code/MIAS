"""Shared module-level lifecycle for shadow writers.

Covers only what macro, Treasury and geopolitical modules had duplicated: the lazy
singleton, non-blocking submission accounting, detached stats, bounded shutdown,
fork reset and the atexit drain. Collector modules still own their writer class,
adapter, persist callback, logger/messages, application_name, reconciliation and
config switch.

State deliberately stays in the owning module's namespace (``_writer``, ``_lock``,
``_shutdown_requested``, ``_submission_lock``, ``_submission_stats``,
``_submission_last_warning``) and is read at call time, so the established
module attributes, fork semantics and test patch points are unchanged.
"""
import atexit
from datetime import datetime, timezone
import math
import os
from threading import Lock
from time import monotonic


def empty_stats():
    return dict(queued=0, persisted=0, duplicate=0, failed=0, dropped_queue_full=0,
                dropped_shutdown=0, rejected_shutdown=0, dropped_invalid=0,
                worker_started=0, worker_stopped=0, drain_timeouts=0, cleanup_failed=0,
                promotion_held=0, promotion_ambiguous=0, promotion_disclosure_only=0,
                queue_depth=0, in_flight=0, last_success_at=None, last_failure_at=None)


def _validate_shutdown(drain, timeout):
    if type(drain) is not bool:
        raise ValueError("Drain must be a boolean")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
        raise ValueError("Shutdown timeout must be finite and between 0 and 30 seconds")


def _submission_defaults():
    return dict(dropped_initializing=0, failed_initializing=0, rejected_shutdown=0, last_failure_at=None)


class ShadowLifecycle:
    """Bind one lazily created writer to a module; never touches a database itself."""

    def __init__(self, namespace, *, writer, label):
        # ``writer`` and ``logger`` are looked up by name in the module at call time.
        self.ns, self.writer_name = namespace, writer
        self.message = f"{label} shadow submission unavailable; snapshot dropped"
        self.reset()
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self.reset)
        atexit.register(self.close)

    def reset(self):
        """Fresh process-local state (fork child); never inherits a parent's worker."""
        self.ns.update(_writer=None, _lock=Lock(), _shutdown_requested=False, _submission_lock=Lock(),
                       _submission_stats=_submission_defaults(), _submission_last_warning=float("-inf"))

    def _failure(self, reason):
        ns = self.ns
        with ns["_submission_lock"]:
            ns["_submission_stats"][reason] += 1
            ns["_submission_stats"]["last_failure_at"] = datetime.now(timezone.utc).isoformat()
            warn = monotonic() - ns["_submission_last_warning"] >= 60
            if warn:
                ns["_submission_last_warning"] = monotonic()
        if warn:
            try:
                ns["logger"].warning(self.message)
            except Exception:
                pass

    def submit(self, event, *, make_current=True):
        ns = self.ns
        lock = ns["_lock"]
        # Concurrent initialization must not hold up a collector.
        if not lock.acquire(blocking=False):
            self._failure("dropped_initializing")
            return False
        reason = None
        try:
            if ns["_shutdown_requested"]:
                reason = "rejected_shutdown"
            elif ns["_writer"] is None:
                try:
                    ns["_writer"] = ns[self.writer_name]()
                except Exception:
                    reason = "failed_initializing"
            writer = ns["_writer"]
        finally:
            lock.release()
        if reason:
            self._failure(reason)
            return False
        return writer.submit(event, make_current=make_current)

    def _with_submission_stats(self, stats):
        ns = self.ns
        with ns["_submission_lock"]:
            for key, value in ns["_submission_stats"].items():
                if key == "last_failure_at":
                    stamps = [stamp for stamp in (stats[key], value) if stamp is not None]
                    stats[key] = max(stamps) if stamps else None
                else:
                    stats[key] = stats.get(key, 0) + value
        return stats

    def get_persistence_stats(self):
        writer = self.ns["_writer"]
        return self._with_submission_stats(writer.get_persistence_stats() if writer is not None else empty_stats())

    def shutdown(self, drain=True, timeout=2):
        _validate_shutdown(drain, timeout)
        ns = self.ns
        # No initialization; safe when disabled. Never hold the module lock while draining.
        with ns["_lock"]:
            ns["_shutdown_requested"] = True
            writer = ns["_writer"]
        if writer is None:
            return dict(stopped=True, timed_out=False, unprocessed=0, stats=self.get_persistence_stats())
        result = writer.shutdown(drain=drain, timeout=timeout)
        result["stats"] = self._with_submission_stats(result["stats"])
        return result

    def close(self):
        return self.shutdown(drain=True, timeout=2)
