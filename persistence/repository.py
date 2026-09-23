"""Explicit core writes; callers own transactions and existing collector identities."""

from datetime import date, datetime, timezone
import hashlib
import json
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from persistence.models import events, event_versions, event_provenance


class IdentityConflict(ValueError):
    pass


def _canonical(value):
    def encode(item):
        if isinstance(item, datetime):
            if item.tzinfo is None or item.utcoffset() is None:
                raise ValueError("Timezone-aware timestamps required")
            return item.astimezone(timezone.utc).isoformat()
        if isinstance(item, date):
            return item.isoformat()
        raise ValueError("Unsupported structured value")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=encode)


def _attributes(value):
    if not isinstance(value, dict):
        raise ValueError("Attributes must be a structured object")
    forbidden = {"password", "secret", "token", "authorization", "cookie", "headers", "raw", "raw_payload", "html", "xml", "body", "api_key"}
    def inspect(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or key.lower() in forbidden:
                    raise ValueError("Sensitive or raw attributes are not supported")
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
    inspect(value)
    encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    if len(encoded.encode()) > 65536:
        raise ValueError("Structured attributes exceed size limit")
    return json.loads(encoded)


class EventRepository:
    """Use within database.transaction. No commits, network calls, or identity inference."""

    def __init__(self, session):
        self.session = session

    def _insert(self, table, values, keys):
        dialect = self.session.get_bind().dialect.name
        insert = {"postgresql": pg_insert, "sqlite": sqlite_insert}[dialect]
        self.session.execute(insert(table).values(**values).on_conflict_do_nothing(index_elements=keys))

    def record(self, *, source_family, event_key, identity_version, version_key,
               normalized, observed_at, make_current=False):
        """Append immutable content; promotion requires an explicit caller decision.

        The first version becomes current. Backfills default to not promoting.
        Re-observing an old version never rolls the pointer back.
        """
        allowed = {c.name for c in event_versions.c} - {"id", "event_id", "version_key", "content_hash", "observed_at", "recorded_at"}
        if set(normalized) - allowed:
            raise ValueError("Unsupported normalized fields")
        data = dict(normalized)
        data["attributes"] = _attributes(data.get("attributes", {}))
        digest = hashlib.sha256(_canonical(data).encode()).hexdigest()
        _canonical(observed_at)
        identity = dict(source_family=source_family, event_key=event_key, identity_version=identity_version)
        self._insert(events, dict(id=str(uuid4()), **identity, first_seen_at=observed_at,
                                 last_seen_at=observed_at), list(identity))
        predicate = sa.and_(*(events.c[k] == v for k, v in identity.items()))
        event = self.session.execute(sa.select(events).where(predicate).with_for_update()).mappings().one()
        self.session.execute(events.update().where(events.c.id == event["id"]).values(
            first_seen_at=min(event["first_seen_at"], observed_at), last_seen_at=max(event["last_seen_at"], observed_at)))
        existing = self.session.execute(sa.select(event_versions).where(
            event_versions.c.event_id == event["id"], event_versions.c.version_key == version_key)).mappings().first()
        if existing:
            if existing["content_hash"] != digest:
                raise IdentityConflict("Version key already belongs to different normalized content")
            return dict(existing)
        version_id = str(uuid4())
        values = dict(id=version_id, event_id=event["id"], version_key=version_key,
                      content_hash=digest, observed_at=observed_at, recorded_at=datetime.now(timezone.utc), **data)
        self.session.execute(event_versions.insert().values(**values))
        if event["current_version_id"] is None or make_current:
            self.session.execute(events.update().where(events.c.id == event["id"]).values(current_version_id=version_id))
        return dict(self.session.execute(sa.select(event_versions).where(event_versions.c.id == version_id)).mappings().one())

    def add_provenance(self, version_id, provenance_key, **values):
        allowed = {c.name for c in event_provenance.c} - {"id", "event_version_id", "provenance_key"}
        if set(values) - allowed:
            raise ValueError("Unsupported provenance fields")
        values["attributes"] = _attributes(values.get("attributes", {}))
        self._insert(event_provenance, dict(id=str(uuid4()), event_version_id=version_id,
                                          provenance_key=provenance_key, **values),
                     ["event_version_id", "provenance_key"])
        row = self.session.execute(sa.select(event_provenance).where(
            event_provenance.c.event_version_id == version_id,
            event_provenance.c.provenance_key == provenance_key)).mappings().one()
        if any(_canonical(row[k]) != _canonical(v) for k, v in values.items() if k != "retrieved_at"):
            raise IdentityConflict("Provenance key already belongs to different source evidence")
        return dict(row)

    def current(self, event_id):
        row = self.session.execute(sa.select(event_versions).join(events,
            events.c.current_version_id == event_versions.c.id).where(events.c.id == event_id)).mappings().first()
        return dict(row) if row else None

    def versions(self, event_id):
        return [dict(row) for row in self.session.execute(sa.select(event_versions).where(
            event_versions.c.event_id == event_id).order_by(event_versions.c.recorded_at, event_versions.c.id)).mappings()]

    def provenance(self, version_id):
        return [dict(row) for row in self.session.execute(sa.select(event_provenance).where(
            event_provenance.c.event_version_id == version_id)).mappings()]
