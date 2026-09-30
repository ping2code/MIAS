"""Canonical JSON and content identity for OptionsSnapshot (pure; no I/O).

Same semantics as the rest of MIAS: ``sort_keys``, compact separators, ``allow_nan=False`` and ASCII escaping.
Defined locally so ``options_data`` never imports ``evidence_packet`` (a test proves the output is identical).
"""
from dataclasses import fields, is_dataclass
import hashlib
import json


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_id(body):
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def to_plain(value):
    """Dataclasses -> dicts, tuples -> lists (JSON-native values only)."""
    if is_dataclass(value):
        return {f.name: to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (tuple, list)):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    return value
