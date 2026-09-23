"""Phase 2N: audit and narrow correction of disclosure-only current-version promotions.

Before Phase 2N, geopolitical promotion treated a later disclosure time as newer
material, so a companion observed after Redis expiry could replace the current
version although its content was identical. The audit finds events whose current
version is *not* the earliest disclosure among versions with identical
substantive content (``substantively_equal``: full content for the same analyzed
document, action-level facts across companion documents) and comparable precision.

The audit is read-only. Correction is dry-run by default; ``apply=True`` changes
only ``events.current_version_id`` with a compare-and-set under the event row
lock (idempotent; concurrent changes are skipped), and returns a change report
that ``revert_disclosure_corrections`` can undo with the same guards. Versions,
provenance, history, the anchor registry, identity and Redis are never touched.
"""
import sqlalchemy as sa

from persistence.adapters.geopolitical import disclosure, same_document, substantively_equal
from persistence.models import events, event_versions

REASON = "current_not_earliest_disclosure_for_identical_content"


def _as_text(value):
    return None if value is None else str(value)


def audit_disclosure_pointers(session, *, max_events=10_000, batch_size=500):
    """Read-only. Returns flagged events with current vs earliest-candidate disclosure."""
    if type(max_events) is not int or not 1 <= max_events <= 100_000:
        raise ValueError("max_events must be between 1 and 100000")
    if type(batch_size) is not int or not 1 <= batch_size <= 5_000:
        raise ValueError("batch_size must be between 1 and 5000")
    flagged, scanned, versions_scanned, last, truncated = [], 0, 0, None, False
    while True:
        query = sa.select(events.c.id, events.c.event_key, events.c.current_version_id, events.c.first_seen_at).where(
            events.c.source_family == "geopolitical")
        if last is not None:
            query = query.where(sa.tuple_(events.c.first_seen_at, events.c.id) > sa.tuple_(
                sa.literal(last[0], type_=events.c.first_seen_at.type), sa.literal(last[1], type_=events.c.id.type)))
        rows = session.execute(query.order_by(events.c.first_seen_at, events.c.id).limit(batch_size)).all()
        if not rows:
            break
        if scanned + len(rows) > max_events:
            rows, truncated = rows[:max_events - scanned], True
        last = (rows[-1].first_seen_at, rows[-1].id)
        scanned += len(rows)
        ids = [r.id for r in rows]
        by_event = {}
        for version in session.execute(sa.select(event_versions).where(event_versions.c.event_id.in_(ids)).order_by(
                event_versions.c.recorded_at, event_versions.c.id)).mappings():
            versions_scanned += 1
            by_event.setdefault(version["event_id"], []).append(dict(version))
        for event in rows:
            versions = by_event.get(event.id, [])
            current = next((v for v in versions if v["id"] == event.current_version_id), None)
            if current is None or len(versions) < 2:
                continue
            precision, current_disclosure = disclosure(current)
            if current_disclosure is None:
                continue
            candidates = [v for v in versions if v["id"] != current["id"]
                          and substantively_equal(current, v)
                          and disclosure(v)[0] == precision and disclosure(v)[1] is not None
                          and disclosure(v)[1] < current_disclosure]
            if not candidates:
                continue
            earliest = min(candidates, key=lambda v: (disclosure(v)[1], v["recorded_at"], v["id"]))
            flagged.append(dict(
                event_id=event.event_key, event_row_id=event.id, reason=REASON, content_identical=True,
                comparison="same_document_full_content" if same_document(current, earliest) else "companion_action_facts",
                precision=precision, current_version_id=current["id"],
                current_disclosure=_as_text(current_disclosure), candidate_version_id=earliest["id"],
                candidate_disclosure=_as_text(disclosure(earliest)[1]),
                current_headline=current["headline"], candidate_headline=earliest["headline"]))
        if truncated or len(rows) < batch_size:
            break
    flagged.sort(key=lambda f: f["event_id"])
    return dict(events_scanned=scanned, versions_scanned=versions_scanned, truncated=truncated,
                flagged_count=len(flagged), flagged=flagged)


def _compare_and_set(session, event_row_id, expected, target):
    row = session.execute(sa.select(events.c.current_version_id).where(events.c.id == event_row_id)
                          .with_for_update()).first()
    if row is None:
        return "missing"
    if row.current_version_id == target:
        return "already"
    if row.current_version_id != expected:
        return "skipped_changed"
    owner = session.execute(sa.select(event_versions.c.event_id).where(event_versions.c.id == target)).scalar_one_or_none()
    if owner != event_row_id:
        return "skipped_foreign_version"  # Defensive; the composite FK would reject it anyway.
    session.execute(events.update().where(events.c.id == event_row_id).values(current_version_id=target))
    return "changed"


def correct_disclosure_pointers(session, *, apply=False, max_events=10_000):
    """Dry-run by default. With ``apply=True`` only ``current_version_id`` changes (guarded)."""
    audit = audit_disclosure_pointers(session, max_events=max_events)
    changes = []
    for item in audit["flagged"]:
        change = dict(event_id=item["event_id"], event_row_id=item["event_row_id"],
                      from_version_id=item["current_version_id"], to_version_id=item["candidate_version_id"],
                      from_disclosure=item["current_disclosure"], to_disclosure=item["candidate_disclosure"])
        change["outcome"] = (_compare_and_set(session, item["event_row_id"], item["current_version_id"],
                                              item["candidate_version_id"]) if apply else "proposed")
        changes.append(change)
    return dict(mode="apply" if apply else "dry_run", events_scanned=audit["events_scanned"],
                truncated=audit["truncated"], changes=changes,
                changed=sum(c["outcome"] == "changed" for c in changes))


def revert_disclosure_corrections(session, changes, *, apply=False):
    """Undo a previous change report with the same compare-and-set guards (dry-run by default)."""
    reverted = []
    for change in changes:
        if change.get("outcome") != "changed":
            continue
        outcome = (_compare_and_set(session, change["event_row_id"], change["to_version_id"], change["from_version_id"])
                   if apply else "proposed")
        reverted.append(dict(event_id=change["event_id"], from_version_id=change["to_version_id"],
                             to_version_id=change["from_version_id"], outcome=outcome))
    return dict(mode="apply" if apply else "dry_run", changes=reverted,
                changed=sum(c["outcome"] == "changed" for c in reverted))
