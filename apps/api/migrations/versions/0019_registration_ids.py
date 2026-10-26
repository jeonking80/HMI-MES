"""Transactional server-issued registration IDs and immutable retry receipts."""

from alembic import op

revision = "0019_registration_ids"
down_revision = "0018_clock_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE registration_id_counters (
        kind text NOT NULL CHECK(kind IN ('recipe','work_order')),
        namespace text NOT NULL CHECK(namespace ~ '^[^[:space:]]{1,128}$'),
        last_value numeric(20,0) NOT NULL
            CHECK(last_value BETWEEN 1 AND 9223372036854775807),
        PRIMARY KEY(kind,namespace)
    )""")
    op.execute("""CREATE TABLE registration_receipts (
        kind text NOT NULL CHECK(kind IN ('recipe','work_order')),
        request_id text NOT NULL CHECK(request_id ~ '^[^[:space:]]{1,128}$'),
        actor text NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        original_request jsonb NOT NULL CHECK(jsonb_typeof(original_request)='object'),
        response jsonb NOT NULL CHECK(jsonb_typeof(response)='object'),
        recipe_version_id text REFERENCES recipe_versions(id) ON DELETE RESTRICT,
        work_order_id text REFERENCES work_orders(id) ON DELETE RESTRICT,
        created_at timestamptz NOT NULL,
        PRIMARY KEY(kind,request_id),
        CHECK((kind='recipe' AND recipe_version_id IS NOT NULL AND work_order_id IS NULL)
            OR (kind='work_order' AND work_order_id IS NOT NULL AND recipe_version_id IS NULL))
    )""")
    op.execute("""CREATE FUNCTION immutable_registration_receipt() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Registration receipts are immutable';
        END $$""")
    op.execute("""CREATE TRIGGER registration_receipts_immutable
        BEFORE UPDATE OR DELETE ON registration_receipts FOR EACH ROW
        EXECUTE FUNCTION immutable_registration_receipt()""")
    op.execute("""CREATE TRIGGER registration_receipts_no_truncate
        BEFORE TRUNCATE ON registration_receipts FOR EACH STATEMENT
        EXECUTE FUNCTION immutable_registration_receipt()""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM registration_id_counters) OR
            EXISTS(SELECT 1 FROM registration_receipts) THEN
            RAISE EXCEPTION 'Used registration IDs and receipts cannot be downgraded';
        END IF;
    END $$""")
    op.drop_table("registration_receipts")
    op.drop_table("registration_id_counters")
    op.execute("DROP FUNCTION immutable_registration_receipt()")
