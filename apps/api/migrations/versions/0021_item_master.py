"""Immutable item definitions and an explicit legacy item backfill."""

from alembic import op

revision = "0021_item_master"
down_revision = "0020_line_queue"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE item_master (
        id text PRIMARY KEY CHECK(id ~ '^[^[:space:]]{1,128}$'),
        name text NOT NULL CHECK(char_length(name) BETWEEN 1 AND 128
            AND name ~ '[^[:space:]]'),
        unit text NOT NULL CHECK(char_length(unit) BETWEEN 1 AND 32
            AND unit ~ '[^[:space:]]'),
        enabled boolean NOT NULL DEFAULT true,
        origin text NOT NULL CHECK(origin IN ('USER','MIGRATION')),
        created_by text REFERENCES users(login) ON DELETE RESTRICT,
        created_at timestamptz NOT NULL,
        CHECK((origin='USER' AND created_by IS NOT NULL)
            OR (origin='MIGRATION' AND created_by IS NULL))
    )""")
    op.execute("""INSERT INTO item_master
        (id,name,unit,enabled,origin,created_by,created_at)
        SELECT item_id,item_id,'EA',true,'MIGRATION',NULL,CURRENT_TIMESTAMP
        FROM (SELECT item_id FROM recipe_versions
            UNION SELECT item_id FROM work_orders) AS legacy_items""")
    op.execute("""ALTER TABLE recipe_versions ADD CONSTRAINT fk_recipe_versions_item_master
        FOREIGN KEY(item_id) REFERENCES item_master(id) ON DELETE RESTRICT""")
    op.execute("""ALTER TABLE work_orders ADD CONSTRAINT fk_work_orders_item_master
        FOREIGN KEY(item_id) REFERENCES item_master(id) ON DELETE RESTRICT""")
    op.execute("""CREATE TABLE item_audit (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        item_id text NOT NULL REFERENCES item_master(id) ON DELETE RESTRICT,
        actor text NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        action text NOT NULL CHECK(action='CREATE'),
        occurred_at timestamptz NOT NULL,
        evidence jsonb NOT NULL CHECK(jsonb_typeof(evidence)='object')
    )""")
    op.execute("CREATE INDEX ix_item_audit_item ON item_audit(item_id,id)")
    op.execute("""CREATE FUNCTION immutable_item_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Item definitions and audit evidence are immutable';
        END $$""")
    for table in ("item_master", "item_audit"):
        op.execute(f"""CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW
            EXECUTE FUNCTION immutable_item_evidence()""")
        op.execute(f"""CREATE TRIGGER {table}_no_truncate
            BEFORE TRUNCATE ON {table} FOR EACH STATEMENT
            EXECUTE FUNCTION immutable_item_evidence()""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM item_master) OR EXISTS(SELECT 1 FROM item_audit)
            OR EXISTS(SELECT 1 FROM recipe_versions) OR EXISTS(SELECT 1 FROM work_orders) THEN
            RAISE EXCEPTION 'Used item definitions cannot be downgraded; restore a backup';
        END IF;
    END $$""")
    op.execute("ALTER TABLE work_orders DROP CONSTRAINT fk_work_orders_item_master")
    op.execute("ALTER TABLE recipe_versions DROP CONSTRAINT fk_recipe_versions_item_master")
    op.drop_table("item_audit")
    op.drop_table("item_master")
    op.execute("DROP FUNCTION immutable_item_evidence()")
