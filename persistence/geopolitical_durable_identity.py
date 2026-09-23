"""Opt-in, bounded, read-only durable identity lookup used only on a Redis alias miss.

PostgreSQL never becomes the resolver: the caller consults this only after its
own Redis alias pre-check found nothing, and any timeout, error, conflict or miss
returns ``None`` so the existing resolver proceeds exactly as before. The caller
never waits longer than the configured timeout; a lookup still running (bounded
by driver/statement timeouts) causes later lookups to skip, never to queue.
Nothing is written to PostgreSQL or Redis here.
"""
import atexit
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import datetime, timezone
import os
from threading import Lock
from time import monotonic

import sqlalchemy as sa

from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.geopolitical_registry import AnchorRegistryRepository, ROOT
from shared.logger import get_logger

logger = get_logger("geopolitical_durable_identity")
COUNTERS = ("lookup_attempted", "lookup_hit", "lookup_miss", "lookup_timeout", "lookup_error",
            "lookup_conflict", "lookup_skipped_busy", "redis_hit_bypass",
            "registry_inserted", "registry_existing", "registry_conflict", "registry_error")
_stats_lock = Lock()
_stats = dict.fromkeys(COUNTERS, 0) | dict(last_error_at=None, last_conflict_at=None)
_last_log = {}


def _count(name, amount=1, stamp=None):
    with _stats_lock:
        _stats[name] += amount
        if stamp:
            _stats[stamp] = datetime.now(timezone.utc).isoformat()


def _log(kind, message, *args):
    # Bounded: at most one line per kind per minute; never URLs, credentials or payloads.
    with _stats_lock:
        now = monotonic()
        if now - _last_log.get(kind, float("-inf")) < 60:
            return
        _last_log[kind] = now
    try:
        logger.warning(message, *args)
    except Exception:
        pass


def get_durable_identity_stats():
    with _stats_lock:
        return dict(_stats)


# Collector-side visibility (Phase 2J): one deterministic key=value line, no
# anchors, URLs, payloads or credentials. Counters are process-local and reset
# on restart; durable truth is the PostgreSQL history and audit.
STATS_LOG_FIELDS = ("lookup_attempted", "lookup_hit", "lookup_miss", "lookup_timeout", "lookup_error",
                    "lookup_conflict", "lookup_skipped_busy", "redis_hit_bypass",
                    "registry_inserted", "registry_existing", "registry_conflict", "registry_error")
stats_logger = get_logger("geopolitical_identity_stats")
_stats_log_last = None


def format_stats_line(stats=None, *, lookup_enabled):
    stats = get_durable_identity_stats() if stats is None else stats
    fields = ["event=geopolitical_identity_stats", "scope=process",
              "lookup_enabled=" + ("true" if lookup_enabled else "false")]
    fields.extend(f"{name}={int(stats.get(name, 0))}" for name in STATS_LOG_FIELDS)
    return " ".join(fields)


def maybe_log_stats(*, interval_seconds, lookup_enabled, now=None):
    """Emit at most one line per interval; called by the collector after a cycle.

    No thread or timer: nothing happens between collection cycles. Returns the
    emitted line (or None) for tests; logging failures are swallowed.
    """
    global _stats_log_last
    now = monotonic() if now is None else now
    with _stats_lock:
        if _stats_log_last is not None and now - _stats_log_last < interval_seconds:
            return None
        _stats_log_last = now
    line = format_stats_line(lookup_enabled=lookup_enabled)
    try:
        stats_logger.info(line)
    except Exception:
        pass
    return line


def record_registry(counts):
    """Called by the shadow writer after a registry write (inside its own transaction)."""
    _count("registry_inserted", counts.get("inserted", 0))
    _count("registry_existing", counts.get("already_present", 0))
    if counts.get("conflicts"):
        _count("registry_conflict", counts["conflicts"], "last_conflict_at")
        _log("registry_conflict", "Geopolitical anchor registry conflict recorded; existing root preserved")


