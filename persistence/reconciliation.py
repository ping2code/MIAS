"""Read-only macro audit: no repair, replay, scoring, or collector interaction."""
from datetime import datetime, timezone
from threading import Lock
from time import monotonic

from persistence.adapters.macro import adapt_macro, digest
from persistence.repository import _canonical
from shared.logger import get_logger

logger = get_logger("macro_reconciliation")
_log_lock = Lock()
_last_warning = float("-inf")


def _warn_mismatch():
    global _last_warning
    with _log_lock:
        now = monotonic()
        if now - _last_warning < 60:
            return
        _last_warning = now
    try:
        logger.warning("Macro shadow reconciliation mismatch")
    except Exception:
        pass


def reconcile_macro_event(event, repository, *, expect_current=True):
    """Compare expected facts/evidence/outcomes; does not mutate the event or DB.

    Current matching can be excluded from mismatches for a historical/skipped
    observation. An outcome not expected is None (not applicable), never invented.
    For a coherent PostgreSQL snapshot, callers may use a read-only repeatable-read
    audit transaction; this helper does not change isolation or own the session.
    """
    adapted = adapt_macro(event, datetime.now(timezone.utc))
    record = adapted["record"]
    result = dict(event_found=False, version_found=False, version_match=False,
                  current_version_match=False, provenance_match=False,
                  score_match=None, decision_match=None, ai_match=None, mismatches=[])
    expected_history = dict(adapted["histories"])
    for kind in expected_history:
        result[kind + "_match"] = False
    anchor = repository.find_event(record["source_family"], record["identity_version"], record["event_key"])
    if anchor is not None:
        result["event_found"] = True
        version = repository.find_version(anchor["id"], record["version_key"])
        if version is not None:
            result["version_found"] = True
            result["version_match"] = (version["content_hash"] == digest(record["normalized"]) and
                all(_canonical(version[key]) == _canonical(value) for key, value in record["normalized"].items()))
            result["current_version_match"] = anchor["current_version_id"] == version["id"]
            provenance = {row["provenance_key"]: row for row in repository.provenance(version["id"])}
            result["provenance_match"] = all(
                key in provenance and all(_canonical(provenance[key][field]) == _canonical(value)
                    for field, value in values.items() if field != "retrieved_at")
                for key, values in adapted["provenance"])
            history = {(row["kind"], row["content_hash"]): row for row in repository.history(version["id"])}
            for kind, attributes in expected_history.items():
                found = history.get((kind, digest(attributes)))
                result[kind + "_match"] = found is not None and _canonical(found["attributes"]) == _canonical(attributes)
    for key, value in result.items():
        if key == "current_version_match" and not expect_current:
            continue
        if value is False:
            result["mismatches"].append(key)
    if result["mismatches"]:
        _warn_mismatch()
    return result
