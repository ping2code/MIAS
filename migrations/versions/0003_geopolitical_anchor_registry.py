"""Additive durable geopolitical anchor registry; no existing table is altered."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_geo_anchor_registry"  # alembic_version.version_num is VARCHAR(32)
down_revision = "0002_macro_shadow_history"
branch_labels = None
depends_on = None


def upgrade():
    stamp = sa.DateTime(timezone=True)
    op.create_table(
        "geopolitical_anchor_registry",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("anchor_type", sa.String(32), nullable=False),
        sa.Column("anchor_value", sa.String(256), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("policy_stage", sa.String(64), nullable=False),
        sa.Column("revision_id", sa.String(256), nullable=False),
        sa.Column("policy_id", sa.String(64), nullable=False),
        sa.Column("event_key", sa.String(512), nullable=False),
        sa.Column("source_document_id", sa.String(512), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("first_seen_at", stamp, nullable=False),
        sa.Column("last_seen_at", stamp, nullable=False),
        sa.Column("created_at", stamp, nullable=False),
        sa.Column("updated_at", stamp, nullable=False),
        sa.Column("attributes", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.UniqueConstraint("anchor_type", "anchor_value", "event_type", "policy_stage", "revision_id",
                            name="uq_geopolitical_anchor_registry_key"),
        sa.CheckConstraint("anchor_type IN ('fr','eo','ofac','ftc-case','moea')",
                           name=op.f("ck_geopolitical_anchor_registry_anchor_type")),
        sa.CheckConstraint("status IN ('active','conflicted')", name=op.f("ck_geopolitical_anchor_registry_status")),
        sa.CheckConstraint("length(anchor_value) > 0 AND length(policy_id) = 64 AND length(event_key) > 0",
                           name=op.f("ck_geopolitical_anchor_registry_identity_shape")),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name=op.f("ck_geopolitical_anchor_registry_observation_order")),
    )
    op.create_index("ix_geopolitical_anchor_registry_policy", "geopolitical_anchor_registry", ["policy_id"])


def downgrade():
    op.drop_index("ix_geopolitical_anchor_registry_policy", table_name="geopolitical_anchor_registry")
    op.drop_table("geopolitical_anchor_registry")
