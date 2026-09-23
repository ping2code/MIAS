"""Read-only audit of durable geopolitical history for possible identity divergence.

After Redis alias/policy expiry the collector can resolve the same official action
to a new root, which persistence records as a second logical event. This audit
finds persisted events that share deterministic, already-persisted identifiers.
It never merges, repairs, writes, consults Redis, or uses text similarity/AI.

Classifications (strongest first):

- ``exact_authoritative_anchor``: an official instrument anchor (FR number, EO,
  OFAC notice, FTC case, MOEA release) shared by events with the same identity
  stage (family, stage, revision). The collector would have resolved these to
  one root had its alias state been present: historical root divergence.
- ``shared_policy_id``: the same resolved policy root on different events.
- ``shared_document_id``: the same official document ID on events of the same
  stage. Publishers can reuse a native ID/URL for a new instrument, so this is a
  possible divergence, not proof.
- ``informational_only``: shared anchor/document across *different* stages
  (proposal/final/amendment are intentionally distinct identities) or a shared
  canonical URL only.

Anchor and document matches are evaluated per identity stage (Phase 2I), so a
later legitimate event of another stage can never mask an existing same-stage
divergence group; cross-stage sharing is reported separately as informational.
"""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json

import sqlalchemy as sa

from persistence.models import events, event_versions, event_provenance

RANK = ("exact_authoritative_anchor", "shared_policy_id", "shared_document_id", "informational_only")
CHUNK = 500
FIELDS = {"anchor": "shared_anchors", "policy": "shared_policy_ids", "document": "shared_document_ids", "url": "shared_urls"}


def _chunks(values):
    values = list(values)
    for start in range(0, len(values), CHUNK):
        yield values[start:start + CHUNK]


def _group_key(event_ids, classification):
    return "geo-divergence:" + hashlib.sha256(json.dumps([classification, event_ids]).encode()).hexdigest()[:24]


def _after(last):
    # Bind with the columns' own types so the keyset compares like stored values on every dialect.
    return sa.tuple_(events.c.first_seen_at, events.c.id) > sa.tuple_(
        sa.literal(last[0], type_=events.c.first_seen_at.type), sa.literal(last[1], type_=events.c.id.type))


def _scan(session, max_events, batch_size):
    """Keyset-paged scan: no SELECT returns more than ``batch_size`` event rows."""
    facts, last, scanned, truncated = {}, None, 0, False
    versions_scanned = provenance_scanned = 0
    while True:
        query = sa.select(events.c.id, events.c.event_key, events.c.first_seen_at).where(
            events.c.source_family == "geopolitical")
        if last is not None:
            query = query.where(_after(last))
        rows = session.execute(query.order_by(events.c.first_seen_at, events.c.id).limit(batch_size)).all()
        if not rows:
            break
        if scanned + len(rows) > max_events:
            rows, truncated = rows[:max_events - scanned], True
        if last is not None and rows and (rows[-1].first_seen_at, rows[-1].id) <= last:
            raise RuntimeError("Audit keyset scan did not advance")
        last = (rows[-1].first_seen_at, rows[-1].id) if rows else last
        scanned += len(rows)
        key_by_id = {row.id: row.event_key for row in rows}
        for key in key_by_id.values():
            facts[key] = dict(policy_ids=set(), anchors=set(), documents=set(), urls=set(), stages=set())
        version_owner = {}
        for chunk in _chunks(key_by_id):
            for version in session.execute(sa.select(
                    event_versions.c.id, event_versions.c.event_id, event_versions.c.event_type,
                    event_versions.c.stage, event_versions.c.revision_key, event_versions.c.attributes)
                    .where(event_versions.c.event_id.in_(chunk))).mappings():
                versions_scanned += 1
                entry = facts[key_by_id[version["event_id"]]]
                version_owner[version["id"]] = key_by_id[version["event_id"]]
                attrs = version["attributes"] or {}
                entry["stages"].add((version["event_type"], version["stage"], version["revision_key"]))
                if attrs.get("policy_id"):
                    entry["policy_ids"].add(attrs["policy_id"])
                entry["anchors"].update(a for a in attrs.get("identity_anchors") or [] if isinstance(a, str))
                if attrs.get("document_id"):
                    entry["documents"].add(attrs["document_id"])
        for chunk in _chunks(version_owner):
            for provenance in session.execute(sa.select(
                    event_provenance.c.event_version_id, event_provenance.c.document_id,
                    event_provenance.c.canonical_url).where(event_provenance.c.event_version_id.in_(chunk))).mappings():
                provenance_scanned += 1
                entry = facts[version_owner[provenance["event_version_id"]]]
                if provenance["document_id"]:
                    entry["documents"].add(provenance["document_id"])
                entry["urls"].add(provenance["canonical_url"])
        if truncated:
            break
        if len(rows) < batch_size:
            break
        if scanned == max_events:
            # Probe one more row so truncation is reported exactly as before.
            probe = session.execute(sa.select(events.c.id).where(
                events.c.source_family == "geopolitical", _after(last)).limit(1)).first()
            truncated = probe is not None
            break
    return facts, scanned, versions_scanned, provenance_scanned, truncated


