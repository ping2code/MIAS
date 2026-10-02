"""Glue between HTTP and the framework-free artifact store: store lifecycle, query parsing and error mapping.

- The store is built from ``MIAS_ARTIFACT_ROOT`` (no I/O at construction). The app lifespan runs a first
  ``refresh()`` at startup, then a background thread refreshes every ``REFRESH_INTERVAL_SECONDS``. Each refresh
  builds a complete new index and swaps it in; a failed refresh keeps the previous snapshot and fails readiness.
- Query strings are strict: only the documented parameters, each at most once, else ``invalid_request``.
- Store errors map to the closed codes: ``InvalidQuery`` → ``invalid_request``, ``NotFound`` → ``not_found``,
  ``AmbiguousLatest`` → ``ambiguous_latest``, ``ArtifactInvalid`` → ``artifact_invalid``, ``StoreUnavailable`` →
  ``dependency_unavailable``. Store messages are never put in responses.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import threading

from artifact_store.errors import (AmbiguousLatest, ArtifactInvalid, ArtifactStoreError, InvalidQuery, NotFound,
                                   StoreUnavailable)
from api.errors import ApiError

REFRESH_INTERVAL_SECONDS = 30
API_VERSION = "v1"
ERROR_CODES = ((InvalidQuery, "invalid_request"), (NotFound, "not_found"), (AmbiguousLatest, "ambiguous_latest"),
               (ArtifactInvalid, "artifact_invalid"), (StoreUnavailable, "dependency_unavailable"))


@contextmanager
def store_errors():
    try:
        yield
    except ArtifactStoreError as error:
        code = next((c for e, c in ERROR_CODES if isinstance(error, e)), "internal")
        raise ApiError(code) from None


def query(request, allowed, required=()):
    """The query parameters as a dict of single strings; unknown, repeated or missing required ones are invalid."""
    items = request.query_params.multi_items()
    names = [k for k, _ in items]
    if len(names) != len(set(names)) or not set(names) <= set(allowed) or not set(required) <= set(names):
        raise ApiError("invalid_request")
    return dict(items)


def limit_param(value):
    if value is None:
        return None
    if not (value.isascii() and value.isdigit() and len(value) <= 4):
        raise ApiError("invalid_request")
    return int(value)


def served_at(request):
    return request.app.state.clock().astimezone(timezone.utc).isoformat(timespec="seconds")


def utc_now():
    return datetime.now(timezone.utc)


def store_for(request):
    store = request.app.state.artifact_store
    if store is None:
        raise ApiError("dependency_unavailable")
    return store


class Refresher:
    """Periodic background refresh (daemon thread); stopped at shutdown. No filesystem watcher."""

    def __init__(self, store, interval):
        self.store, self.interval = store, interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.store.refresh()
        if self.interval and self.interval > 0:
            self._thread = threading.Thread(target=self._run, name="mias-artifact-refresh", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            self.store.refresh()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
