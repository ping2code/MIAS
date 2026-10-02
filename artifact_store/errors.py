"""Artifact store errors. Messages are safe to show: they never contain paths, file contents or validator text."""


class ArtifactStoreError(Exception):
    """Base class."""


class InvalidInput(ArtifactStoreError):
    """Unreadable, non-JSON, duplicate-key or oversized input, or an unknown kind."""


class ArtifactInvalid(ArtifactStoreError):
    """An object failed its domain validation, is not canonical, or a stored file no longer matches its index."""


class StoreUnavailable(ArtifactStoreError):
    """The store root, a kind directory, or the index is missing, unsafe or not built."""


class StoreConflict(ArtifactStoreError):
    """A different body (or a non-regular file) already occupies an artifact's immutable name."""


class NotFound(ArtifactStoreError):
    """No artifact matches."""


class AmbiguousLatest(ArtifactStoreError):
    """More than one distinct artifact shares the latest sealed as_of."""


class InvalidQuery(ArtifactStoreError):
    """A malformed id, symbol, instant, window, limit or cursor."""
