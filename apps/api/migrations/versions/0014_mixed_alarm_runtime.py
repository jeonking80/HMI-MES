"""Gate the mixed coordinator and index applied logical Line fault evidence."""

from alembic import op

revision = "0014_mixed_alarm_runtime"
down_revision = "0013_work_order_cancel"
branch_labels = None
depends_on = None


def upgrade():
    # env.py already owns an exclusive transactional SCHEMA_LOCK_ID lock.
    # Do not use CONCURRENTLY/autocommit: DDL and revision must roll back together.
    op.execute("""CREATE INDEX ix_inbox_applied_line_fault_seq
        ON event_inbox(source_id,stream_id,seq DESC)
        WHERE applied_at IS NOT NULL AND kind='FAULT_CHANGED'
            AND body->>'equipment_id'='Line'""")


def downgrade():
    # Even legacy Sealer-only ownership prevents re-enabling an old binary that
    # cannot enforce the complete durable source roster. No implicit retirement.
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM alarm_capture_frontiers) OR
           EXISTS(SELECT 1 FROM alarm_capture_decisions) THEN
            RAISE EXCEPTION 'Used mixed alarm runtime gate cannot be downgraded';
        END IF;
    END $$""")
    op.drop_index("ix_inbox_applied_line_fault_seq", table_name="event_inbox")
