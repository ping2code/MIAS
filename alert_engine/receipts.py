"""Durable delivery receipts for Phase 12E (``phase12-receipt-v1``): one append-only file per provider send.

A receipt is **operational evidence, not a canonical object**: it records the runtime outcome of one adapter send for
one accepted AlertEvent on one channel. It never alters an AlertEvent, its ``alert_id``, or Phase 12C state, and
Phase 12C state is never rebuilt from receipts.

Fields (closed set): ``receipt_format_version``, ``alert_id``, ``channel``, ``sequence``, ``status`` (``delivered`` |
``failed``), ``provider_message_id`` (delivered only), ``attempts``, ``safe_error_code`` (failed only, from the closed
Phase 12D codes), ``attempted_at`` and ``completed_at`` (runtime UTC; never fed back into any decision),
``delivery_contract_version`` and ``render_version``. There is no token, chat id, URL, header, raw response,
exception text or rendered payload.

Files live in an operator-provided directory, named deterministically
``<channel>-<alert hex>-<sequence:06d>.json``. ``sequence`` is 1, 2, ... per (alert, channel) and is also inside the
file. A receipt is written to a temporary file, fsynced, then hard-linked into place (no-clobber), so it's atomic and
never overwritten; a concurrent writer that loses the race takes the next sequence. Correctness never depends on
directory listing order: receipts are parsed, then ordered by their sequence numbers, and a gap, a duplicate, a name
that disagrees with its content, or an unknown file fails closed.

``delivered_markers(receipts)`` derives the Phase 12D delivered markers that receipts prove: for each (alert, channel)
with at least one ``delivered`` receipt, every message id it was delivered under (normally one; more only after an
at-least-once duplicate send).
"""
from datetime import datetime
import json
import os
import re
import tempfile

from alert_engine.canonical import canonical_json
from alert_engine.delivery.base import (CHANNELS, DELIVERED, DELIVERY_CONTRACT_VERSION, ERROR_CODES, FAILED,
                                        DeliveryRequest, DeliveryResult)
from alert_engine.rules import CONTENT_ID

RECEIPT_FORMAT_VERSION = "phase12-receipt-v1"
RECEIPT_FIELDS = ("receipt_format_version", "alert_id", "channel", "sequence", "status", "provider_message_id",
                  "attempts", "safe_error_code", "attempted_at", "completed_at", "delivery_contract_version",
                  "render_version")
NAME = re.compile(r"(?P<channel>[a-z]+)-(?P<hex>[0-9a-f]{64})-(?P<sequence>[0-9]{6})\.json")
TEMP_PREFIX = ".receipt-"
UTC_INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?\+00:00")
MAX_SEQUENCE = 999999
MAX_WRITE_RACES = 5


class ReceiptError(Exception):
    """A receipt could not be written, or the receipt history is malformed (fail closed)."""


def receipt_name(alert_id, channel, sequence):
    return f"{channel}-{alert_id.split(':', 1)[1]}-{sequence:06d}.json"


def make_receipt(request, result, sequence, attempted_at, completed_at):
    """The receipt dict for one adapter send. Only closed, secret-free fields are copied."""
    if not (isinstance(request, DeliveryRequest) and isinstance(result, DeliveryResult)):
        raise ReceiptError("receipt needs a DeliveryRequest and a DeliveryResult")
    receipt = dict(receipt_format_version=RECEIPT_FORMAT_VERSION, alert_id=request.alert_id, channel=request.channel,
                   sequence=sequence, status=result.status, provider_message_id=result.provider_message_id,
                   attempts=result.attempts, safe_error_code=result.error_code, attempted_at=attempted_at,
                   completed_at=completed_at, delivery_contract_version=DELIVERY_CONTRACT_VERSION,
                   render_version=request.render_version)
    return validated_receipt(receipt)


