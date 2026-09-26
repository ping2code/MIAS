"""Core metadata only. No engine creation, connections, or automatic DDL."""

from datetime import timezone
from decimal import Decimal

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


class ExactDecimal(TypeDecorator):
    """Exact Decimal storage: PostgreSQL NUMERIC; SQLite (tests/dev only) canonical decimal TEXT.

    SQLite has no decimal type, and its NUMERIC affinity would turn values into
    64-bit floats. Storing canonical text keeps SQLite round trips exact too.
    """
    impl = sa.Numeric
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(sa.Text() if dialect.name == "sqlite" else sa.Numeric(asdecimal=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = Decimal(value)
        if not value.is_finite():
            raise ValueError("Finite decimal required")
        return format(value, "f") if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


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

# Immutable snapshots of already-computed outcomes; never delivery state.
event_history = sa.Table(
    "event_history", metadata,
    sa.Column("id", ID, primary_key=True),
    sa.Column("event_version_id", ID, sa.ForeignKey("event_versions.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("attributes", JSON, nullable=False),
    sa.Column("recorded_at", UTCDateTime(), nullable=False),
    sa.CheckConstraint("kind IN ('score','decision','ai')", name="history_kind"),
    sa.CheckConstraint("length(content_hash) = 64", name="history_hash"),
    sa.UniqueConstraint("event_version_id", "kind", "content_hash", name="uq_event_history_snapshot"),
    sa.Index("ix_event_history_version_recorded", "event_version_id", "recorded_at"),
)

# Durable geopolitical identity coordination (Phase 2H). One row per authoritative
# anchor within one collector identity stage; aliases in Redis are stage-scoped,
# so the stage is part of the key. The root is never rewritten; a conflicting
# registration marks the row conflicted and lookups then fail closed.
geopolitical_anchor_registry = sa.Table(
    "geopolitical_anchor_registry", metadata,
    sa.Column("id", ID, primary_key=True),
    sa.Column("anchor_type", sa.String(32), nullable=False),
    sa.Column("anchor_value", sa.String(256), nullable=False),
    sa.Column("event_type", sa.String(64), nullable=False),
    sa.Column("policy_stage", sa.String(64), nullable=False),
    sa.Column("revision_id", sa.String(256), nullable=False),
    sa.Column("policy_id", sa.String(64), nullable=False),
    sa.Column("event_key", sa.String(512), nullable=False),
    sa.Column("source_document_id", sa.String(512), nullable=True),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("first_seen_at", UTCDateTime(), nullable=False),
    sa.Column("last_seen_at", UTCDateTime(), nullable=False),
    sa.Column("created_at", UTCDateTime(), nullable=False),
    sa.Column("updated_at", UTCDateTime(), nullable=False),
    sa.Column("attributes", JSON, nullable=False),
    sa.UniqueConstraint("anchor_type", "anchor_value", "event_type", "policy_stage", "revision_id",
                        name="uq_geopolitical_anchor_registry_key"),
    sa.CheckConstraint("anchor_type IN ('fr','eo','ofac','ftc-case','moea')", name="anchor_type"),
    sa.CheckConstraint("status IN ('active','conflicted')", name="status"),
    sa.CheckConstraint("length(anchor_value) > 0 AND length(policy_id) = 64 AND length(event_key) > 0",
                       name="identity_shape"),
    sa.CheckConstraint("last_seen_at >= first_seen_at", name="observation_order"),
    sa.Index("ix_geopolitical_anchor_registry_policy", "policy_id"),
)

# Phase 4C: durable technical snapshots (one row per completed bar, symbol, interval and engine version).
# Scalar columns hold the frequently queried metrics; JSON holds only structured arrays/evidence.
TECHNICAL_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "1d")
TECHNICAL_STATES = ("bullish_setup", "bearish_setup", "bullish_momentum", "bearish_momentum", "breakout_watch",
                    "breakdown_watch", "range", "mixed", "insufficient_data")
TECHNICAL_TRENDS = ("bullish", "bearish", "range", "mixed", "insufficient_data")
BREAKOUT_STATES = ("breakout", "breakdown", "failed_breakout", "failed_breakdown", "none")


def _in(column, values, nullable=False):
    listed = ",".join(f"'{v}'" for v in values)
    return f"{column} IS NULL OR {column} IN ({listed})" if nullable else f"{column} IN ({listed})"


technical_snapshots = sa.Table(
    "technical_snapshots", metadata,
    sa.Column("id", ID, primary_key=True),
    sa.Column("symbol", sa.String(16), nullable=False),
    sa.Column("interval", sa.String(8), nullable=False),
    sa.Column("snapshot_timestamp", UTCDateTime(), nullable=False),
    sa.Column("engine_version", sa.String(32), nullable=False),
    sa.Column("source_provider", sa.String(32), nullable=False),
    sa.Column("provider_delay_seconds", sa.Integer, nullable=False),
    sa.Column("is_completed_bar", sa.Boolean, nullable=False),
    sa.Column("session_type", sa.String(8), nullable=True),
    sa.Column("price", sa.Float, nullable=False),
    sa.Column("ema9", sa.Float, nullable=True),
    sa.Column("ema20", sa.Float, nullable=True),
    sa.Column("ema50", sa.Float, nullable=True),
    sa.Column("ema200", sa.Float, nullable=True),
    sa.Column("vwap", sa.Float, nullable=True),
    sa.Column("rsi14", sa.Float, nullable=True),
    sa.Column("atr14", sa.Float, nullable=True),
    # NUMERIC since 0006: exact vendor volume (fractional shares) and deterministic derived volume values.
    sa.Column("volume", ExactDecimal(), nullable=False),
    sa.Column("average_volume", ExactDecimal(), nullable=True),
    sa.Column("relative_volume", ExactDecimal(), nullable=True),
    sa.Column("trend", sa.String(24), nullable=False),
    sa.Column("last_high_type", sa.String(4), nullable=True),
    sa.Column("last_low_type", sa.String(4), nullable=True),
    sa.Column("significant_high", JSON, nullable=True),
    sa.Column("significant_low", JSON, nullable=True),
    sa.Column("gap_type", sa.String(16), nullable=True),
    sa.Column("gap_percent", sa.Float, nullable=True),
    sa.Column("gap_absolute", sa.Float, nullable=True),
    sa.Column("breakout_state", sa.String(24), nullable=False),
    sa.Column("breakout_level", sa.Float, nullable=True),
    sa.Column("momentum", sa.String(24), nullable=True),
    sa.Column("ema_alignment", sa.String(24), nullable=True),
    sa.Column("vwap_position", sa.String(32), nullable=True),
    sa.Column("technical_state", sa.String(24), nullable=False),
    sa.Column("confidence", sa.String(8), nullable=False),
    sa.Column("agreeing", sa.Integer, nullable=False),
    sa.Column("conflicting", sa.Integer, nullable=False),
    sa.Column("support_levels", JSON, nullable=False),
    sa.Column("resistance_levels", JSON, nullable=False),
    sa.Column("reasons", JSON, nullable=False),
    sa.Column("ema_state", JSON, nullable=False),
    sa.Column("vwap_state", JSON, nullable=False),
    sa.Column("evidence", JSON, nullable=False),
    sa.Column("warmup_start", UTCDateTime(), nullable=False),
    sa.Column("warmup_bars", sa.Integer, nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("created_at", UTCDateTime(), nullable=False),
    sa.UniqueConstraint("symbol", "interval", "snapshot_timestamp", "engine_version",
                        name="uq_technical_snapshots_identity"),
    sa.CheckConstraint(_in("interval", TECHNICAL_INTERVALS), name="interval"),
    sa.CheckConstraint(_in("technical_state", TECHNICAL_STATES), name="technical_state"),
    sa.CheckConstraint(_in("confidence", ("LOW", "MEDIUM", "HIGH")), name="confidence"),
    sa.CheckConstraint(_in("trend", TECHNICAL_TRENDS), name="trend"),
    sa.CheckConstraint(_in("breakout_state", BREAKOUT_STATES), name="breakout_state"),
    sa.CheckConstraint(_in("gap_type", ("gap_up", "gap_down", "no_gap"), nullable=True), name="gap_type"),
    sa.CheckConstraint(_in("session_type", ("regular", "pre", "post"), nullable=True), name="session_type"),
    sa.CheckConstraint("is_completed_bar", name="completed_bar"),
    sa.CheckConstraint("length(symbol) > 0 AND length(engine_version) > 0 AND length(content_hash) = 64",
                       name="identity_shape"),
    sa.CheckConstraint("volume >= 0 AND warmup_bars > 0 AND provider_delay_seconds >= 0 AND price > 0",
                       name="value_bounds"),
    sa.Index("ix_technical_snapshots_state", "symbol", "interval", "technical_state"),
    sa.Index("ix_technical_snapshots_created_at", "created_at"),
)

# Rejected rewrites of an existing snapshot identity: recorded once per distinct content, never applied.
technical_snapshot_conflicts = sa.Table(
    "technical_snapshot_conflicts", metadata,
    sa.Column("id", ID, primary_key=True),
    sa.Column("snapshot_id", ID, sa.ForeignKey("technical_snapshots.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("symbol", sa.String(16), nullable=False),
    sa.Column("interval", sa.String(8), nullable=False),
    sa.Column("snapshot_timestamp", UTCDateTime(), nullable=False),
    sa.Column("engine_version", sa.String(32), nullable=False),
    sa.Column("existing_hash", sa.String(64), nullable=False),
    sa.Column("rejected_hash", sa.String(64), nullable=False),
    sa.Column("rejected_snapshot", JSON, nullable=False),
    sa.Column("detected_at", UTCDateTime(), nullable=False),
    sa.UniqueConstraint("snapshot_id", "rejected_hash", name="uq_technical_snapshot_conflicts_rejection"),
    sa.CheckConstraint("existing_hash <> rejected_hash AND length(rejected_hash) = 64", name="distinct_hash"),
    sa.Index("ix_technical_snapshot_conflicts_identity", "symbol", "interval", "snapshot_timestamp"),
)

# Phase 6 (migration 0007): append-only prospective evidence ledger. Collection, provenance and integrity only:
# no returns, states, prices, raw bars or vendor payloads. Snapshots stay in technical_snapshots (soft reference).
LEDGER_STATUSES = ("collected", "failed", "conflict")
LEDGER_INTERVALS = ("5m", "1h", "1d")
LEDGER_ERROR_KINDS = ("provider_auth", "rate_limit", "transport", "provider_payload", "contract_violation",
                      "incomplete_session", "registry_hash_mismatch", "engine_version_mismatch", "snapshot_unavailable",
                      "internal")
LEDGER_CHECKS = dict(
    record_status=_in("record_status", LEDGER_STATUSES),
    interval=_in("interval", LEDGER_INTERVALS),
    error_kind=_in("error_kind", LEDGER_ERROR_KINDS, nullable=True),
    hash_shape=("length(registry_hash) = 64 AND (bar_content_hash IS NULL OR length(bar_content_hash) = 64) AND "
                "(evidence_hash IS NULL OR length(evidence_hash) = 64) AND "
                "(snapshot_content_hash IS NULL OR length(snapshot_content_hash) = 64)"),
    provenance_shape=("length(code_commit) BETWEEN 7 AND 40 AND data_delay_seconds >= 0 AND length(symbol) > 0 "
                      "AND length(engine_version) > 0"),
    collected_invariants=("record_status <> 'collected' OR (bar_count > 0 AND bar_content_hash IS NOT NULL AND "
                          "evidence_hash IS NOT NULL AND error_kind IS NULL AND conflicts_with IS NULL)"),
    failed_invariants=("record_status <> 'failed' OR (error_kind IS NOT NULL AND bar_content_hash IS NULL AND "
                       "evidence_hash IS NULL AND conflicts_with IS NULL)"),
    conflict_invariants=("record_status <> 'conflict' OR (conflicts_with IS NOT NULL AND evidence_hash IS NOT NULL "
                         "AND error_kind IS NULL)"),
)

technical_evidence_ledger = sa.Table(
    "technical_evidence_ledger", metadata,
    sa.Column("id", ID, primary_key=True),
    sa.Column("ledger_format_version", sa.String(16), nullable=False),
    sa.Column("record_status", sa.String(16), nullable=False),
    sa.Column("market_session_date", sa.Date, nullable=False),
    sa.Column("symbol", sa.String(16), nullable=False),
    sa.Column("interval", sa.String(8), nullable=False),
    sa.Column("engine_version", sa.String(32), nullable=False),
    sa.Column("registry_version", sa.String(16), nullable=False),
    sa.Column("registry_hash", sa.String(64), nullable=False),
    sa.Column("hypothesis_hashes", JSON, nullable=False),
    sa.Column("provider", sa.String(32), nullable=False),
    sa.Column("adjusted", sa.Boolean, nullable=False),
    sa.Column("data_delay_seconds", sa.Integer, nullable=False),
    sa.Column("include_extended_hours", sa.Boolean, nullable=False),
    sa.Column("code_commit", sa.String(40), nullable=False),
    sa.Column("collected_at", UTCDateTime(), nullable=False),
    sa.Column("expected_collection_date", sa.Date, nullable=False),
    sa.Column("backfilled", sa.Boolean, nullable=False),
    sa.Column("bar_count", sa.Integer, nullable=True),
    sa.Column("bar_content_hash", sa.String(64), nullable=True),
    sa.Column("snapshot_timestamp", UTCDateTime(), nullable=True),
    sa.Column("snapshot_content_hash", sa.String(64), nullable=True),
    sa.Column("evidence_hash", sa.String(64), nullable=True),
    sa.Column("error_kind", sa.String(32), nullable=True),
    sa.Column("error_detail", sa.String(200), nullable=True),
    sa.Column("conflicts_with", ID, sa.ForeignKey("technical_evidence_ledger.id", ondelete="RESTRICT"), nullable=True),
    sa.Column("recorded_at", UTCDateTime(), nullable=False),
    *(sa.CheckConstraint(text, name=name) for name, text in LEDGER_CHECKS.items()),
    sa.Index("uq_technical_evidence_ledger_accepted", "market_session_date", "symbol", "interval", "engine_version",
             "registry_hash", unique=True, postgresql_where=sa.text("record_status = 'collected'"),
             sqlite_where=sa.text("record_status = 'collected'")),
    sa.Index("uq_technical_evidence_ledger_conflict", "conflicts_with", "evidence_hash", unique=True,
             postgresql_where=sa.text("record_status = 'conflict'"), sqlite_where=sa.text("record_status = 'conflict'")),
    sa.Index("ix_technical_evidence_ledger_session", "symbol", "interval", "market_session_date"),
    sa.Index("ix_technical_evidence_ledger_status", "record_status", "market_session_date"),
    sa.Index("ix_technical_evidence_ledger_collected_at", "collected_at"),
    sa.Index("ix_technical_evidence_ledger_conflicts_with", "conflicts_with"),
)
