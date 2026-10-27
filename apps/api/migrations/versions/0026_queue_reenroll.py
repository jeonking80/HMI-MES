"""Re-enroll an edited work order in a line queue as a new immutable entry.

Queue entries keep their enrolled work-order version immutable. Before this
revision a work order could own one entry ever, so an order removed from the
queue, edited (new row version) and queued again kept its stale version and
auto-advance stopped on it. Only one active (WAITING/BOUND) entry per work
order is now required; REMOVED entries stay as enrollment evidence.
"""

from alembic import op

revision = "0026_queue_reenroll"
down_revision = "0025_mes_edit_audit"
branch_labels = None
depends_on = None


def upgrade_statements():
    return (
        "ALTER TABLE public.line_queue_entries "
        "DROP CONSTRAINT line_queue_entries_work_order_id_key",
        "CREATE UNIQUE INDEX ux_line_queue_entries_active_order "
        "ON public.line_queue_entries(work_order_id) WHERE state <> 'REMOVED'",
        "CREATE INDEX ix_line_queue_entries_work_order ON public.line_queue_entries(work_order_id)",
    )


def upgrade():
    for statement in upgrade_statements():
        op.execute(statement)


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM public.line_queue_entries
                  GROUP BY work_order_id HAVING count(*) > 1) THEN
            RAISE EXCEPTION 'Re-enrolled queue entries need a forward migration';
        END IF;
    END $$""")
    op.execute("DROP INDEX public.ix_line_queue_entries_work_order")
    op.execute("DROP INDEX public.ux_line_queue_entries_active_order")
    op.execute(
        "ALTER TABLE public.line_queue_entries "
        "ADD CONSTRAINT line_queue_entries_work_order_id_key UNIQUE (work_order_id)"
    )
