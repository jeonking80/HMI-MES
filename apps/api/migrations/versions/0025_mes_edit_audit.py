"""Audited in-place edits of unbound work orders and recipe archiving.

Only the MES audit action vocabulary changes; every existing row stays valid.
"""

from alembic import op

revision = "0025_mes_edit_audit"
down_revision = "0024_ingest_retired_segments"
branch_labels = None
depends_on = None

_ORDER_ACTIONS = "'REGISTER_WORK_ORDER','RELEASE_WORK_ORDER','BIND_EXECUTION','CANCEL_WORK_ORDER'"


def _statements(order_actions, recipe_actions):
    return (
        "ALTER TABLE public.mes_management_audit DROP CONSTRAINT mes_management_audit_action_check",
        "ALTER TABLE public.mes_management_audit DROP CONSTRAINT mes_management_audit_check",
        "ALTER TABLE public.mes_management_audit ADD CONSTRAINT "
        f"mes_management_audit_action_check CHECK (action IN ({recipe_actions},{order_actions}))",
        "ALTER TABLE public.mes_management_audit ADD CONSTRAINT mes_management_audit_check CHECK ("
        f"(action IN ({recipe_actions}) AND recipe_version_id IS NOT NULL "
        "AND work_order_id IS NULL) OR "
        f"(action IN ({order_actions}) AND work_order_id IS NOT NULL "
        "AND recipe_version_id IS NULL))",
    )


def upgrade_statements():
    return _statements(_ORDER_ACTIONS + ",'EDIT_WORK_ORDER'", "'REGISTER_RECIPE','ARCHIVE_RECIPE'")


def upgrade():
    for statement in upgrade_statements():
        op.execute(statement)


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM public.mes_management_audit
                  WHERE action IN ('EDIT_WORK_ORDER','ARCHIVE_RECIPE')) THEN
            RAISE EXCEPTION 'Recorded edits or archives need a forward migration';
        END IF;
    END $$""")
    for statement in _statements(_ORDER_ACTIONS, "'REGISTER_RECIPE'"):
        op.execute(statement)
