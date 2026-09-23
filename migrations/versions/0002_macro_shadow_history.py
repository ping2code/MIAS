"""Add immutable score/decision/AI snapshots without changing foundation tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_macro_shadow_history"
down_revision = "0001_persistence_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "event_history",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("event_version_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("attributes", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_version_id"], ["event_versions.id"],
                                name="fk_event_history_event_version_id_event_versions", ondelete="RESTRICT"),
        sa.CheckConstraint("kind IN ('score','decision','ai')", name=op.f("ck_event_history_history_kind")),
        sa.CheckConstraint("length(content_hash) = 64", name=op.f("ck_event_history_history_hash")),
        sa.UniqueConstraint("event_version_id", "kind", "content_hash", name="uq_event_history_snapshot"),
    )
    op.create_index("ix_event_history_version_recorded", "event_history", ["event_version_id", "recorded_at"])


def downgrade():
    op.drop_index("ix_event_history_version_recorded", table_name="event_history")
    op.drop_table("event_history")
