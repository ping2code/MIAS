"""Symlink-proof file access under the store root.

Every access walks root, kind directory and file with ``O_NOFOLLOW`` directory file descriptors, so a symlink at any
store-controlled level (the root's last component, a kind directory, an artifact file) is refused, and a path can't
escape the root. Artifact files must be regular files no larger than ``MAX_ARTIFACT_BYTES``.
"""
from contextlib import contextmanager
import os
import stat

from artifact_store.errors import ArtifactInvalid, StoreUnavailable
from artifact_store.layout import MAX_ARTIFACT_BYTES, checked_root

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)


@contextmanager
def open_dir(name, dir_fd=None):
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    except OSError:
        raise StoreUnavailable("artifact store directory is missing, unreadable or a symlink") from None
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_root(root):
    with open_dir(checked_root(root)) as fd:
        yield fd


def kind_present(root_fd, kind):
    """True if the kind directory exists as a real directory, False if absent; unsafe entries fail closed."""
    try:
        info = os.lstat(kind, dir_fd=root_fd)
    except FileNotFoundError:
        return False
    except OSError:
        raise StoreUnavailable("artifact kind directory is unreadable") from None
    if not stat.S_ISDIR(info.st_mode):
        raise StoreUnavailable("artifact kind directory is not a directory")
    return True


def read_regular(name, dir_fd, *, missing=ArtifactInvalid, max_bytes=MAX_ARTIFACT_BYTES):
    """The bytes of a regular, non-symlink file (bounded)."""
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise missing("artifact file is missing") from None
    except OSError:
        raise ArtifactInvalid("artifact file is unreadable or a symlink") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactInvalid("artifact file is not a regular file")
        if info.st_size > max_bytes:
            raise ArtifactInvalid("artifact file is too large")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ArtifactInvalid("artifact file is too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