def _groups(facts):
    index = defaultdict(set)
    for event_key, entry in facts.items():
        for stage in entry["stages"]:
            for anchor in entry["anchors"]:
                index[("anchor_stage", anchor, stage)].add(event_key)
            for document in entry["documents"]:
                index[("document_stage", document, stage)].add(event_key)
        for anchor in entry["anchors"]:
            index[("anchor", anchor, None)].add(event_key)
        for document in entry["documents"]:
            index[("document", document, None)].add(event_key)
        for policy in entry["policy_ids"]:
            index[("policy", policy, None)].add(event_key)
        for url in entry["urls"]:
            index[("url", url, None)].add(event_key)
    groups = {}
    for (kind, value, _stage), members in index.items():
        if len(members) < 2:
            continue
        if kind in ("anchor", "document"):
            # Cross-stage sharing only; same-stage sharing is the stage-scoped kinds.
            if set.intersection(*(facts[m]["stages"] for m in members)):
                continue
            classification = "informational_only"
            reason = "shared_anchor_distinct_stages" if kind == "anchor" else "shared_document_id_distinct_stages"
        elif kind == "anchor_stage":
            kind, classification, reason = "anchor", "exact_authoritative_anchor", "shared_authoritative_anchor_same_stage"
        elif kind == "document_stage":
            kind, classification, reason = "document", "shared_document_id", "shared_document_id_same_stage"
        elif kind == "policy":
            classification, reason = "shared_policy_id", "shared_policy_root"
        else:
            classification, reason = "informational_only", "shared_canonical_url"
        group = groups.setdefault(tuple(sorted(members)), dict(
            classifications=set(), reasons=set(), shared_anchors=set(), shared_policy_ids=set(),
            shared_document_ids=set(), shared_urls=set()))
        group["classifications"].add(classification)
        group["reasons"].add(reason)
        group[FIELDS[kind]].add(value)
    report = []
    for member_ids, group in groups.items():
        classification = min(group["classifications"], key=RANK.index)
        members = [facts[m] for m in member_ids]
        report.append(dict(
            group_key=_group_key(list(member_ids), classification),
            classification=classification,
            reasons=sorted(group["reasons"]),
            event_ids=list(member_ids),
            policy_ids=sorted(set().union(*(m["policy_ids"] for m in members))),
            stages=[dict(event_type=family, stage=stage, revision=revision) for family, stage, revision in
                    sorted({s for m in members for s in m["stages"]}, key=lambda s: tuple(v or "" for v in s))],
            shared_anchors=sorted(group["shared_anchors"]),
            shared_policy_ids=sorted(group["shared_policy_ids"]),
            shared_document_ids=sorted(group["shared_document_ids"]),
            provenance_urls=sorted(set().union(*(m["urls"] for m in members))),
        ))
    report.sort(key=lambda g: (RANK.index(g["classification"]), g["event_ids"]))
    return report


