"""Phase 6: append-only technical_evidence_ledger (prospective collection, provenance and integrity only).

- There are no returns, states, prices, raw bars or vendor payloads. Snapshots stay
  in ``technical_snapshots``, referenced softly (no FK).
- Append-only is enforced by triggers that reject UPDATE, DELETE and TRUNCATE on
  PostgreSQL (UPDATE/DELETE on SQLite, which is used by tests only), and by an
  insert-only repository.
- **Downgrade refuses whenever the table contains rows,** so evidence is never
  destroyed. There is no override.
- 0001-0006 are unchanged. The chain is 0006 -> 0007.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_technical_evidence_ledger"  # alembic_version.version_num is VARCHAR(32)
down_revision = "0006_technical_numeric_volume"
branch_labels = None
depends_on = None

TABLE = "technical_evidence_ledger"
STATUSES = ("collected", "failed", "conflict")
INTERVALS = ("5m", "1h", "1d")
ERROR_KINDS = ("provider_auth", "rate_limit", "transport", "provider_payload", "contract_violation",
               "incomplete_session", "registry_hash_mismatch", "engine_version_mismatch", "snapshot_unavailable",
               "internal")
MESSAGE = "technical_evidence_ledger is append-only"


def _in(column, values, nullable=False):
    listed = ",".join(f"'{v}'" for v in values)
    return f"{column} IS NULL OR {column} IN ({listed})" if nullable else f"{column} IN ({listed})"


CHECKS = dict(
    record_status=_in("record_status", STATUSES),
    interval=_in("interval", INTERVALS),
    error_kind=_in("error_kind", ERROR_KINDS, nullable=True),
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


def upgrade():
    stamp, json = sa.DateTime(timezone=True), sa.JSON().with_variant(JSONB(), "postgresql")
    ident = sa.Uuid(as_uuid=False)
    op.create_table(
        TABLE,
        sa.Column("id", ident, primary_key=True),
        sa.Column("ledger_format_version", sa.String(16), nullable=False),
        sa.Column("record_status", sa.String(16), nullable=False),
        sa.Column("market_session_date", sa.Date, nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("registry_version", sa.String(16), nullable=False),
        sa.Column("registry_hash", sa.String(64), nullable=False),
        sa.Column("hypothesis_hashes", json, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("adjusted", sa.Boolean, nullable=False),
        sa.Column("data_delay_seconds", sa.Integer, nullable=False),
        sa.Column("include_extended_hours", sa.Boolean, nullable=False),
        sa.Column("code_commit", sa.String(40), nullable=False),
        sa.Column("collected_at", stamp, nullable=False),
        sa.Column("expected_collection_date", sa.Date, nullable=False),
        sa.Column("backfilled", sa.Boolean, nullable=False),
        sa.Column("bar_count", sa.Integer, nullable=True),
        sa.Column("bar_content_hash", sa.String(64), nullable=True),
        sa.Column("snapshot_timestamp", stamp, nullable=True),
        sa.Column("snapshot_content_hash", sa.String(64), nullable=True),
        sa.Column("evidence_hash", sa.String(64), nullable=True),
        sa.Column("error_kind", sa.String(32), nullable=True),
        sa.Column("error_detail", sa.String(200), nullable=True),
        sa.Column("conflicts_with", ident, sa.ForeignKey(f"{TABLE}.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("recorded_at", stamp, nullable=False),
        *(sa.CheckConstraint(text, name=op.f(f"ck_{TABLE}_{name}")) for name, text in CHECKS.items()),
    )
    where = dict(postgresql_where=sa.text("record_status = 'collected'"), sqlite_where=sa.text("record_status = 'collected'"))
    op.create_index("uq_technical_evidence_ledger_accepted", TABLE,
                    ["market_session_date", "symbol", "interval", "engine_version", "registry_hash"], unique=True, **where)
    op.create_index("uq_technical_evidence_ledger_conflict", TABLE, ["conflicts_with", "evidence_hash"], unique=True,
                    postgresql_where=sa.text("record_status = 'conflict'"),
                    sqlite_where=sa.text("record_status = 'conflict'"))
    op.create_index("ix_technical_evidence_ledger_session", TABLE, ["symbol", "interval", "market_session_date"])
    op.create_index("ix_technical_evidence_ledger_status", TABLE, ["record_status", "market_session_date"])
    op.create_index("ix_technical_evidence_ledger_collected_at", TABLE, ["collected_at"])
    op.create_index("ix_technical_evidence_ledger_conflicts_with", TABLE, ["conflicts_with"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"""CREATE FUNCTION technical_evidence_ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '{MESSAGE}'; END $$""")
        op.execute(f"CREATE TRIGGER technical_evidence_ledger_no_update_delete BEFORE UPDATE OR DELETE ON {TABLE} "
                   "FOR EACH ROW EXECUTE FUNCTION technical_evidence_ledger_append_only()")
        op.execute(f"CREATE TRIGGER technical_evidence_ledger_no_truncate BEFORE TRUNCATE ON {TABLE} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION technical_evidence_ledger_append_only()")
    else:  # SQLite (tests/dev only): no TRUNCATE statement exists.
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER technical_evidence_ledger_no_{action.lower()} BEFORE {action} ON {TABLE} "
                       f"BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END")


def downgrade():
    rows = op.get_bind().execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar_one()
    if rows:
        raise RuntimeError(f"refusing to downgrade: {TABLE} contains {rows} evidence rows (append-only; export first; "
                           "no override exists)")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS technical_evidence_ledger_no_truncate ON {TABLE}")
        op.execute(f"DROP TRIGGER IF EXISTS technical_evidence_ledger_no_update_delete ON {TABLE}")
        op.execute("DROP FUNCTION IF EXISTS technical_evidence_ledger_append_only()")
    else:
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS technical_evidence_ledger_no_{action}")
    for name in ("ix_technical_evidence_ledger_conflicts_with", "ix_technical_evidence_ledger_collected_at",
                 "ix_technical_evidence_ledger_status", "ix_technical_evidence_ledger_session",
                 "uq_technical_evidence_ledger_conflict", "uq_technical_evidence_ledger_accepted"):
        op.drop_index(name, table_name=TABLE)
    op.drop_table(TABLE)
