"""Phase 4C follow-up: technical snapshot volume becomes double precision (fractional-share volume).

The first live provider check showed that vendor aggregates carry fractional volume (fractional-share
trades). Snapshot volume is the engine's float bar volume, so the column changes from BIGINT to
DOUBLE PRECISION. Existing whole-number values convert exactly.

Downgrade converts back to BIGINT with ``round()`` and is lossy for fractional values; export first.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_technical_fractional_vol"  # alembic_version.version_num is VARCHAR(32)
down_revision = "0004_technical_snapshots"
branch_labels = None
depends_on = None


def _alter(existing, new, using):
    if op.get_bind().dialect.name == "sqlite":
        # SQLite cannot ALTER a column type; batch mode recreates the table (tests/dev only).
        with op.batch_alter_table("technical_snapshots", recreate="always") as batch:
            batch.alter_column("volume", existing_type=existing, type_=new, existing_nullable=False)
        return
    op.alter_column("technical_snapshots", "volume", existing_type=existing, type_=new, existing_nullable=False,
                    postgresql_using=using)


def upgrade():
    _alter(sa.BigInteger(), sa.Float(), "volume::double precision")


def downgrade():
    _alter(sa.Float(), sa.BigInteger(), "round(volume)::bigint")
