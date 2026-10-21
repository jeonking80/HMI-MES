"""Explicit cancellation of work orders with no managed execution link."""

from alembic import op

revision = "0013_work_order_cancel"
down_revision = "0012_alarm_line_fault"
branch_labels = None
depends_on = None


def _audit_checks(actions):
    op.execute("ALTER TABLE mes_management_audit DROP CONSTRAINT mes_management_audit_action_check")
    op.execute("ALTER TABLE mes_management_audit DROP CONSTRAINT mes_management_audit_check")
    quoted = ",".join("'" + action + "'" for action in actions)
    op.execute(
        "ALTER TABLE mes_management_audit ADD CONSTRAINT mes_management_audit_action_check "
        f"CHECK(action IN ('REGISTER_RECIPE',{quoted}))"
    )
    op.execute(
        "ALTER TABLE mes_management_audit ADD CONSTRAINT mes_management_audit_check CHECK("
        "(action='REGISTER_RECIPE' AND recipe_version_id IS NOT NULL AND work_order_id IS NULL) "
        f"OR(action IN ({quoted}) AND work_order_id IS NOT NULL AND recipe_version_id IS NULL))"
    )


def upgrade():
    op.execute("ALTER TABLE work_orders DROP CONSTRAINT work_orders_status_check")
    op.execute(
        "ALTER TABLE work_orders ADD CONSTRAINT work_orders_status_check "
        "CHECK(status IN ('DRAFT','RELEASED','CANCELLED'))"
    )
    _audit_checks(
        ("REGISTER_WORK_ORDER", "RELEASE_WORK_ORDER", "BIND_EXECUTION", "CANCEL_WORK_ORDER")
    )
    op.execute("""CREATE TABLE work_order_cancellations (
        cancellation_id TEXT PRIMARY KEY CHECK(cancellation_id ~ '^[^[:space:]]{1,128}$'),
        work_order_id TEXT NOT NULL UNIQUE REFERENCES work_orders(id) ON DELETE RESTRICT,
        actor TEXT NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        expected_row_version NUMERIC(20,0) NOT NULL CHECK
            (expected_row_version BETWEEN 1 AND 18446744073709551615),
        original_intent JSONB NOT NULL, receipt JSONB NOT NULL,
        cancel_event_id BIGINT NOT NULL UNIQUE REFERENCES mes_management_audit(id)
            ON DELETE RESTRICT,
        cancelled_at TIMESTAMPTZ NOT NULL
    )""")
    op.execute("""CREATE FUNCTION immutable_work_order_cancellation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'Original work order cancellations are immutable'; END $$""")
    op.execute("""CREATE TRIGGER immutable_work_order_cancellation
        BEFORE UPDATE OR DELETE ON work_order_cancellations FOR EACH ROW
        EXECUTE FUNCTION immutable_work_order_cancellation()""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM work_order_cancellations) OR
           EXISTS(SELECT 1 FROM work_orders WHERE status='CANCELLED') OR
           EXISTS(SELECT 1 FROM mes_management_audit WHERE action='CANCEL_WORK_ORDER') THEN
            RAISE EXCEPTION 'Used work order cancellation cannot be downgraded';
        END IF;
    END $$""")
    op.drop_table("work_order_cancellations")
    op.execute("DROP FUNCTION immutable_work_order_cancellation()")
    _audit_checks(("REGISTER_WORK_ORDER", "RELEASE_WORK_ORDER", "BIND_EXECUTION"))
    op.execute("ALTER TABLE work_orders DROP CONSTRAINT work_orders_status_check")
    op.execute(
        "ALTER TABLE work_orders ADD CONSTRAINT work_orders_status_check "
        "CHECK(status IN ('DRAFT','RELEASED'))"
    )
