"""Core metadata only. No engine creation, connections, or automatic DDL."""

from datetime import timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Require real timezone-aware instants and preserve UTC on SQLite reads."""
    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timezone-aware timestamp required")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


metadata = sa.MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_name)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})
JSON = sa.JSON().with_variant(JSONB(), "postgresql")
ID = sa.Uuid(as_uuid=False)

events = sa.Table(
    "events", metadata,
    sa.Column("id", ID, primary_key=True),
    sa.Column("source_family", sa.String(32), nullable=False),
    sa.Column("event_key", sa.String(512), nullable=False),
    sa.Column("identity_version", sa.String(64), nullable=False),
    sa.Column("first_seen_at", UTCDateTime(), nullable=False),
    sa.Column("last_seen_at", UTCDateTime(), nullable=False),
    sa.Column("current_version_id", ID, nullable=True),
    sa.UniqueConstraint("source_family", "identity_version", "event_key", name="uq_events_identity"),
    sa.CheckConstraint("source_family IN ('news','sec','fed','macro','treasury','geopolitical')", name="source_family"),
    sa.CheckConstraint("length(event_key) > 0 AND length(identity_version) > 0", name="identity_nonempty"),
    sa.CheckConstraint("last_seen_at >= first_seen_at", name="observation_order"),
    # A current pointer can only refer to a version of this very event.
    sa.ForeignKeyConstraint(["id", "current_version_id"], ["event_versions.event_id", "event_versions.id"],
                            name="fk_events_current_version", use_alter=True, deferrable=True, initially="DEFERRED"),
    sa.Index("ix_events_family_last_seen", "source_family", "last_seen_at"),
)

event_versions = sa.Table(
    "event_versions", metadata,
    sa.Column("id", ID, primary_key=True),
    sa.Column("event_id", ID, sa.ForeignKey("events.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("version_key", sa.String(512), nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("schema_version", sa.Integer, nullable=False),
    sa.Column("normalizer_version", sa.String(64), nullable=False),
    sa.Column("headline", sa.Text, nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("source_name", sa.String(256), nullable=False),
    sa.Column("publisher", sa.String(256), nullable=False),
    sa.Column("canonical_url", sa.Text, nullable=True),
    sa.Column("event_type", sa.String(64), nullable=True),
    sa.Column("market_scope", sa.String(256), nullable=True),
    sa.Column("published_at", UTCDateTime(), nullable=True),
    sa.Column("publication_date", sa.Date, nullable=True),
    sa.Column("timestamp_precision", sa.String(16), nullable=False),
    sa.Column("publication_basis", sa.String(64), nullable=False),
    sa.Column("stage", sa.String(64), nullable=True),
    sa.Column("revision_key", sa.String(256), nullable=True),
    sa.Column("attributes", JSON, nullable=False),
    sa.Column("observed_at", UTCDateTime(), nullable=False),
    sa.Column("recorded_at", UTCDateTime(), nullable=False),
    sa.UniqueConstraint("event_id", "version_key", name="uq_event_versions_key"),
    sa.UniqueConstraint("event_id", "id", name="uq_event_versions_owner"),
    sa.CheckConstraint("length(content_hash) = 64 AND schema_version > 0 AND length(version_key) > 0", name="version_shape"),
    sa.CheckConstraint("timestamp_precision IN ('unknown','date','minute','second')", name="timestamp_precision"),
    sa.CheckConstraint("(timestamp_precision = 'unknown' AND published_at IS NULL AND publication_date IS NULL) OR (timestamp_precision = 'date' AND published_at IS NULL AND publication_date IS NOT NULL) OR (timestamp_precision IN ('minute','second') AND published_at IS NOT NULL)", name="publication_precision"),
    sa.Index("ix_event_versions_event_observed", "event_id", "observed_at"),
    sa.Index("ix_event_versions_published", "published_at"),
    sa.Index("ix_event_versions_content_hash", "content_hash"),
)

event_provenance = sa.Table(
    "event_provenance", metadata,
    sa.Column("id", ID, primary_key=True),
    sa.Column("event_version_id", ID, sa.ForeignKey("event_versions.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("provenance_key", sa.String(512), nullable=False),
    sa.Column("source_name", sa.String(256), nullable=False),
    sa.Column("document_id", sa.String(512), nullable=True),
    sa.Column("canonical_url", sa.Text, nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=True),
    sa.Column("mime_type", sa.String(128), nullable=True),
    sa.Column("byte_size", sa.BigInteger, nullable=True),
    sa.Column("retrieved_at", UTCDateTime(), nullable=False),
    sa.Column("attributes", JSON, nullable=False),
    sa.UniqueConstraint("event_version_id", "provenance_key", name="uq_event_provenance_key"),
    sa.CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="byte_size"),
    sa.CheckConstraint("content_hash IS NULL OR length(content_hash) = 64", name="content_hash"),
    sa.CheckConstraint("length(provenance_key) > 0", name="provenance_nonempty"),
    sa.Index("ix_event_provenance_document", "source_name", "document_id"),
    sa.Index("ix_event_provenance_version", "event_version_id"),
)
