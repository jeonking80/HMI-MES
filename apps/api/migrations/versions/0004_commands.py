"""Durable API command requests and actor audit; no dispatch implementation."""

from alembic import op

revision = "0004_commands"
down_revision = "0003_user_auth"
branch_labels = None
depends_on = None

_DDL = (
    """CREATE TABLE command_requests (
        command_id TEXT PRIMARY KEY CHECK
            (length(command_id) BETWEEN 1 AND 128 AND command_id !~ '[[:space:]]'),
        line_id TEXT NOT NULL REFERENCES lines(id) ON DELETE RESTRICT,
        source_id TEXT NOT NULL,
        stream_id TEXT NOT NULL,
        target_id TEXT NOT NULL CHECK (target_id='Line'),
        execution_id TEXT REFERENCES production_executions(execution_id) ON DELETE RESTRICT,
        actor TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN
            ('START','PAUSE','RESET_FAULT','LOAD_JOB','END_RUN','SET_PARAMETERS')),
        request JSONB NOT NULL,
        canonical_payload TEXT NOT NULL,
        result JSONB NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('REQUESTED','DELIVERED','ACCEPTED','SUCCEEDED','FAILED',
             'REJECTED','EXPIRED','OUTCOME_UNKNOWN')),
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        FOREIGN KEY(source_id,stream_id)
            REFERENCES source_streams(source_id,stream_id) ON DELETE RESTRICT,
        CHECK (result->>'command_id'=command_id AND result->>'status'=status),
        CHECK (request->>'command_id'=command_id AND request->>'requested_by'=actor)
    )""",
    "CREATE INDEX ix_commands_stream_status ON command_requests(source_id,stream_id,status)",
    """CREATE TABLE command_request_audit (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        command_id TEXT NOT NULL REFERENCES command_requests(command_id) ON DELETE RESTRICT,
        actor TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('REQUESTED','EXPIRED')),
        occurred_at TIMESTAMP WITH TIME ZONE NOT NULL
    )""",
    "CREATE INDEX ix_command_audit_command ON command_request_audit(command_id)",
)


def upgrade():
    for statement in _DDL:
        op.execute(statement)


def downgrade():
    op.drop_table("command_request_audit")
    op.drop_table("command_requests")
