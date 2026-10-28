"""Reusable work orders: one order, many runs.

Before this revision a work order could own one execution ever, so a finished
order could never be loaded again. Each load now links a new execution to the
same order; earlier runs stay as their own evidence. Only one waiting queue
entry per order is kept unique; bound entries are history.
"""

from alembic import op

revision = "0028_work_order_reuse"
down_revision = "0027_recipe_pressure"
branch_labels = None
depends_on = None


def upgrade_statements():
    return (
        "ALTER TABLE public.work_order_execution_links "
        "DROP CONSTRAINT work_order_execution_links_work_order_id_key",
        "CREATE INDEX ix_work_order_execution_links_order "
        "ON public.work_order_execution_links(work_order_id,bound_at)",
        "DROP INDEX public.ux_line_queue_entries_active_order",
        "CREATE UNIQUE INDEX ux_line_queue_entries_waiting_order "
        "ON public.line_queue_entries(work_order_id) WHERE state='WAITING'",
    )


def upgrade():
    for statement in upgrade_statements():
        op.execute(statement)


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM public.work_order_execution_links
                  GROUP BY work_order_id HAVING count(*) > 1)
           OR EXISTS(SELECT 1 FROM public.line_queue_entries WHERE state <> 'REMOVED'
                  GROUP BY work_order_id HAVING count(*) > 1) THEN
            RAISE EXCEPTION 'Reused work orders need a forward migration';
        END IF;
    END $$""")
    op.execute("DROP INDEX public.ux_line_queue_entries_waiting_order")
    op.execute(
        "CREATE UNIQUE INDEX ux_line_queue_entries_active_order "
        "ON public.line_queue_entries(work_order_id) WHERE state <> 'REMOVED'"
    )
    op.execute("DROP INDEX public.ix_work_order_execution_links_order")
    op.execute(
        "ALTER TABLE public.work_order_execution_links "
        "ADD CONSTRAINT work_order_execution_links_work_order_id_key UNIQUE (work_order_id)"
    )
