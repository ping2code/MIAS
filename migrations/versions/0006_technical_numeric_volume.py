"""Phase 4C follow-up: technical snapshot volume columns become NUMERIC (exact, deterministic persistence).

``volume``, ``average_volume`` and ``relative_volume`` change from DOUBLE PRECISION to
unconstrained NUMERIC.

- ``volume`` now holds the vendor's exact Decimal volume (fractional shares).
- ``average_volume`` and ``relative_volume`` hold the engine's float values, stored as
  the exact decimal of their shortest round-trip text.

**Existing rows:** the cast goes through text (``::text::numeric``). PostgreSQL's direct
``double precision -> numeric`` cast keeps only 15 significant digits, while float
text output (``extra_float_digits`` default, PostgreSQL 12+) is shortest-exact. So every
stored float converts to the numeric that reads back as the same float.

**Downgrade** casts back to DOUBLE PRECISION. That is lossy for values with more than
about 17 significant digits; export first.

0004 and 0005 are unchanged. The chain is 0003 -> 0004 -> 0005 -> 0006.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_technical_numeric_volume"  # alembic_version.version_num is VARCHAR(32)
down_revision = "0005_technical_fractional_vol"
branch_labels = None
depends_on = None

COLUMNS = (("volume", False), ("average_volume", True), ("relative_volume", True))


def _alter(existing, new, using):
    if op.get_bind().dialect.name == "sqlite":
        # SQLite (tests/dev only) cannot ALTER a column type; batch mode recreates the table. SQLite has no decimal
        # type, so exact values are stored as canonical decimal TEXT (see persistence.models.ExactDecimal).
        existing = sa.Text() if isinstance(existing, sa.Numeric) and not isinstance(existing, sa.Float) else existing
        new = sa.Text() if isinstance(new, sa.Numeric) and not isinstance(new, sa.Float) else new
        with op.batch_alter_table("technical_snapshots", recreate="always") as batch:
            for name, nullable in COLUMNS:
                batch.alter_column(name, existing_type=existing, type_=new, existing_nullable=nullable)
        return
    for name, nullable in COLUMNS:
        op.alter_column("technical_snapshots", name, existing_type=existing, type_=new, existing_nullable=nullable,
                        postgresql_using=using.format(name))


def upgrade():
    _alter(sa.Float(), sa.Numeric(), "{0}::text::numeric")


def downgrade():
    _alter(sa.Numeric(), sa.Float(), "{0}::double precision")
