"""Phase 4C: dedicated technical snapshot tables; additive, no existing table is altered."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_technical_snapshots"  # alembic_version.version_num is VARCHAR(32)
down_revision = "0003_geo_anchor_registry"
branch_labels = None
depends_on = None

INTERVALS = ("1m", "5m", "15m", "30m", "1h", "1d")
STATES = ("bullish_setup", "bearish_setup", "bullish_momentum", "bearish_momentum", "breakout_watch",
          "breakdown_watch", "range", "mixed", "insufficient_data")
TRENDS = ("bullish", "bearish", "range", "mixed", "insufficient_data")
BREAKOUTS = ("breakout", "breakdown", "failed_breakout", "failed_breakdown", "none")


def _in(column, values, nullable=False):
    listed = ",".join(f"'{v}'" for v in values)
    return f"{column} IS NULL OR {column} IN ({listed})" if nullable else f"{column} IN ({listed})"


def upgrade():
    stamp, json = sa.DateTime(timezone=True), sa.JSON().with_variant(JSONB(), "postgresql")
    floats = ("ema9", "ema20", "ema50", "ema200", "vwap", "rsi14", "atr14")
    op.create_table(
        "technical_snapshots",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("snapshot_timestamp", stamp, nullable=False),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("source_provider", sa.String(32), nullable=False),
        sa.Column("provider_delay_seconds", sa.Integer, nullable=False),
        sa.Column("is_completed_bar", sa.Boolean, nullable=False),
        sa.Column("session_type", sa.String(8), nullable=True),
        sa.Column("price", sa.Float, nullable=False),
        *(sa.Column(name, sa.Float, nullable=True) for name in floats),
        sa.Column("volume", sa.BigInteger, nullable=False),
        sa.Column("average_volume", sa.Float, nullable=True),
        sa.Column("relative_volume", sa.Float, nullable=True),
        sa.Column("trend", sa.String(24), nullable=False),
        sa.Column("last_high_type", sa.String(4), nullable=True),
        sa.Column("last_low_type", sa.String(4), nullable=True),
        sa.Column("significant_high", json, nullable=True),
        sa.Column("significant_low", json, nullable=True),
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
        sa.Column("support_levels", json, nullable=False),
        sa.Column("resistance_levels", json, nullable=False),
        sa.Column("reasons", json, nullable=False),
        sa.Column("ema_state", json, nullable=False),
        sa.Column("vwap_state", json, nullable=False),
        sa.Column("evidence", json, nullable=False),
        sa.Column("warmup_start", stamp, nullable=False),
        sa.Column("warmup_bars", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", stamp, nullable=False),
        sa.UniqueConstraint("symbol", "interval", "snapshot_timestamp", "engine_version",
                            name="uq_technical_snapshots_identity"),
        sa.CheckConstraint(_in("interval", INTERVALS), name=op.f("ck_technical_snapshots_interval")),
        sa.CheckConstraint(_in("technical_state", STATES), name=op.f("ck_technical_snapshots_technical_state")),
        sa.CheckConstraint(_in("confidence", ("LOW", "MEDIUM", "HIGH")), name=op.f("ck_technical_snapshots_confidence")),
        sa.CheckConstraint(_in("trend", TRENDS), name=op.f("ck_technical_snapshots_trend")),
        sa.CheckConstraint(_in("breakout_state", BREAKOUTS), name=op.f("ck_technical_snapshots_breakout_state")),
        sa.CheckConstraint(_in("gap_type", ("gap_up", "gap_down", "no_gap"), nullable=True),
                           name=op.f("ck_technical_snapshots_gap_type")),
        sa.CheckConstraint(_in("session_type", ("regular", "pre", "post"), nullable=True),
                           name=op.f("ck_technical_snapshots_session_type")),
        sa.CheckConstraint("is_completed_bar", name=op.f("ck_technical_snapshots_completed_bar")),
        sa.CheckConstraint("length(symbol) > 0 AND length(engine_version) > 0 AND length(content_hash) = 64",
                           name=op.f("ck_technical_snapshots_identity_shape")),
        sa.CheckConstraint("volume >= 0 AND warmup_bars > 0 AND provider_delay_seconds >= 0 AND price > 0",
                           name=op.f("ck_technical_snapshots_value_bounds")),
    )
    op.create_index("ix_technical_snapshots_state", "technical_snapshots", ["symbol", "interval", "technical_state"])
    op.create_index("ix_technical_snapshots_created_at", "technical_snapshots", ["created_at"])
    op.create_table(
        "technical_snapshot_conflicts",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("snapshot_id", sa.Uuid(as_uuid=False), sa.ForeignKey("technical_snapshots.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("snapshot_timestamp", stamp, nullable=False),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("existing_hash", sa.String(64), nullable=False),
        sa.Column("rejected_hash", sa.String(64), nullable=False),
        sa.Column("rejected_snapshot", json, nullable=False),
        sa.Column("detected_at", stamp, nullable=False),
        sa.UniqueConstraint("snapshot_id", "rejected_hash", name="uq_technical_snapshot_conflicts_rejection"),
        sa.CheckConstraint("existing_hash <> rejected_hash AND length(rejected_hash) = 64",
                           name=op.f("ck_technical_snapshot_conflicts_distinct_hash")),
    )
    op.create_index("ix_technical_snapshot_conflicts_identity", "technical_snapshot_conflicts",
                    ["symbol", "interval", "snapshot_timestamp"])


def downgrade():
    op.drop_index("ix_technical_snapshot_conflicts_identity", table_name="technical_snapshot_conflicts")
    op.drop_table("technical_snapshot_conflicts")
    op.drop_index("ix_technical_snapshots_created_at", table_name="technical_snapshots")
    op.drop_index("ix_technical_snapshots_state", table_name="technical_snapshots")
    op.drop_table("technical_snapshots")
