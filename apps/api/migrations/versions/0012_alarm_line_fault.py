"""Logical line fault channels without inventing a physical Line asset."""

from alembic import op

revision = "0012_alarm_line_fault"
down_revision = "0011_alarm_capture_context"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE alarm_channels DROP CONSTRAINT alarm_channels_asset_id_check")
    op.execute("ALTER TABLE alarm_channels DROP CONSTRAINT alarm_channels_line_id_asset_id_fkey")
    op.execute(
        "ALTER TABLE alarm_channels ADD CONSTRAINT ck_alarm_target "
        "CHECK(asset_id IN ('Line','Sealer'))"
    )
    op.execute(
        "ALTER TABLE alarm_channels ADD COLUMN physical_asset_code TEXT GENERATED ALWAYS AS "
        "(CASE WHEN asset_id='Line' THEN NULL ELSE asset_id END) STORED"
    )
    # The existing source/stream/line FK still binds the registered logical line.
    # MATCH SIMPLE intentionally exempts only Line; Sealer keeps its asset FK.
    op.execute(
        "ALTER TABLE alarm_channels ADD CONSTRAINT fk_alarm_physical_asset "
        "FOREIGN KEY(line_id,physical_asset_code) REFERENCES assets(line_id,code) "
        "ON DELETE RESTRICT"
    )


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM alarm_channels WHERE asset_id='Line') OR
           EXISTS(SELECT 1 FROM alarm_rule_versions WHERE config->>'kind'='PLC_FAULT') THEN
            RAISE EXCEPTION 'Used logical line fault channels cannot be downgraded';
        END IF;
    END $$""")
    op.execute("ALTER TABLE alarm_channels DROP CONSTRAINT fk_alarm_physical_asset")
    op.execute("ALTER TABLE alarm_channels DROP COLUMN physical_asset_code")
    op.execute("ALTER TABLE alarm_channels DROP CONSTRAINT ck_alarm_target")
    op.execute(
        "ALTER TABLE alarm_channels ADD CONSTRAINT alarm_channels_asset_id_check "
        "CHECK(asset_id='Sealer')"
    )
    op.execute(
        "ALTER TABLE alarm_channels ADD CONSTRAINT alarm_channels_line_id_asset_id_fkey "
        "FOREIGN KEY(line_id,asset_id) REFERENCES assets(line_id,code) ON DELETE RESTRICT"
    )