def audit_geopolitical_identity_divergence(repository, *, max_events=10_000, batch_size=CHUNK):
    """Return a deterministic, JSON-serializable report; issues SELECT statements only.

    Scans at most ``max_events`` geopolitical events ordered by first observation,
    in keyset batches of ``batch_size``; ``truncated`` reports whether more exist.
    Memory holds only the compact identifier index, never full version rows.
    """
    if type(max_events) is not int or not 1 <= max_events <= 100_000:
        raise ValueError("max_events must be between 1 and 100000")
    if type(batch_size) is not int or not 1 <= batch_size <= 5_000:
        raise ValueError("batch_size must be between 1 and 5000")
    facts, scanned, versions, provenance, truncated = _scan(repository.session, max_events, batch_size)
    report = _groups(facts)
    summary = {name: sum(g["classification"] == name for g in report) for name in RANK}
    return dict(source_family="geopolitical", events_scanned=scanned, versions_scanned=versions,
                provenance_scanned=provenance, truncated=truncated, max_events=max_events,
                summary=summary, groups=report)


def audit_geopolitical_identity_divergence_page(repository, *, page_size=50, after=None,
                                                max_events=10_000, batch_size=CHUNK):
    """Deterministic page of groups ordered by (classification rank, event_ids).

    ``after`` is the ``group_key`` of the previous page's last group. A cursor no
    longer present (history changed between pages) raises ValueError rather than
    silently skipping or repeating groups.
    """
    if type(page_size) is not int or not 1 <= page_size <= 1_000:
        raise ValueError("page_size must be between 1 and 1000")
    report = audit_geopolitical_identity_divergence(repository, max_events=max_events, batch_size=batch_size)
    groups = report["groups"]
    start = 0
    if after is not None:
        keys = [g["group_key"] for g in groups]
        if after not in keys:
            raise ValueError("Stale or unknown audit cursor")
        start = keys.index(after) + 1
    page = groups[start:start + page_size]
    more = start + page_size < len(groups)
    report.update(groups=page, total_groups=len(groups), page_size=page_size, after=after,
                  next_cursor=page[-1]["group_key"] if page and more else None)
    return report


def capture_identity_audit_snapshot(repository, *, max_events=100_000, batch_size=CHUNK):
    """Read-only acceptance snapshot of every group, keyed deterministically."""
    report = audit_geopolitical_identity_divergence(repository, max_events=max_events, batch_size=batch_size)
    return dict(snapshot_version=1, captured_at=datetime.now(timezone.utc).isoformat(),
                events_scanned=report["events_scanned"], truncated=report["truncated"],
                summary=report["summary"], groups=report["groups"])


def compare_identity_audits(before, after):
    """Separate historical from newly created divergence; pure function, no I/O.

    Acceptance: no ``exact_authoritative_anchor`` group in ``after`` that was not
    already in ``before``. Historical groups may remain. A truncated snapshot
    cannot prove acceptance and is reported as inconclusive.
    """
    for snapshot in (before, after):
        if not isinstance(snapshot, dict) or snapshot.get("snapshot_version") != 1:
            raise ValueError("Unsupported audit snapshot")
    old = {g["group_key"]: g for g in before["groups"]}
    new = {g["group_key"]: g for g in after["groups"]}
    added = [new[k] for k in sorted(new.keys() - old.keys(), key=lambda k: (RANK.index(new[k]["classification"]), new[k]["event_ids"]))]
    removed = [old[k] for k in sorted(old.keys() - new.keys(), key=lambda k: (RANK.index(old[k]["classification"]), old[k]["event_ids"]))]
    new_exact = [g for g in added if g["classification"] == "exact_authoritative_anchor"]
    inconclusive = bool(before["truncated"] or after["truncated"])
    return dict(
        historical_groups=len(old.keys() & new.keys()),
        new_groups=added, removed_groups=removed,
        new_by_classification={name: sum(g["classification"] == name for g in added) for name in RANK},
        new_exact_authoritative_anchor=len(new_exact),
        inconclusive=inconclusive,
        accepted=not new_exact and not inconclusive,
    )
