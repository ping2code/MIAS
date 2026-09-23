"""Durable geopolitical anchor registry: deterministic register/lookup/backfill.

Keys are only the authoritative instrument anchors the collector's resolver
already emits (``fr:``, ``eo:``, ``ofac:``, ``ftc-case:``, MOEA release IDs),
scoped by the resolver's identity stage ``(event_type, policy_stage,
revision_id)`` exactly like the Redis aliases. A registered root is never
rewritten: a conflicting registration marks the row ``conflicted`` and records
the other root, and lookups on it fail closed. No text, URLs, similarity or AI.
Callers own transactions; nothing here touches Redis or event history tables.
"""
from datetime import datetime, timezone
import re
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from persistence.models import events, event_versions, geopolitical_anchor_registry as registry

PATTERNS = {"fr": r"\d{4}-\d{4,6}", "eo": r"\d{1,6}", "ofac": r"\d{8}(?:_\d+)?",
            "ftc-case": r"\d{6,16}", "moea": r"\d{1,16}"}
KEY = ["anchor_type", "anchor_value", "event_type", "policy_stage", "revision_id"]
ROOT = re.compile(r"[0-9a-f]{64}")
MAX_ANCHORS = 32


def normalize_anchor(anchor):
    """(type, value) for an authoritative resolver anchor, else None (never guessed)."""
    if not isinstance(anchor, str) or ":" not in anchor or len(anchor) > 300:
        return None
    kind, value = anchor.split(":", 1)
    kind, value = kind.strip().lower(), value.strip()
    return (kind, value) if kind in PATTERNS and re.fullmatch(PATTERNS[kind], value) else None


def stage_of(event_type, policy_stage, revision_id):
    stage = (event_type, policy_stage, revision_id)
    if not all(isinstance(part, str) and part for part in stage):
        raise ValueError("Complete resolver identity stage required")
    return stage


class AnchorRegistryRepository:
    """Use within database.transaction; SQLAlchemy stays behind this boundary."""

    def __init__(self, session):
        self.session = session

    def _normalized(self, anchors):
        found, skipped = set(), 0
        for anchor in anchors or []:
            value = normalize_anchor(anchor)
            if value is None:
                skipped += 1
            else:
                found.add(value)
        if len(found) > MAX_ANCHORS:
            raise ValueError("Too many identity anchors")
        return sorted(found), skipped

    def register(self, *, anchors, stage, policy_id, event_key, observed_at, source_document_id=None):
        """Idempotent per (anchor, stage). Returns per-call counts; never overwrites a root."""
        if not isinstance(policy_id, str) or not ROOT.fullmatch(policy_id) or not event_key:
            raise ValueError("Resolved geopolitical root required")
        if observed_at.tzinfo is None:
            raise ValueError("Aware observation time required")
        stage = stage_of(*stage)
        normalized, skipped = self._normalized(anchors)
        counts = dict(anchors_seen=len(normalized) + skipped, inserted=0, already_present=0,
                      conflicts=0, skipped_non_authoritative=skipped)
        dialect = self.session.get_bind().dialect.name
        insert = {"postgresql": pg_insert, "sqlite": sqlite_insert}[dialect]
        for anchor_type, anchor_value in normalized:
            key = dict(zip(KEY, (anchor_type, anchor_value, *stage)))
            now = datetime.now(timezone.utc)
            inserted = self.session.execute(insert(registry).values(
                id=str(uuid4()), **key, policy_id=policy_id, event_key=event_key,
                source_document_id=source_document_id, status="active", first_seen_at=observed_at,
                last_seen_at=observed_at, created_at=now, updated_at=now, attributes={},
            ).on_conflict_do_nothing(index_elements=KEY).returning(registry.c.id)).scalar_one_or_none()
            if inserted is not None:
                counts["inserted"] += 1
                continue
            row = self.session.execute(sa.select(registry).where(
                *(registry.c[k] == v for k, v in key.items())).with_for_update()).mappings().one()
            if row["policy_id"] == policy_id and row["event_key"] == event_key:
                counts["already_present"] += 1
                first, last = min(row["first_seen_at"], observed_at), max(row["last_seen_at"], observed_at)
                if (first, last) != (row["first_seen_at"], row["last_seen_at"]):
                    self.session.execute(registry.update().where(registry.c.id == row["id"]).values(
                        first_seen_at=first, last_seen_at=last, updated_at=now))
                continue
            counts["conflicts"] += 1
            attrs = dict(row["attributes"] or {})
            roots = sorted(set(attrs.get("conflicting_policy_ids", [])) | {policy_id})
            keys = sorted(set(attrs.get("conflicting_event_keys", [])) | {event_key})
            if row["status"] != "conflicted" or roots != attrs.get("conflicting_policy_ids") \
                    or keys != attrs.get("conflicting_event_keys"):
                attrs.update(conflicting_policy_ids=roots, conflicting_event_keys=keys)
                # The registered root is preserved; only status/evidence change.
                self.session.execute(registry.update().where(registry.c.id == row["id"]).values(
                    status="conflicted", attributes=attrs, updated_at=now))
        return counts

    @staticmethod
    def _lookup_query(normalized, stage):
        # Served by the unique (anchor_type, anchor_value, event_type, policy_stage, revision_id) index.
        return sa.select(
            registry.c.anchor_type, registry.c.anchor_value, registry.c.policy_id,
            registry.c.event_key, registry.c.status).where(
            registry.c.event_type == stage[0], registry.c.policy_stage == stage[1],
            registry.c.revision_id == stage[2],
            sa.or_(*(sa.and_(registry.c.anchor_type == t, registry.c.anchor_value == v) for t, v in normalized)),
        ).limit(MAX_ANCHORS + 1)

    def lookup(self, *, anchors, stage):
        """Read-only. hit: exactly one active root; conflict: any conflicted row or 2+ roots."""
        stage = stage_of(*stage)
        normalized, _ = self._normalized(anchors)
        if not normalized:
            return dict(status="miss", policy_id=None, event_key=None, anchors=[])
        rows = self.session.execute(self._lookup_query(normalized, stage)).mappings().all()
        matched = sorted(f"{r['anchor_type']}:{r['anchor_value']}" for r in rows)
        roots = {(r["policy_id"], r["event_key"]) for r in rows}
        if not rows:
            return dict(status="miss", policy_id=None, event_key=None, anchors=[])
        if any(r["status"] != "active" for r in rows) or len(roots) != 1:
            return dict(status="conflict", policy_id=None, event_key=None, anchors=matched)
        policy_id, event_key = roots.pop()
        return dict(status="hit", policy_id=policy_id, event_key=event_key, anchors=matched)

    def conflicts(self, *, limit=100, after=None):
        """Read-only bounded inspection of conflicted anchors, deterministic key order.

        ``after`` is the previous page's last registry key (5-tuple, KEY order).
        """
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        query = sa.select(registry).where(registry.c.status == "conflicted")
        if after is not None:
            if not isinstance(after, (list, tuple)) or len(after) != len(KEY) or not all(isinstance(v, str) for v in after):
                raise ValueError("Invalid conflict cursor")
            query = query.where(sa.tuple_(*(registry.c[k] for k in KEY)) > sa.tuple_(
                *(sa.literal(v, type_=registry.c[k].type) for k, v in zip(KEY, after))))
        return [dict(r) for r in self.session.execute(
            query.order_by(*(registry.c[k] for k in KEY)).limit(limit)).mappings()]

    def status(self):
        """Read-only aggregate counts; one bounded SELECT."""
        row = self.session.execute(sa.select(
            sa.func.count(), sa.func.count().filter(registry.c.status == "active"),
            sa.func.count().filter(registry.c.status == "conflicted"), sa.func.max(registry.c.updated_at),
        ).select_from(registry)).one()
        return dict(rows=row[0], active=row[1], conflicted=row[2], latest_updated_at=row[3])


