"""Canonical JSON, content identity and Decimal text for Setup Evaluation (local copies; standard library only).

The same semantics as the rest of MIAS: ``sort_keys``, compact separators, ``allow_nan=False``, ASCII escaping, and
canonical Decimal text (no exponent, no trailing zeros). Tests prove these are identical to the upstream helpers, so
setup_evaluation never imports options_data, market_data or the evidence layers.
"""
from dataclasses import fields, is_dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_id(body):
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def to_plain(value):
    if is_dataclass(value):
        return {f.name: to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (tuple, list)):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    return value


def format_decimal(value):
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "0") else text


def canonical_decimal(value):
    """The Decimal for a canonical Decimal string, or None if ``value`` is not one."""
    if not isinstance(value, str):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() and format_decimal(number) == value else None
