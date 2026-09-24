"""Read-only macro/Treasury/geopolitical/Fed/SEC audit: no repair, replay, scoring, or collector interaction."""
from datetime import datetime, timezone
from threading import Lock
from time import monotonic

from persistence.adapters.macro import adapt_macro, digest
from persistence.adapters.fed import adapt_fed
from persistence.adapters.geopolitical import adapt_geopolitical, RELEVANCE
from persistence.adapters.sec import adapt_sec
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


def reconcile_fed_event(event, repository, *, expect_current=True):
    """Fed counterpart; ``event`` is the submitted snapshot (with ``fed_fingerprint``)."""
    return _reconcile(adapt_fed(event, datetime.now(timezone.utc)), repository,
                      expect_current, "Fed shadow reconciliation mismatch")


def reconcile_geopolitical_event(event, repository, *, expect_current=True):
    """Geopolitical counterpart; adds explicit relevance and document->policy checks.

    Never consults or repairs Redis alias/policy state: the stored history is
    compared with the collector-resolved event supplied by the caller.
    """
    adapted = adapt_geopolitical(event, datetime.now(timezone.utc))

    def extra(version, provenance):
        expected = {key: event[key] for key in RELEVANCE if key in event}
        relevance = all(_canonical(version["attributes"].get(key)) == _canonical(value)
                        for key, value in expected.items())
        documents = {values["document_id"] for _, values in adapted["provenance"]}
        linked = {row["document_id"] for row in provenance
                  if row["attributes"].get("relation") == "policy_document"
                  and row["attributes"].get("policy_id") == event["policy_id"]}
        return dict(relevance_match=relevance, relationship_match=documents <= linked)

    return _reconcile(adapted, repository, expect_current,
                      "Geopolitical shadow reconciliation mismatch", extra)


def reconcile_sec_event(event, repository, *, expect_current=True):
    """SEC counterpart; ``event`` is the submitted snapshot (with ``sec_fingerprint``).

    Adds an explicit accession check: the stored version and its provenance must
    carry the event's accession number. SEC has no AI path, so ``ai_match`` stays
    None (not applicable).
    """
    adapted = adapt_sec(event, datetime.now(timezone.utc))
    accession = event["accession_number"]

    def extra(version, provenance):
        return dict(accession_match=version["attributes"].get("accession_number") == accession
                    and any(row["document_id"] == accession for row in provenance))

    return _reconcile(adapted, repository, expect_current, "SEC shadow reconciliation mismatch",
                      extra, extra_keys=("accession_match",))


def _reconcile(adapted, repository, expect_current, warning, extra=None,
               extra_keys=("relevance_match", "relationship_match")):
    record = adapted["record"]
    result = dict(event_found=False, version_found=False, version_match=False,
                  current_version_match=False, provenance_match=False,
                  score_match=None, decision_match=None, ai_match=None, mismatches=[])
    if extra is not None:
        result.update(dict.fromkeys(extra_keys, False))
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
            rows = repository.provenance(version["id"])
            provenance = {row["provenance_key"]: row for row in rows}
            if extra is not None:
                result.update(extra(version, rows))
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
