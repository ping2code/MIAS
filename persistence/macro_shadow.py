"""Best-effort bounded macro shadow writes; no DB work on the collector thread."""
import atexit
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from queue import Queue, Empty, Full
from threading import Thread, Event, Lock
from time import monotonic

from persistence.adapters.macro import adapt_macro
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.repository import EventRepository
from shared.logger import get_logger

logger = get_logger("macro_shadow")


def persist_macro(engine, event, observed_at, *, make_current=True):
    """One atomic transaction: facts, evidence, then existing outcome snapshots."""
    adapted = adapt_macro(event, observed_at, make_current=make_current)
    with transaction(engine) as session:
        repo = EventRepository(session)
        row = repo.record(**adapted["record"])
        for key, values in adapted["provenance"]:
            repo.add_provenance(row["id"], key, **values)
        for kind, values in adapted["histories"]:
            repo.append_history(row["id"], kind, values)
    return row


def runtime_engine():
    settings = DatabaseSettings.from_env()
    if settings.backend != "postgresql":
        raise ValueError("Macro shadow requires PostgreSQL")
    # Worker only; never wait/retry in the collector. Cap caller-provided limits.
    settings = replace(settings, pool_size=1, max_overflow=0, pool_timeout_seconds=1,
                       connect_timeout_seconds=2, statement_timeout_ms=1000,
                       lock_timeout_ms=500, application_name="mias_macro_shadow")
    return make_engine(settings)


class ShadowWriter:
    def __init__(self, engine_factory=runtime_engine, capacity=64):
        self.queue = Queue(maxsize=capacity)
        self.engine_factory = engine_factory
        self.stopping = Event()
        self.last_failure = float("-inf")
        self.log_lock = Lock()
        self.thread = Thread(target=self._run, name="mias-macro-shadow", daemon=True)
        self.thread.start()

    def failure(self):
        # Fixed message: no driver errors, URLs, input payloads, or credentials.
        with self.log_lock:
            now = monotonic()
            if now - self.last_failure >= 60:
                self.last_failure = now
                logger.warning("Macro shadow persistence failed or dropped (best effort)")

    def submit(self, event, *, make_current=True):
        try:
            if self.stopping.is_set():
                return False
            snapshot = deepcopy(event)
            if len(json.dumps(snapshot, allow_nan=False).encode()) > 131072:
                raise ValueError("Shadow snapshot exceeds limit")
            self.queue.put_nowait((snapshot, datetime.now(timezone.utc), make_current))
            return True
        except (Full, ValueError, TypeError):
            self.failure()
            return False

    def _run(self):
        engine = None
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    event, observed_at, make_current = self.queue.get(timeout=0.1)
                except Empty:
                    continue
                try:
                    if engine is None:
                        engine = self.engine_factory()
                    persist_macro(engine, event, observed_at, make_current=make_current)
                    logger.info("Macro shadow persisted (idempotent snapshot)")
                except Exception:
                    self.failure()
                finally:
                    self.queue.task_done()
        finally:
            if engine is not None:
                engine.dispose()

    def close(self, timeout=2):
        """Bounded best-effort drain, never an alert-delivery dependency."""
        self.stopping.set()
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()


_writer = None
_lock = Lock()


def _after_fork():
    global _writer, _lock
    _writer, _lock = None, Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def submit_macro(event, *, make_current=True):
    global _writer
    # Concurrent initialization must not hold up a collector.
    if not _lock.acquire(blocking=False):
        return False
    try:
        if _writer is None:
            _writer = ShadowWriter()
        return _writer.submit(event, make_current=make_current)
    finally:
        _lock.release()


@atexit.register
def close_shadow():
    if _writer is not None:
        _writer.close(timeout=2)
