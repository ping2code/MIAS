"""Canonical serialization and content identity for EvidenceSynthesis (same semantics as ``evidence_packet``).

- Canonical JSON: ``sort_keys=True``, compact separators, ``allow_nan=False`` (from
  ``evidence_packet.serialization``).
- ``synthesis_id = "sha256:" + SHA-256(canonical JSON of the body without synthesis_id)``.
- Model values are JSON-native (str, int, bool, None); tuples become arrays in their fixed canonical order.
"""
from dataclasses import fields, is_dataclass

from evidence_packet.serialization import canonical_json, content_id

__all__ = ("canonical_json", "content_id", "to_plain")


def to_plain(value):
    if is_dataclass(value):
        return {f.name: to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [to_plain(item) for item in value]
    return value