def record_registry_error():
    _count("registry_error", stamp="last_error_at")
    _log("registry_error", "Geopolitical anchor registry write failed; event history unaffected")


def runtime_engine(timeout_ms):
    settings = DatabaseSettings.from_env()
    if settings.backend != "postgresql":
        raise ValueError("Durable identity lookup requires PostgreSQL")
    # libpq's smallest effective connect timeout is 2 s; the caller's wait is
    # bounded separately by the future timeout below.
    return make_engine(replace(settings, pool_size=1, max_overflow=0, pool_timeout_seconds=1,
                               connect_timeout_seconds=2, statement_timeout_ms=timeout_ms,
                               lock_timeout_ms=timeout_ms, application_name="mias_geopolitical_identity"))


class DurableIdentityLookup:
    def __init__(self, timeout_ms=250, engine_factory=None):
        if type(timeout_ms) is not int or not 10 <= timeout_ms <= 5000:
            raise ValueError("Durable identity timeout must be 10-5000 ms")
        self.timeout_ms = timeout_ms
        self.engine_factory = engine_factory or (lambda: runtime_engine(timeout_ms))
        self._engine = None
        self._pending = None
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mias-geo-durable-identity")

    def redis_hit(self):
        _count("redis_hit_bypass")

    def _query(self, anchors, stage):
        try:
            if self._engine is None:
                self._engine = self.engine_factory()
            with transaction(self._engine) as session:
                if self._engine.dialect.name == "postgresql":
                    session.execute(sa.text("SET TRANSACTION READ ONLY"))
                return AnchorRegistryRepository(session).lookup(anchors=anchors, stage=stage)
        except Exception:
            engine, self._engine = self._engine, None
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    pass
            raise

    def lookup(self, anchors, stage):
        """Return a durable root only for exactly one consistent active match; else None."""
        _count("lookup_attempted")
        with self._lock:
            if self._pending is not None and not self._pending.done():
                _count("lookup_skipped_busy")
                return None
            try:
                future = self._pending = self._executor.submit(self._query, list(anchors), tuple(stage))
            except RuntimeError:  # Executor shut down at exit.
                _count("lookup_error", stamp="last_error_at")
                return None
        try:
            result = future.result(timeout=self.timeout_ms / 1000)
        except FutureTimeout:
            _count("lookup_timeout", stamp="last_error_at")
            _log("timeout", "Geopolitical durable identity lookup timed out; existing resolver used")
            return None
        except Exception as error:
            _count("lookup_error", stamp="last_error_at")
            _log("error", "Geopolitical durable identity lookup failed (%s); existing resolver used", type(error).__name__)
            return None
        if result["status"] == "hit" and ROOT.fullmatch(result["policy_id"] or ""):
            _count("lookup_hit")
            return result["policy_id"]
        if result["status"] == "conflict":
            _count("lookup_conflict", stamp="last_conflict_at")
            _log("conflict", "Geopolitical durable identity conflict on %d anchor(s); durable root not applied",
                 len(result["anchors"]))
            return None
        _count("lookup_miss")
        return None

    def close(self):
        self._executor.shutdown(wait=False, cancel_futures=True)
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


_lookup = None
_lookup_lock = Lock()


def get_durable_lookup(timeout_ms):
    """Lazy process singleton; creates no engine until the first Redis-miss lookup."""
    global _lookup
    with _lookup_lock:
        if _lookup is None:
            _lookup = DurableIdentityLookup(timeout_ms=timeout_ms)
        return _lookup


def _reset():
    """Fresh process-local state (fork child / simulated restart); never touches PostgreSQL."""
    global _lookup, _lookup_lock, _stats_lock, _stats, _stats_log_last
    _lookup, _lookup_lock, _stats_lock = None, Lock(), Lock()
    _stats = dict.fromkeys(COUNTERS, 0) | dict(last_error_at=None, last_conflict_at=None)
    _stats_log_last = None
    _last_log.clear()  # Warning rate limits are process-local too.


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset)


@atexit.register
def _close():
    if _lookup is not None:
        _lookup.close()
