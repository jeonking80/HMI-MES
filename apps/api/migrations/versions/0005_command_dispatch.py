"""Command reconciliation audit evidence and durable polling fairness."""

from alembic import op

revision = "0005_command_dispatch"
down_revision = "0004_commands"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE command_requests ADD COLUMN last_polled_at TIMESTAMPTZ")
    op.execute("ALTER TABLE command_requests ADD COLUMN last_poll_order BIGINT NOT NULL DEFAULT 0")
    op.execute(
        "ALTER TABLE command_requests ADD COLUMN collision_latched BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE command_request_audit ADD COLUMN evidence JSONB NOT NULL DEFAULT '{}'")
    op.execute(
        "ALTER TABLE command_request_audit DROP CONSTRAINT command_request_audit_action_check"
    )
    op.execute("""ALTER TABLE command_request_audit
        ADD CONSTRAINT command_request_audit_action_check
        CHECK (action IN ('REQUESTED','EXPIRED','DISPATCH_RESERVED','QUERY',
                          'SUBMISSION_RECORDED','RESULT_RECORDED','RESULT_NOT_FOUND'))""")
    op.execute("CREATE INDEX ix_commands_line_status ON command_requests(line_id,status)")


def downgrade():
    # Preserve all journal/audit rows. Downgrade after using dispatch is refused
    # by this CHECK instead of silently deleting its evidence.
    op.execute(
        "ALTER TABLE command_request_audit DROP CONSTRAINT command_request_audit_action_check"
    )
    op.execute("""ALTER TABLE command_request_audit
        ADD CONSTRAINT command_request_audit_action_check
        CHECK (action IN ('REQUESTED','EXPIRED'))""")
    op.execute("DROP INDEX ix_commands_line_status")
    op.execute("ALTER TABLE command_request_audit DROP COLUMN evidence")
    op.execute("ALTER TABLE command_requests DROP COLUMN last_polled_at")
    op.execute("ALTER TABLE command_requests DROP COLUMN last_poll_order")
    op.execute("ALTER TABLE command_requests DROP COLUMN collision_latched")
