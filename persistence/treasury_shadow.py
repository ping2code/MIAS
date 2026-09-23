"""Best-effort bounded Treasury shadow writes; reuses the macro writer contract."""
import atexit
from datetime import datetime, timezone
import os
from threading import Lock
from time import monotonic

from persistence import macro_shadow
from persistence.adapters.treasury import adapt_treasury, treasury_promotion
from persistence.database import transaction
from persistence.macro_shadow import empty_stats, _validate_shutdown
from persistence.repository import EventRepository
from shared.logger import get_logger

logger = get_logger("treasury_shadow")


def persist_treasury(engine, event, observed_at, *, make_current=True, report=False):
    """One atomic transaction: facts, evidence, then existing outcome snapshots."""
    adapted = adapt_treasury(event, observed_at, make_current=make_current)
    with transaction(engine) as session:
        repo = EventRepository(session)
        row = repo.record(**adapted["record"], promotion_policy=treasury_promotion)
        for key, values in adapted["provenance"]:
            repo.add_provenance(row["id"], key, **values)
        for kind, values in adapted["histories"]:
            repo.append_history(row["id"], kind, values)
    return {"version": row, "duplicate": repo.inserted_count == 0,
            "promotion": repo.promotion_reason} if report else row


def runtime_engine():
    return macro_shadow.runtime_engine(application_name="mias_treasury_shadow")


_MESSAGES = {key: value.replace("Macro shadow", "Treasury shadow")
             for key, value in macro_shadow._MESSAGES.items()}


class TreasuryShadowWriter(macro_shadow.ShadowWriter):
    """Same queue, counters, logging bounds and shutdown semantics as macro."""
    logger = logger
    messages = _MESSAGES
    thread_name = "mias-treasury-shadow"

    def __init__(self, engine_factory=runtime_engine, capacity=64):
        super().__init__(engine_factory=engine_factory, capacity=capacity)

    def _persist(self, engine, event, observed_at, make_current):
        # Module-level lookup keeps the Treasury persist function patchable in tests.
        return persist_treasury(engine, event, observed_at, make_current=make_current, report=True)


_writer = None
_lock = Lock()
_shutdown_requested = False
_submission_lock = Lock()
_submission_stats = dict(dropped_initializing=0, failed_initializing=0, rejected_shutdown=0,
                         last_failure_at=None)
_submission_last_warning = float("-inf")


def _submission_failure(reason):
    global _submission_last_warning
    with _submission_lock:
        _submission_stats[reason] += 1
        _submission_stats["last_failure_at"] = datetime.now(timezone.utc).isoformat()
        warn = monotonic() - _submission_last_warning >= 60
        if warn:
            _submission_last_warning = monotonic()
    if warn:
        try:
            logger.warning("Treasury shadow submission unavailable; snapshot dropped")
        except Exception:
            pass


def _after_fork():
    global _writer, _lock, _shutdown_requested, _submission_lock, _submission_stats, _submission_last_warning
    _writer, _lock = None, Lock()
    _shutdown_requested = False
    _submission_lock = Lock()
    _submission_stats = dict(dropped_initializing=0, failed_initializing=0, rejected_shutdown=0,
                             last_failure_at=None)
    _submission_last_warning = float("-inf")


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def submit_treasury(event, *, make_current=True):
    global _writer
    # Concurrent initialization must not hold up a collector.
    if not _lock.acquire(blocking=False):
        _submission_failure("dropped_initializing")
        return False
    reason = None
    try:
        if _shutdown_requested:
            reason = "rejected_shutdown"
        elif _writer is None:
            try:
                _writer = TreasuryShadowWriter()
            except Exception:
                reason = "failed_initializing"
        writer = _writer
    finally:
        _lock.release()
    if reason:
        _submission_failure(reason)
        return False
    return writer.submit(event, make_current=make_current)


def _with_submission_stats(stats):
    with _submission_lock:
        for key, value in _submission_stats.items():
            if key == "last_failure_at":
                stamps = [stamp for stamp in (stats[key], value) if stamp is not None]
                stats[key] = max(stamps) if stamps else None
            else:
                stats[key] = stats.get(key, 0) + value
    return stats


def get_persistence_stats():
    writer = _writer
    return _with_submission_stats(writer.get_persistence_stats() if writer is not None else empty_stats())


def shutdown(drain=True, timeout=2):
    global _shutdown_requested
    _validate_shutdown(drain, timeout)
    # No initialization; safe when disabled. Never hold the module lock while draining.
    with _lock:
        _shutdown_requested = True
        writer = _writer
    if writer is None:
        return dict(stopped=True, timed_out=False, unprocessed=0, stats=get_persistence_stats())
    result = writer.shutdown(drain=drain, timeout=timeout)
    result["stats"] = _with_submission_stats(result["stats"])
    return result


@atexit.register
def close_shadow():
    return shutdown(drain=True, timeout=2)
