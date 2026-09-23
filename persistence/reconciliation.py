"""Read-only macro/Treasury audit: no repair, replay, scoring, or collector interaction."""
from datetime import datetime, timezone
from threading import Lock
from time import monotonic

from persistence.adapters.macro import adapt_macro, digest
from persistence.adapters.treasury import adapt_treasury
from persistence.repository import _canonical
from shared.logger import get_logger

logger = get_logger("macro_reconciliation")
_log_lock = Lock()
_last_warning = {}


def _warn_mismatch(message="Macro shadow reconciliation mismatch"):
    with _log_lock:
        now = monotonic()
        if now - _last_warning.get(message, float("-inf")) < 60:
            return
        _last_warning[message] = now
    try:
        logger.warning(message)
    except Exception:
        pass


def reconcile_macro_event(event, repository, *, expect_current=True):
    """Compare expected facts/evidence/outcomes; does not mutate the event or DB.

    Current matching can be excluded from mismatches for a historical/skipped
    observation. An outcome not expected is None (not applicable), never invented.
    For a coherent PostgreSQL snapshot, callers may use a read-only repeatable-read
    audit transaction; this helper does not change isolation or own the session.
    """
    return _reconcile(adapt_macro(event, datetime.now(timezone.utc)), repository,
                      expect_current, "Macro shadow reconciliation mismatch")


def reconcile_treasury_event(event, repository, *, expect_current=True):
    """Treasury counterpart of reconcile_macro_event; identical read-only contract."""
    return _reconcile(adapt_treasury(event, datetime.now(timezone.utc)), repository,
                      expect_current, "Treasury shadow reconciliation mismatch")


def _reconcile(adapted, repository, expect_current, warning):
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
        _warn_mismatch(warning)
    return result
