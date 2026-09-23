"""Best-effort bounded macro shadow writes; no DB work on the collector thread."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from collections import deque
from threading import Thread, Event, Lock, Condition
from time import monotonic

from persistence.adapters.macro import adapt_macro, macro_promotion
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction, PersistenceError
from persistence.repository import EventRepository
from persistence.shadow_lifecycle import ShadowLifecycle, empty_stats, _validate_shutdown  # noqa: F401 (re-exported)
from shared.logger import get_logger

logger = get_logger("macro_shadow")


def persist_macro(engine, event, observed_at, *, make_current=True, report=False):
    """One atomic transaction: facts, evidence, then existing outcome snapshots."""
    adapted = adapt_macro(event, observed_at, make_current=make_current)
    with transaction(engine) as session:
        repo = EventRepository(session)
        row = repo.record(**adapted["record"], promotion_policy=macro_promotion)
        for key, values in adapted["provenance"]:
            repo.add_provenance(row["id"], key, **values)
        for kind, values in adapted["histories"]:
            repo.append_history(row["id"], kind, values)
    return {"version": row, "duplicate": repo.inserted_count == 0,
            "promotion": repo.promotion_reason} if report else row


def runtime_engine(application_name="mias_macro_shadow"):
    settings = DatabaseSettings.from_env()
    if settings.backend != "postgresql":
        raise ValueError("Shadow persistence requires PostgreSQL")
    # Worker only; never wait/retry in the collector. Cap caller-provided limits.
    settings = replace(settings, pool_size=1, max_overflow=0, pool_timeout_seconds=1,
                       connect_timeout_seconds=2, statement_timeout_ms=1000,
                       lock_timeout_ms=500, application_name=application_name)
    return make_engine(settings)


_MESSAGES = {
    "success": "Macro shadow persistence success",
    "duplicate": "Macro shadow persistence duplicate",
    "database_failure": "Macro shadow database failure",
    "task_failure": "Macro shadow task failure",
    "queue_full": "Macro shadow queue full; snapshot dropped",
    "invalid": "Macro shadow invalid snapshot; dropped",
    "drain_timeout": "Macro shadow drain timeout; pending work discarded; check in_flight",
    "start": "Macro shadow worker started",
    "stop": "Macro shadow worker stopped",
    "promotion_held": "Macro shadow current version retained by source-order policy",
    "promotion_ambiguous": "Macro shadow ambiguous version ordering; current retained",
}


class ShadowWriter:
    """Source-neutral bounded worker; subclasses supply only persist/log labels."""
    logger = logger
    messages = _MESSAGES
    thread_name = "mias-macro-shadow"

    def _persist(self, engine, event, observed_at, make_current):
        # Module-level lookup keeps the macro persist function patchable in tests.
        return persist_macro(engine, event, observed_at, make_current=make_current, report=True)

    def __init__(self, engine_factory=runtime_engine, capacity=64):
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise ValueError("Shadow queue capacity must be between 1 and 4096")
        self._queue = deque()
        self.capacity = capacity
        self.engine_factory = engine_factory
        self.stopping = Event()
        self._condition = Condition()
        self._stats = empty_stats()
        self._timeout_reported = False
        self._last_log = {}
        self.log_lock = Lock()
        self.thread = Thread(target=self._run, name=self.thread_name, daemon=True)
        self.thread.start()

    def _log(self, outcome):
        # Never hold lifecycle/stats locks across logging or database calls.
        with self.log_lock:
            now = monotonic()
            if now - self._last_log.get(outcome, float("-inf")) < 60:
                return
            self._last_log[outcome] = now
        try:
            emit = self.logger.info if outcome in {"success", "duplicate", "start", "stop"} else self.logger.warning
            emit(self.messages[outcome])
        except Exception:
            pass  # An unavailable log sink cannot kill the worker or affect alerts.

    def failure(self):
        """Compatibility logging hook; task accounting is owned by the worker."""
        self._log("task_failure")

    def get_persistence_stats(self):
        """Detached, coherent per-writer snapshot; never initializes a database."""
        with self._condition:
            return dict(self._stats)

    def submit(self, event, *, make_current=True):
        try:
            snapshot = deepcopy(event)
            if len(json.dumps(snapshot, allow_nan=False).encode()) > 131072:
                raise ValueError("Shadow snapshot exceeds limit")
        except Exception:
            with self._condition:
                self._stats["dropped_invalid"] += 1
                self._stats["last_failure_at"] = datetime.now(timezone.utc).isoformat()
            self._log("invalid")
            return False
        with self._condition:
            if self.stopping.is_set():
                self._stats["rejected_shutdown"] += 1
                self._stats["last_failure_at"] = datetime.now(timezone.utc).isoformat()
                return False
            if len(self._queue) >= self.capacity:
                self._stats["dropped_queue_full"] += 1
                self._stats["last_failure_at"] = datetime.now(timezone.utc).isoformat()
                full = True
            else:
                self._queue.append((snapshot, datetime.now(timezone.utc), make_current))
                self._stats["queued"] += 1
                self._stats["queue_depth"] = len(self._queue)
                self._condition.notify()
                full = False
        if full:
            self._log("queue_full")
        return not full

    def _run(self):
        engine = None
        with self._condition:
            self._stats["worker_started"] += 1
        self._log("start")
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._queue or self.stopping.is_set())
                    if not self._queue:
                        break
                    event, observed_at, make_current = self._queue.popleft()
                    self._stats["queue_depth"] = len(self._queue)
                    self._stats["in_flight"] = 1
                try:
                    if engine is None:
                        engine = self.engine_factory()
                    result = self._persist(engine, event, observed_at, make_current)
                except Exception as error:
                    # Discard a possibly poisoned/stale pool. The next accepted
                    # task gets a fresh lazy engine; this is bounded recovery,
                    # not a retry of the failed task.
                    if engine is not None:
                        try:
                            engine.dispose()
                        except Exception:
                            pass
                        engine = None
                    with self._condition:
                        self._stats["failed"] += 1
                        self._stats["last_failure_at"] = datetime.now(timezone.utc).isoformat()
                        self._stats["in_flight"] = 0
                    self._log("database_failure" if isinstance(error, PersistenceError) else "task_failure")
                else:
                    duplicate = bool(result and result.get("duplicate"))
                    promotion = result.get("promotion") if result else None
                    held = promotion in {"older", "cosmetic", "ambiguous", "caller_disabled"}
                    with self._condition:
                        self._stats["persisted"] += 1
                        self._stats["duplicate"] += int(duplicate)
                        self._stats["promotion_held"] += int(held)
                        self._stats["promotion_ambiguous"] += int(promotion == "ambiguous")
                        self._stats["last_success_at"] = datetime.now(timezone.utc).isoformat()
                        self._stats["in_flight"] = 0
                    self._log("duplicate" if duplicate else "success")
                    if held:
                        self._log("promotion_ambiguous" if promotion == "ambiguous" else "promotion_held")
        finally:
            try:
                if engine is not None:
                    engine.dispose()
            except Exception:
                with self._condition:
                    self._stats["cleanup_failed"] += 1
                    self._stats["last_failure_at"] = datetime.now(timezone.utc).isoformat()
                self._log("database_failure")
            finally:
                with self._condition:
                    self._stats["worker_stopped"] += 1
                self._log("stop")

    def _discard_pending(self):
        # Caller holds _condition. A claimed transaction is never misreported as dropped.
        if self._queue:
            self._stats["last_failure_at"] = datetime.now(timezone.utc).isoformat()
        self._stats["dropped_shutdown"] += len(self._queue)
        self._queue.clear()
        self._stats["queue_depth"] = 0

    def shutdown(self, drain=True, timeout=2):
        """Stop accepting work; bound caller wait and report any in-flight transaction.

        Pending tasks are discarded on timeout or immediate shutdown. An already
        running DB call cannot be safely killed; it finishes under driver limits.
        """
        _validate_shutdown(drain, timeout)
        with self._condition:
            self.stopping.set()
            if not drain:
                self._discard_pending()
            self._condition.notify_all()
        self.thread.join(timeout=timeout if drain else min(timeout, 0.1))
        with self._condition:
            stopped = not self.thread.is_alive()
            timed_out = not stopped and drain
            if timed_out:
                self._discard_pending()
                if not self._timeout_reported:
                    self._stats["drain_timeouts"] += 1
                    self._stats["last_failure_at"] = datetime.now(timezone.utc).isoformat()
                    self._timeout_reported = True
            stats = dict(self._stats)
        if timed_out:
            self._log("drain_timeout")
        return dict(stopped=stopped, timed_out=bool(timed_out),
                    unprocessed=stats["dropped_shutdown"] + stats["queue_depth"] + stats["in_flight"],
                    stats=stats)

    def close(self, timeout=2):
        """Phase 2B compatibility wrapper; prefer shutdown() for full accounting."""
        return self.shutdown(drain=True, timeout=timeout)["stopped"]


# Shared lifecycle; state stays in this module's namespace (see shadow_lifecycle).
_lifecycle = ShadowLifecycle(globals(), writer="ShadowWriter", label="Macro")
submit_macro = _lifecycle.submit
get_persistence_stats = _lifecycle.get_persistence_stats
shutdown = _lifecycle.shutdown
close_shadow = _lifecycle.close
_after_fork = _lifecycle.reset