def register_geopolitical_event(session, event, observed_at):
    """Register a resolved, shadow-persisted collector event's own anchors."""
    return AnchorRegistryRepository(session).register(
        anchors=event.get("identity_anchors"),
        stage=(event.get("event_type"), event.get("policy_stage"), event.get("revision_id")),
        policy_id=event.get("policy_id"), event_key=event.get("event_id"),
        observed_at=observed_at, source_document_id=event.get("document_id"))


def backfill_geopolitical_anchor_registry(session, *, max_events=100_000):
    """Deterministic, idempotent, rerunnable backfill from persisted versions only.

    Reads events/event_versions (never modifies them), registers each resolved
    version's stored anchors under its stored root and stage, reports conflicts
    and never merges or chooses between historical roots. Caller owns the
    transaction; Redis is never touched.
    """
    if type(max_events) is not int or not 1 <= max_events <= 1_000_000:
        raise ValueError("max_events must be between 1 and 1000000")
    repo = AnchorRegistryRepository(session)
    summary = dict(events_scanned=0, versions_scanned=0, versions_skipped=0, anchors_seen=0, inserted=0,
                   already_present=0, conflicts=0, skipped_non_authoritative=0, truncated=False)
    rows = session.execute(sa.select(events.c.id, events.c.event_key).where(
        events.c.source_family == "geopolitical").order_by(events.c.first_seen_at, events.c.id)
        .limit(max_events + 1)).all()
    summary["truncated"] = len(rows) > max_events
    for event in rows[:max_events]:
        summary["events_scanned"] += 1
        versions = session.execute(sa.select(
            event_versions.c.event_type, event_versions.c.stage, event_versions.c.revision_key,
            event_versions.c.attributes, event_versions.c.observed_at).where(
            event_versions.c.event_id == event.id).order_by(event_versions.c.observed_at, event_versions.c.id)).mappings()
        for version in versions:
            summary["versions_scanned"] += 1
            attrs = version["attributes"] or {}
            if attrs.get("identity_status") != "resolved" or not ROOT.fullmatch(str(attrs.get("policy_id"))):
                summary["versions_skipped"] += 1
                continue
            try:
                counts = repo.register(anchors=attrs.get("identity_anchors"),
                                       stage=(version["event_type"], version["stage"], version["revision_key"]),
                                       policy_id=attrs["policy_id"], event_key=event.event_key,
                                       observed_at=version["observed_at"], source_document_id=attrs.get("document_id"))
            except ValueError:
                summary["versions_skipped"] += 1
                continue
            for key, value in counts.items():
                summary[key] += value
    return summary
