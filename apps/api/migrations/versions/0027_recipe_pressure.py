"""Sealing air pressure as an optional recipe setpoint.

Recipes made before this revision keep NULL: their jobs load without a pressure
and the line seals at supply pressure, exactly as before.
"""

from alembic import op

revision = "0027_recipe_pressure"
down_revision = "0026_queue_reenroll"
branch_labels = None
depends_on = None


def upgrade_statements():
    return (
        "ALTER TABLE public.recipe_versions ADD COLUMN air_pressure_bar double precision "
        "CONSTRAINT recipe_versions_air_pressure_bar_check CHECK (air_pressure_bar IS NULL OR "
        "(air_pressure_bar > (0)::double precision "
        "AND air_pressure_bar < 'Infinity'::double precision))",
    )


def upgrade():
    for statement in upgrade_statements():
        op.execute(statement)


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM public.recipe_versions WHERE air_pressure_bar IS NOT NULL) THEN
            RAISE EXCEPTION 'Recipes with a pressure need a forward migration';
        END IF;
    END $$""")
    op.execute("ALTER TABLE public.recipe_versions DROP COLUMN air_pressure_bar")
