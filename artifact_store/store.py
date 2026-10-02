"""Parsing, validation and the immutable, no-clobber publish.

``parse(kind, raw)``: UTF-8 JSON with duplicate keys rejected, then the kind's domain validator (which recomputes the
content id). Returns ``Parsed(kind, artifact_id, symbol, as_of, canonical)``, where ``canonical`` is the object's
canonical bytes. Any validator failure is ``ArtifactInvalid`` with a fixed message (validator text is never surfaced).

``publish(root, kind, source)`` stores ``canonical`` at ``<root>/<kind>/<hex>.json``:
1. validate first (nothing is written for an invalid object);
2. write a temporary file in the kind directory (exclusive create, no symlink following, mode 0444), fsync it;
3. hard-link it to the final name, which fails if the name exists (no-clobber), then remove the temporary name and
   fsync the directory. Readers never see a partial file.

If the name already holds identical bytes, the result is ``already_present`` (a valid no-op). Different bytes or a
non-regular file under the name is ``StoreConflict``: an immutable artifact is never replaced. There is no delete,
replace or purge. The root must already exist; a missing kind directory is created (never through a symlink).
"""
from dataclasses import dataclass
import json
import os
import secrets
import stat

from artifact_store import files
from artifact_store.errors import ArtifactInvalid, InvalidInput, StoreConflict, StoreUnavailable
from artifact_store.kinds import KINDS
from artifact_store.layout import MAX_ARTIFACT_BYTES, TEMP_PREFIX, TEMP_SUFFIX, file_name

PUBLISHED, ALREADY_PRESENT = "published", "already_present"


@dataclass(frozen=True)
class Parsed:
    kind: str
    artifact_id: str
    symbol: str
    as_of: str
    canonical: bytes


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def load_json(raw):
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise InvalidInput("artifact is too large")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InvalidInput("artifact is not UTF-8 JSON, or has duplicate keys") from None


def parse(kind, raw):
    if kind not in KINDS:
        raise InvalidInput("unknown artifact kind")
    spec = KINDS[kind]
    value = load_json(raw)
    try:
        data = spec.validate(value)
        artifact_id, symbol, as_of = data[spec.id_field], spec.symbol(data), spec.as_of(data)
        canonical = spec.canonical_bytes(data)
    except Exception:                         # every domain validation failure, whatever its type, fails closed
        raise ArtifactInvalid(f"object is not a valid {kind} artifact") from None
    return Parsed(kind, artifact_id, symbol, as_of, canonical)


def read_source(path):
    """An operator-supplied source file: a regular, non-symlink file, read without modification."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise InvalidInput("source file path is invalid")
    try:
        fd = os.open(path, files.FILE_FLAGS)
    except OSError:
        raise InvalidInput("source file is missing, unreadable or a symlink") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
            raise InvalidInput("source file is not a regular file of acceptable size")
        with os.fdopen(os.dup(fd), "rb") as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise InvalidInput("source file is too large")
    return raw


def _ensure_kind_dir(root_fd, kind):
    if not files.kind_present(root_fd, kind):
        try:
            os.mkdir(kind, 0o755, dir_fd=root_fd)
        except FileExistsError:
            pass
        except OSError:
            raise StoreUnavailable("cannot create the artifact kind directory") from None
        files.kind_present(root_fd, kind)


def _existing(name, kind_fd, canonical):
    try:
        current = files.read_regular(name, kind_fd, missing=FileNotFoundError)
    except FileNotFoundError:
        return None
    except ArtifactInvalid:
        raise StoreConflict("the artifact name is occupied by something else") from None
    if current != canonical:
        raise StoreConflict("a different body already exists under this artifact id")
    return ALREADY_PRESENT


def publish(root, kind, raw):
    """Validate ``raw`` as ``kind`` and store it immutably. Returns (result, Parsed)."""
    parsed = parse(kind, raw)
    name = file_name(parsed.artifact_id)
    with files.open_root(root) as root_fd:
        _ensure_kind_dir(root_fd, kind)
        with files.open_dir(kind, dir_fd=root_fd) as kind_fd:
            if _existing(name, kind_fd, parsed.canonical):
                return ALREADY_PRESENT, parsed
            temp = f"{TEMP_PREFIX}{secrets.token_hex(8)}{TEMP_SUFFIX}"
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o444,
                             dir_fd=kind_fd)
            except OSError:
                raise StoreUnavailable("cannot create a temporary file in the artifact store") from None
            try:
                try:
                    view = memoryview(parsed.canonical)
                    while view:
                        view = view[os.write(fd, view):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                try:
                    os.link(temp, name, src_dir_fd=kind_fd, dst_dir_fd=kind_fd, follow_symlinks=False)
                except FileExistsError:                    # a concurrent publisher won: identical, or a conflict
                    if _existing(name, kind_fd, parsed.canonical):
                        return ALREADY_PRESENT, parsed
                    raise StoreConflict("the artifact name is occupied by something else") from None
            except OSError:
                raise StoreUnavailable("cannot write the artifact") from None
            finally:
                try:
                    os.unlink(temp, dir_fd=kind_fd)
                except OSError:
                    pass
            try:
                os.fsync(kind_fd)
            except OSError:
                pass
    return PUBLISHED, parsed
