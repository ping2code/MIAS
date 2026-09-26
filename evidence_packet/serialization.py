"""Canonical JSON, content-addressed identity and deep freezing for the evidence packet (Phase 7C v1).

**Canonical JSON:** ``json.dumps(sort_keys=True, separators=(",", ":"), allow_nan=False)``.

- ``Decimal`` uses ``format_decimal`` (the MIAS canonical text).
- Packet-level datetimes use UTC ISO 8601.
- Dates use ISO.
- Floats and ``None`` are emitted as-is.
- Tuples become JSON arrays, in the semantic order fixed by the assembler.
- Read-only mappings become objects.

``MarketContext`` is serialized only through its own ``to_dict()``, and
technical rows keep their durable ``snapshot_row`` form.

**Identity:** ``packet_id = "sha256:" + sha256(canonical JSON of the packet body
without packet_id)``. There is no randomness and no wall-clock field.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from types import MappingProxyType

from market_data.models import format_decimal


def freeze(value):
    """Deep, private, immutable copy: mappings become read-only proxies over new dicts; lists and tuples become tuples."""
    if isinstance(value, (dict, MappingProxyType)):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value):
    """Plain JSON-compatible structure for a frozen value: read-only proxies become dicts, tuples become lists."""
    if isinstance(value, MappingProxyType):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def utc_iso(value):
    if value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc).isoformat()


def plain(value):
    """Canonical JSON-compatible value for packet fields (never used on MarketContext, which has its own to_dict)."""
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (MappingProxyType, dict)):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_id(body):
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
