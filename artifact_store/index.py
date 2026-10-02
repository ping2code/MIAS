"""The validated, immutable in-memory index and its deterministic queries.

``build_index(root)`` walks only the closed kind directories (a missing kind directory is an empty kind; other
entries in the root are not part of the store). In each, names are processed in sorted order, so the result never
depends on directory listing order. Every entry must be:
- a ``<64 lowercase hex>.json`` regular file (not a symlink, not a directory);
- valid for its kind under its domain validator;
- named after its own content id;
- stored in exactly its canonical bytes.

Interrupted-publish temporary files (``.artifact-*.tmp``) are never visible artifacts and are ignored. Any other file,
an invalid object, a mismatch or a duplicate id fails the **whole** build (``ArtifactInvalid`` / ``StoreUnavailable``):
nothing is silently skipped.

An ``Index`` keeps only safe metadata per artifact (kind, id, symbol, sealed ``as_of``, its UTC instant, and the
SHA-256 of its bytes), never object bodies, scores or ranks.

Queries (per kind):
- ``get(id)``: exact id;
- ``latest(symbol, as_of=None)``: the artifact with the greatest sealed ``as_of`` <= ``as_of`` (or overall). Two
  distinct artifacts at that same instant are ``AmbiguousLatest``; none is chosen by id;
- ``history(symbol=None, as_of_from=None, as_of_to=None, limit, cursor)``: sealed ``as_of`` descending, then id
  ascending, inside the inclusive window, one bounded page at a time, with an opaque cursor bound to the query.
"""
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re

from artifact_store import files, store
from artifact_store.errors import AmbiguousLatest, ArtifactInvalid, InvalidQuery, NotFound, StoreUnavailable
from artifact_store.kinds import KIND_NAMES
from artifact_store.layout import FILE_NAME, checked_id, is_temp

SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")       # = trade_setup.rules.SYMBOL = market_data.models.SYMBOL
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})")
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_CURSOR_LENGTH = 512
CURSOR_VERSION = 1


def utc(value):
    """A sealed or requested instant as an aware UTC datetime (for comparison only; content is never changed)."""
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("instant has no offset")
    return parsed.astimezone(timezone.utc)


def micros(instant):
    """Exact integer microseconds since the epoch (no float rounding in ordering)."""
    return (instant - EPOCH) // timedelta(microseconds=1)


def query_instant(value):
    """A query instant: RFC 3339 with an explicit offset (``Z`` or ``+HH:MM``)."""
    if not (isinstance(value, str) and RFC3339.fullmatch(value)):
        raise InvalidQuery("instant must be RFC 3339 with an explicit offset")
    try:
        return utc(value)
    except ValueError:
        raise InvalidQuery("instant must be RFC 3339 with an explicit offset") from None


def query_symbol(value):
    if not (isinstance(value, str) and SYMBOL.fullmatch(value)):
        raise InvalidQuery("symbol is not a valid MIAS symbol")
    return value


def query_limit(value):
    if value is None:
        return DEFAULT_LIMIT
    if not (isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_LIMIT):
        raise InvalidQuery(f"limit must be from 1 to {MAX_LIMIT}")
    return value


@dataclass(frozen=True)
class Entry:
    kind: str
    artifact_id: str
    symbol: str
    as_of: str                 # the sealed text, unchanged
    instant: datetime          # its UTC instant (ordering only)
    digest: str                # SHA-256 of the stored bytes
    size: int

    @property
    def order(self):           # as_of descending, then id ascending
        return (-micros(self.instant), self.artifact_id)


def _entries(root_fd, kind):
    if not files.kind_present(root_fd, kind):
        return []
    entries = []
    with files.open_dir(kind, dir_fd=root_fd) as kind_fd:
        try:
            names = sorted(os.listdir(kind_fd))
        except OSError:
            raise StoreUnavailable("artifact kind directory is unreadable") from None
        for name in names:
            if is_temp(name):
                continue
            match = FILE_NAME.fullmatch(name)
            if not match:
                raise ArtifactInvalid("artifact store contains an unexpected file")
            raw = files.read_regular(name, kind_fd)
            parsed = store.parse(kind, raw)
            if parsed.artifact_id != f"sha256:{match.group(1)}":
                raise ArtifactInvalid("artifact file name does not match its content id")
            if raw != parsed.canonical:
                raise ArtifactInvalid("artifact file is not in canonical form")
            entries.append(Entry(kind, parsed.artifact_id, parsed.symbol, parsed.as_of, utc(parsed.as_of),
                                 hashlib.sha256(raw).hexdigest(), len(raw)))
    return entries


