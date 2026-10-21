"""Immutable final source evidence and explicit MES reconciliation state."""

from alembic import op

revision = "0006_settlement"
down_revision = "0005_command_dispatch"
branch_labels = None
depends_on = None

_DDL = (
    """CREATE TABLE run_reconciliations (
        execution_id TEXT PRIMARY KEY REFERENCES production_executions(execution_id)
            ON DELETE RESTRICT,
        final_summary JSONB NOT NULL,
        canonical_final TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PENDING','SETTLED','MISMATCH','CONFLICT')),
        reason_code TEXT NOT NULL,
        received_at TIMESTAMPTZ NOT NULL,
        checked_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE run_summary_conflicts (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        execution_id TEXT NOT NULL REFERENCES run_reconciliations(execution_id)
            ON DELETE RESTRICT,
        summary JSONB NOT NULL,
        canonical_final TEXT NOT NULL,
        received_at TIMESTAMPTZ NOT NULL,
        UNIQUE(execution_id,canonical_final)
    )""",
)


def upgrade():
    for statement in _DDL:
        op.execute(statement)


def downgrade():
    op.drop_table("run_summary_conflicts")
    op.drop_table("run_reconciliations")