def validated_receipt(receipt):
    """A receipt dict, validated structurally (fail closed)."""
    ok = isinstance(receipt, dict) and set(receipt) == set(RECEIPT_FIELDS)
    if ok:
        r = receipt
        integer = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
        ok = (r["receipt_format_version"] == RECEIPT_FORMAT_VERSION
              and isinstance(r["alert_id"], str) and bool(CONTENT_ID.fullmatch(r["alert_id"]))
              and r["channel"] in CHANNELS and integer(r["sequence"]) and 1 <= r["sequence"] <= MAX_SEQUENCE
              and integer(r["attempts"]) and r["attempts"] >= 1
              and r["delivery_contract_version"] == DELIVERY_CONTRACT_VERSION
              and isinstance(r["render_version"], str) and bool(r["render_version"])
              and all(isinstance(r[k], str) and UTC_INSTANT.fullmatch(r[k]) for k in ("attempted_at", "completed_at"))
              and datetime.fromisoformat(r["attempted_at"]) <= datetime.fromisoformat(r["completed_at"]))
        if ok and r["status"] == DELIVERED:
            ok = (isinstance(r["provider_message_id"], str) and bool(r["provider_message_id"])
                  and r["safe_error_code"] is None)
        elif ok:
            ok = r["status"] == FAILED and r["provider_message_id"] is None and r["safe_error_code"] in ERROR_CODES
    if not ok:
        raise ReceiptError("delivery receipt is malformed")
    return receipt


def _sequences(directory, alert_id, channel):
    prefix = f"{channel}-{alert_id.split(':', 1)[1]}-"
    found = []
    for name in os.listdir(directory):
        match = NAME.fullmatch(name)
        if match and name.startswith(prefix):
            found.append(int(match["sequence"]))
    return found


def append_receipt(directory, request, result, attempted_at, completed_at):
    """Atomically write the next receipt for (alert, channel); never overwrites. Returns (path, receipt)."""
    try:
        if not os.path.isdir(directory):
            raise ReceiptError("receipt directory does not exist")
        for _ in range(MAX_WRITE_RACES):
            sequence = max(_sequences(directory, request.alert_id, request.channel), default=0) + 1
            if sequence > MAX_SEQUENCE:
                raise ReceiptError("receipt sequence exhausted")
            receipt = make_receipt(request, result, sequence, attempted_at, completed_at)
            path = os.path.join(directory, receipt_name(request.alert_id, request.channel, sequence))
            fd, temp = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(canonical_json(receipt) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temp, path)
                except FileExistsError:
                    continue                      # a concurrent writer took this sequence; take the next one
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            _fsync_directory(directory)
            return path, receipt
        raise ReceiptError("receipt sequence kept racing; not written")
    except OSError:
        raise ReceiptError("cannot write the delivery receipt") from None


def _fsync_directory(directory):
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def read_receipts(directory):
    """Every receipt in the directory, validated and ordered by (alert_id, channel, sequence). Fails closed on any
    unknown file, unreadable or malformed receipt, name/content mismatch, or a sequence gap."""
    try:
        names = os.listdir(directory)
    except OSError:
        raise ReceiptError("receipt directory is unreadable") from None
    receipts = []
    for name in names:
        if name.startswith(TEMP_PREFIX) and name.endswith(".tmp"):
            continue                              # an interrupted write's leftover; never a receipt
        match = NAME.fullmatch(name)
        if not match:
            raise ReceiptError("receipt directory contains an unknown file")
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                receipt = json.load(handle, object_pairs_hook=_no_duplicate_keys)
        except (OSError, UnicodeDecodeError, ValueError):
            raise ReceiptError("a delivery receipt is unreadable, not JSON, or has duplicate keys") from None
        validated_receipt(receipt)
        if name != receipt_name(receipt["alert_id"], receipt["channel"], receipt["sequence"]):
            raise ReceiptError("a delivery receipt's name does not match its content")
        receipts.append(receipt)
    receipts.sort(key=lambda r: (r["alert_id"], r["channel"], r["sequence"]))
    groups = {}
    for receipt in receipts:
        groups.setdefault((receipt["alert_id"], receipt["channel"]), []).append(receipt["sequence"])
    if any(seqs != list(range(1, len(seqs) + 1)) for seqs in groups.values()):
        raise ReceiptError("delivery receipt history has a sequence gap")
    return receipts


def delivered_markers(receipts):
    """{(alert_id, channel): (message ids in sequence order)} for every pair with a delivered receipt."""
    markers = {}
    for receipt in receipts:
        if receipt["status"] == DELIVERED:
            markers.setdefault((receipt["alert_id"], receipt["channel"]), []).append(receipt["provider_message_id"])
    return {key: tuple(ids) for key, ids in sorted(markers.items())}