class Index:
    """An immutable snapshot. Built once; never modified afterwards."""

    def __init__(self, entries):
        by_kind = {kind: {} for kind in KIND_NAMES}
        seen = set()
        for entry in entries:
            if entry.artifact_id in seen:
                raise ArtifactInvalid("artifact store contains a duplicate id")
            seen.add(entry.artifact_id)
            by_kind[entry.kind][entry.artifact_id] = entry
        self._by_id = {kind: dict(items) for kind, items in by_kind.items()}
        self._ordered = {kind: tuple(sorted(items.values(), key=lambda e: e.order))
                         for kind, items in by_kind.items()}
        self._by_symbol = {}
        for kind, ordered in self._ordered.items():
            for entry in ordered:
                self._by_symbol.setdefault((kind, entry.symbol), []).append(entry)
        self._by_symbol = {key: tuple(value) for key, value in self._by_symbol.items()}

    def counts(self):
        return {kind: len(self._by_id[kind]) for kind in KIND_NAMES}

    def get(self, kind, artifact_id):
        entry = self._by_id[kind].get(checked_id(artifact_id))
        if entry is None:
            raise NotFound("artifact not found")
        return entry

    def latest(self, kind, symbol, as_of=None):
        candidates = self._by_symbol.get((kind, query_symbol(symbol)), ())
        cutoff = None if as_of is None else query_instant(as_of)
        eligible = [e for e in candidates if cutoff is None or e.instant <= cutoff]
        if not eligible:
            raise NotFound("no artifact matches")
        top = eligible[0].instant                 # ordered by as_of descending
        tied = [e for e in eligible if e.instant == top]
        if len(tied) > 1:
            raise AmbiguousLatest("more than one artifact shares the latest as_of")
        return tied[0]

    def history(self, kind, *, symbol=None, as_of_from=None, as_of_to=None, limit=None, cursor=None):
        """(entries, next_cursor or None)."""
        limit = query_limit(limit)
        source = self._ordered[kind] if symbol is None else self._by_symbol.get((kind, query_symbol(symbol)), ())
        low = None if as_of_from is None else query_instant(as_of_from)
        high = None if as_of_to is None else query_instant(as_of_to)
        if low is not None and high is not None and low > high:
            raise InvalidQuery("as_of_from is after as_of_to")
        scope = _scope(kind, symbol, as_of_from, as_of_to)
        position = None if cursor is None else decode_cursor(cursor, scope)
        page = []
        for entry in source:
            if low is not None and entry.instant < low or high is not None and entry.instant > high:
                continue
            if position is not None and entry.order <= position:
                continue
            page.append(entry)
            if len(page) > limit:
                break
        more = len(page) > limit
        page = page[:limit]
        return tuple(page), (encode_cursor(page[-1], scope) if more else None)


def _scope(kind, symbol, as_of_from, as_of_to):
    """A short fingerprint of the query a cursor belongs to (a cursor is only valid for the same query)."""
    text = json.dumps([kind, symbol, as_of_from, as_of_to], separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def encode_cursor(entry, scope):
    body = json.dumps({"v": CURSOR_VERSION, "s": scope, "t": entry.instant.isoformat(timespec="microseconds"),
                       "i": entry.artifact_id}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(body.encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(cursor, scope):
    """The (as_of descending, id) position after which the next page starts. Strict; never executes anything."""
    invalid = InvalidQuery("cursor is invalid")
    if not (isinstance(cursor, str) and 0 < len(cursor) <= MAX_CURSOR_LENGTH
            and re.fullmatch(r"[A-Za-z0-9_-]+", cursor)):
        raise invalid
    try:
        body = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        raise invalid from None
    if not (isinstance(body, dict) and set(body) == {"v", "s", "t", "i"} and body["v"] == CURSOR_VERSION
            and body["s"] == scope and isinstance(body["t"], str) and isinstance(body["i"], str)):
        raise invalid
    try:
        checked_id(body["i"])
        instant = utc(body["t"])
    except (InvalidQuery, ValueError):
        raise invalid from None
    return (-micros(instant), body["i"])


def build_index(root):
    """A fully validated Index of the store, or an exception (never a partial index)."""
    with files.open_root(root) as root_fd:
        entries = []
        for kind in KIND_NAMES:
            entries.extend(_entries(root_fd, kind))
    return Index(entries)
