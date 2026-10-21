"""Explicit release receipts and atomic managed execution reservations."""

from alembic import op

revision = "0009_work_order_binding"
down_revision = "0008_work_orders"
branch_labels = None
depends_on = None

_DDL = (
    "ALTER TABLE work_orders DROP CONSTRAINT work_orders_status_check",
    "ALTER TABLE work_orders ADD CONSTRAINT work_orders_status_check CHECK (status "
    "IN ('DRAFT','RELEASED'))",
    "ALTER TABLE work_orders ADD CONSTRAINT uq_work_order_line UNIQUE(id,line_id)",
    "ALTER TABLE source_streams ADD CONSTRAINT uq_binding_source_line "
    "UNIQUE(source_id,stream_id,line_id)",
    """ALTER TABLE production_executions ADD CONSTRAINT uq_binding_execution
        UNIQUE(execution_id,work_order_id,source_id,stream_id)""",
    """ALTER TABLE command_requests ADD CONSTRAINT uq_binding_command
        UNIQUE(command_id,kind,execution_id,line_id,source_id,stream_id)""",
    "ALTER TABLE mes_management_audit DROP CONSTRAINT mes_management_audit_action_check",
    "ALTER TABLE mes_management_audit DROP CONSTRAINT mes_management_audit_check",
    """ALTER TABLE mes_management_audit ADD CONSTRAINT mes_management_audit_action_check
        CHECK (action IN ('REGISTER_RECIPE','REGISTER_WORK_ORDER',
            'RELEASE_WORK_ORDER','BIND_EXECUTION'))""",
    """ALTER TABLE mes_management_audit ADD CONSTRAINT mes_management_audit_check CHECK (
        (action='REGISTER_RECIPE' AND recipe_version_id IS NOT NULL AND work_order_id IS NULL)
        OR (action IN ('REGISTER_WORK_ORDER','RELEASE_WORK_ORDER','BIND_EXECUTION')
            AND work_order_id IS NOT NULL AND recipe_version_id IS NULL))""",
    """CREATE TABLE work_order_releases (
        release_id TEXT PRIMARY KEY CHECK (release_id ~ '^[^[:space:]]{1,128}$'),
        work_order_id TEXT NOT NULL UNIQUE REFERENCES work_orders(id) ON DELETE RESTRICT,
        actor TEXT NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        expected_row_version NUMERIC(20,0) NOT NULL CHECK
            (expected_row_version BETWEEN 1 AND 18446744073709551615),
        original_intent JSONB NOT NULL, receipt JSONB NOT NULL,
        release_event_id BIGINT NOT NULL UNIQUE REFERENCES mes_management_audit(id) ON
            DELETE RESTRICT,
        released_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE work_order_execution_links (
        execution_id TEXT PRIMARY KEY,
        work_order_id TEXT NOT NULL UNIQUE,
        line_id TEXT NOT NULL, source_id TEXT NOT NULL, stream_id TEXT NOT NULL,
        load_command_id TEXT NOT NULL UNIQUE,
        load_kind TEXT NOT NULL DEFAULT 'LOAD_JOB' CHECK (load_kind='LOAD_JOB'),
        original_intent JSONB NOT NULL, original_request JSONB NOT NULL, receipt JSONB NOT NULL,
        bound_by TEXT NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        bound_at TIMESTAMPTZ NOT NULL,
        UNIQUE(line_id,execution_id),
        FOREIGN KEY(work_order_id,line_id) REFERENCES work_orders(id,line_id) ON DELETE RESTRICT,
        FOREIGN KEY(source_id,stream_id,line_id) REFERENCES
            source_streams(source_id,stream_id,line_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(execution_id,work_order_id,source_id,stream_id)
            REFERENCES production_executions(execution_id,work_order_id,source_id,stream_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(load_command_id,load_kind,execution_id,line_id,source_id,stream_id)
            REFERENCES command_requests(command_id,kind,execution_id,line_id,source_id,stream_id)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE line_execution_reservations (
        line_id TEXT PRIMARY KEY REFERENCES lines(id) ON DELETE RESTRICT,
        execution_id TEXT NOT NULL UNIQUE,
        FOREIGN KEY(line_id,execution_id) REFERENCES
            work_order_execution_links(line_id,execution_id)
            ON DELETE RESTRICT
    )""",
)


def upgrade():
    for sql in _DDL:
        op.execute(sql)


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM work_order_releases)
            OR EXISTS (SELECT 1 FROM work_order_execution_links)
            OR EXISTS (SELECT 1 FROM line_execution_reservations)
            OR EXISTS (SELECT 1 FROM work_orders WHERE status<>'DRAFT')
            OR EXISTS (SELECT 1 FROM mes_management_audit
                WHERE action IN ('RELEASE_WORK_ORDER','BIND_EXECUTION'))
        THEN RAISE EXCEPTION 'Used work-order workflow cannot be downgraded'; END IF;
    END $$""")
    for name in (
        "line_execution_reservations",
        "work_order_execution_links",
        "work_order_releases",
    ):
        op.drop_table(name)
    for table, constraint in (
        ("command_requests", "uq_binding_command"),
        ("production_executions", "uq_binding_execution"),
        ("source_streams", "uq_binding_source_line"),
        ("work_orders", "uq_work_order_line"),
    ):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
    op.execute("ALTER TABLE work_orders DROP CONSTRAINT work_orders_status_check")
    op.execute(
        "ALTER TABLE work_orders ADD CONSTRAINT work_orders_status_check CHECK (status='DRAFT')"
    )
    op.execute("ALTER TABLE mes_management_audit DROP CONSTRAINT mes_management_audit_action_check")
    op.execute("ALTER TABLE mes_management_audit DROP CONSTRAINT mes_management_audit_check")
    op.execute("""ALTER TABLE mes_management_audit ADD CONSTRAINT mes_management_audit_action_check
        CHECK (action IN ('REGISTER_RECIPE','REGISTER_WORK_ORDER'))""")
    op.execute("""ALTER TABLE mes_management_audit ADD CONSTRAINT mes_management_audit_check CHECK (
        (action='REGISTER_RECIPE' AND recipe_version_id IS NOT NULL AND work_order_id IS NULL)
        OR (action='REGISTER_WORK_ORDER' AND work_order_id IS NOT NULL
            AND recipe_version_id IS NULL))""")
