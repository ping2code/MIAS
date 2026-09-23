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
"""
from collections import defaultdict
import hashlib
import json

import sqlalchemy as sa

from persistence.models import events, event_versions, event_provenance

RANK = ("exact_authoritative_anchor", "shared_policy_id", "shared_document_id", "informational_only")
CHUNK = 500


def _chunks(values):
    values = list(values)
    for start in range(0, len(values), CHUNK):
        yield values[start:start + CHUNK]


def _group_key(event_ids, classification):
    return "geo-divergence:" + hashlib.sha256(json.dumps([classification, event_ids]).encode()).hexdigest()[:24]


def audit_geopolitical_identity_divergence(repository, *, max_events=10_000):
    """Return a deterministic, JSON-serializable report; issues SELECT statements only.

    Scans at most ``max_events`` geopolitical events ordered by first observation;
    ``truncated`` reports whether more exist. Queries are chunked and bounded.
    """
    if type(max_events) is not int or not 1 <= max_events <= 100_000:
        raise ValueError("max_events must be between 1 and 100000")
    session = repository.session
    rows = session.execute(sa.select(events.c.id, events.c.event_key).where(
        events.c.source_family == "geopolitical").order_by(events.c.first_seen_at, events.c.id)
        .limit(max_events + 1)).all()
    truncated = len(rows) > max_events
    rows = rows[:max_events]
    key_by_id = {row.id: row.event_key for row in rows}
    facts = {key: dict(policy_ids=set(), anchors=set(), documents=set(), urls=set(), stages=set())
             for key in key_by_id.values()}
    version_owner, versions_scanned, provenance_scanned = {}, 0, 0
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

    # Index each deterministic identifier; only identifiers shared by 2+ events matter.
    index = defaultdict(set)
    for event_key, entry in facts.items():
        for kind, field in (("anchor", "anchors"), ("policy", "policy_ids"), ("document", "documents"), ("url", "urls")):
            for value in entry[field]:
                index[(kind, value)].add(event_key)
    groups = {}
    for (kind, value), members in index.items():
        if len(members) < 2:
            continue
        stage_sets = [facts[m]["stages"] for m in members]
        same_stage = bool(set.intersection(*stage_sets))
        if kind == "anchor":
            classification, reason = (("exact_authoritative_anchor", "shared_authoritative_anchor_same_stage") if same_stage
                                      else ("informational_only", "shared_anchor_distinct_stages"))
        elif kind == "policy":
            classification, reason = "shared_policy_id", "shared_policy_root"
        elif kind == "document":
            classification, reason = (("shared_document_id", "shared_document_id_same_stage") if same_stage
                                      else ("informational_only", "shared_document_id_distinct_stages"))
        else:
            classification, reason = "informational_only", "shared_canonical_url"
        member_ids = tuple(sorted(members))
        group = groups.setdefault(member_ids, dict(classifications=set(), reasons=set(), shared_anchors=set(),
                                                   shared_policy_ids=set(), shared_document_ids=set(), shared_urls=set()))
        group["classifications"].add(classification)
        group["reasons"].add(reason)
        group[{"anchor": "shared_anchors", "policy": "shared_policy_ids", "document": "shared_document_ids",
               "url": "shared_urls"}[kind]].add(value)

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
    summary = {name: sum(g["classification"] == name for g in report) for name in RANK}
    return dict(source_family="geopolitical", events_scanned=len(rows), versions_scanned=versions_scanned,
                provenance_scanned=provenance_scanned, truncated=truncated, max_events=max_events,
                summary=summary, groups=report)
