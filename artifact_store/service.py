"""``ArtifactStore``: the process-level holder of the current index snapshot, plus verified byte reads.

- ``refresh()`` builds a complete new ``Index`` and swaps it in with one assignment. Readers always see either the
  previous snapshot or the new one, never a partial build. A failed refresh keeps the previous snapshot for reads and
  marks the store not healthy (``healthy`` is False) until a later refresh succeeds. Refreshes are serialized.
- ``snapshot()`` returns the current index, or raises ``StoreUnavailable`` before the first successful build.
- ``read_bytes(entry)`` re-reads an artifact through the symlink-proof walk and returns its bytes only if their
  SHA-256 still equals the digest recorded at indexing; a changed or vanished file is ``ArtifactInvalid``.

Constructing an ``ArtifactStore`` performs no I/O. There is no watcher, Redis pointer or database catalog.
"""
import hashlib
import threading

from artifact_store import files
from artifact_store.errors import ArtifactInvalid, ArtifactStoreError, StoreUnavailable
from artifact_store.index import build_index
from artifact_store.layout import checked_root, file_name


class ArtifactStore:
    def __init__(self, root):
        self.root = checked_root(root)
        self._index = None
        self._healthy = False
        self._lock = threading.Lock()

    @property
    def healthy(self):
        return self._index is not None and self._healthy

    def snapshot(self):
        index = self._index
        if index is None:
            raise StoreUnavailable("artifact index is not built")
        return index

    def refresh(self):
        """Rebuild and swap. Returns True on success; on failure keeps the previous snapshot and returns False."""
        with self._lock:
            try:
                index = build_index(self.root)
            except ArtifactStoreError:
                self._healthy = False
                return False
            self._index, self._healthy = index, True
            return True

    def read_bytes(self, entry):
        with files.open_root(self.root) as root_fd:
            try:
                with files.open_dir(entry.kind, dir_fd=root_fd) as kind_fd:
                    raw = files.read_regular(file_name(entry.artifact_id), kind_fd)
            except StoreUnavailable:
                raise ArtifactInvalid("artifact kind directory changed after indexing") from None
        if len(raw) != entry.size or hashlib.sha256(raw).hexdigest() != entry.digest:
            raise ArtifactInvalid("artifact file changed after indexing")
        return raw
