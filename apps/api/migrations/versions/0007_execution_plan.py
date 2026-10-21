"""Immutable execution target quantity; prior executions remain explicitly unplanned."""

from alembic import op

revision = "0007_execution_plan"
down_revision = "0006_settlement"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE execution_plans (
        execution_id TEXT PRIMARY KEY REFERENCES production_executions(execution_id)
            ON DELETE RESTRICT,
        target_quantity BIGINT NOT NULL CHECK (target_quantity BETWEEN 1 AND 4294967295)
    )""")


def downgrade():
    op.drop_table("execution_plans")
