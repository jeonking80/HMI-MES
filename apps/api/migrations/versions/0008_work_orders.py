"""Immutable recipe versions and draft-only MES work-order registration."""

from alembic import op

revision = "0008_work_orders"
down_revision = "0007_execution_plan"
branch_labels = None
depends_on = None

_DDL = (
    """CREATE TABLE recipe_versions (
        id TEXT PRIMARY KEY CHECK (id ~ '^[^[:space:]]{1,128}$'),
        recipe_id TEXT NOT NULL CHECK (recipe_id ~ '^[^[:space:]]{1,128}$'),
        version TEXT NOT NULL CHECK (version ~ '^[^[:space:]]{1,128}$'),
        item_id TEXT NOT NULL CHECK (item_id ~ '^[^[:space:]]{1,128}$'),
        temperature_c DOUBLE PRECISION NOT NULL
            CHECK (temperature_c >= 0 AND temperature_c < 'Infinity'::float8),
        speed_m_per_min DOUBLE PRECISION NOT NULL
            CHECK (speed_m_per_min > 0 AND speed_m_per_min < 'Infinity'::float8),
        created_by TEXT NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE(recipe_id,version), UNIQUE(id,item_id)
    )""",
    """CREATE TABLE work_orders (
        id TEXT PRIMARY KEY CHECK (id ~ '^[^[:space:]]{1,128}$'),
        item_id TEXT NOT NULL CHECK (item_id ~ '^[^[:space:]]{1,128}$'),
        line_id TEXT NOT NULL REFERENCES lines(id) ON DELETE RESTRICT,
        recipe_version_id TEXT NOT NULL,
        target_quantity BIGINT NOT NULL CHECK (target_quantity BETWEEN 1 AND 4294967295),
        planned_date DATE NOT NULL,
        status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status='DRAFT'),
        row_version NUMERIC(20,0) NOT NULL DEFAULT 1
            CHECK (row_version BETWEEN 1 AND 18446744073709551615),
        created_by TEXT NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL,
        FOREIGN KEY(recipe_version_id,item_id) REFERENCES recipe_versions(id,item_id)
            ON DELETE RESTRICT
    )""",
    "CREATE INDEX ix_work_orders_line_status_date ON work_orders(line_id,status,planned_date,id)",
    """CREATE TABLE mes_management_audit (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        actor TEXT NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        action TEXT NOT NULL CHECK (action IN ('REGISTER_RECIPE','REGISTER_WORK_ORDER')),
        recipe_version_id TEXT REFERENCES recipe_versions(id) ON DELETE RESTRICT,
        work_order_id TEXT REFERENCES work_orders(id) ON DELETE RESTRICT,
        occurred_at TIMESTAMPTZ NOT NULL,
        evidence JSONB NOT NULL,
        CHECK ((action='REGISTER_RECIPE' AND recipe_version_id IS NOT NULL
                AND work_order_id IS NULL)
            OR (action='REGISTER_WORK_ORDER' AND work_order_id IS NOT NULL
                AND recipe_version_id IS NULL))
    )""",
    "CREATE INDEX ix_mes_audit_recipe ON mes_management_audit(recipe_version_id,id)",
    "CREATE INDEX ix_mes_audit_work_order ON mes_management_audit(work_order_id,id)",
)


def upgrade():
    for statement in _DDL:
        op.execute(statement)


def downgrade():
    op.drop_table("mes_management_audit")
    op.drop_table("work_orders")
    op.drop_table("recipe_versions")
