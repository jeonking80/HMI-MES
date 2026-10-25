"""Explicit per-line queue policy and durable, actor-bound intent receipts."""

from alembic import op

revision = "0020_line_queue"
down_revision = "0019_registration_ids"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE line_queue_policies (
        line_id text PRIMARY KEY REFERENCES lines(id) ON DELETE RESTRICT,
        revision numeric(20,0) NOT NULL DEFAULT 0
            CHECK(revision BETWEEN 0 AND 18446744073709551615),
        source_id text, stream_id text, gateway_id text,
        capture_generation numeric(20,0)
            CHECK(capture_generation BETWEEN 0 AND 18446744073709551615),
        auto_advance boolean NOT NULL DEFAULT false,
        enabled_by text REFERENCES users(login) ON DELETE RESTRICT,
        updated_at timestamptz NOT NULL,
        blocked_reason text,
        FOREIGN KEY(source_id,stream_id) REFERENCES source_streams(source_id,stream_id)
            ON DELETE RESTRICT,
        CHECK(NOT auto_advance OR (source_id IS NOT NULL AND stream_id IS NOT NULL
            AND gateway_id IS NOT NULL AND capture_generation IS NOT NULL
            AND enabled_by IS NOT NULL))
    )""")
    op.execute("""CREATE TABLE line_queue_entries (
        entry_id text PRIMARY KEY CHECK(entry_id ~ '^[^[:space:]]{1,128}$'),
        line_id text NOT NULL REFERENCES lines(id) ON DELETE RESTRICT,
        position integer NOT NULL CHECK(position >= 0),
        work_order_id text NOT NULL UNIQUE REFERENCES work_orders(id) ON DELETE RESTRICT,
        expected_row_version numeric(20,0) NOT NULL
            CHECK(expected_row_version BETWEEN 1 AND 18446744073709551615),
        load_command_id text NOT NULL UNIQUE
            CHECK(load_command_id ~ '^[^[:space:]]{1,128}$'),
        state text NOT NULL DEFAULT 'WAITING' CHECK(state IN ('WAITING','BOUND','REMOVED')),
        binding_receipt jsonb CHECK(jsonb_typeof(binding_receipt)='object'),
        created_at timestamptz NOT NULL,
        CHECK(state <> 'BOUND' OR binding_receipt IS NOT NULL)
    )""")
    op.execute("""CREATE INDEX ix_line_queue_entries_order
        ON line_queue_entries(line_id,state,position,entry_id)""")
    op.execute("""CREATE FUNCTION preserve_line_queue_entry() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP IN ('DELETE','TRUNCATE') THEN
                RAISE EXCEPTION 'Line queue enrollment evidence cannot be erased';
            END IF;
            IF ROW(NEW.entry_id,NEW.line_id,NEW.work_order_id,NEW.expected_row_version,
                NEW.load_command_id,NEW.created_at) IS DISTINCT FROM
                ROW(OLD.entry_id,OLD.line_id,OLD.work_order_id,OLD.expected_row_version,
                    OLD.load_command_id,OLD.created_at) THEN
                RAISE EXCEPTION 'Line queue enrollment identity is immutable';
            END IF;
            IF OLD.state='BOUND' AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Bound line queue evidence is immutable';
            END IF;
            IF OLD.binding_receipt IS NOT NULL
                AND NEW.binding_receipt IS DISTINCT FROM OLD.binding_receipt THEN
                RAISE EXCEPTION 'Line queue binding receipt is immutable';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER line_queue_entries_preserve
        BEFORE UPDATE OR DELETE ON line_queue_entries FOR EACH ROW
        EXECUTE FUNCTION preserve_line_queue_entry()""")
    op.execute("""CREATE TRIGGER line_queue_entries_no_truncate
        BEFORE TRUNCATE ON line_queue_entries FOR EACH STATEMENT
        EXECUTE FUNCTION preserve_line_queue_entry()""")
    op.execute("""CREATE TABLE line_queue_updates (
        request_id text PRIMARY KEY CHECK(request_id ~ '^[^[:space:]]{1,128}$'),
        line_id text NOT NULL REFERENCES lines(id) ON DELETE RESTRICT,
        actor text NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        original_intent jsonb NOT NULL CHECK(jsonb_typeof(original_intent)='object'),
        receipt jsonb NOT NULL CHECK(jsonb_typeof(receipt)='object'),
        created_at timestamptz NOT NULL
    )""")
    op.execute("""CREATE TABLE line_queue_audit (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        line_id text NOT NULL REFERENCES lines(id) ON DELETE RESTRICT,
        actor text NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        action text NOT NULL,
        occurred_at timestamptz NOT NULL,
        evidence jsonb NOT NULL CHECK(jsonb_typeof(evidence)='object')
    )""")
    op.execute("""CREATE INDEX ix_line_queue_audit_line
        ON line_queue_audit(line_id,id)""")
    op.execute("""CREATE FUNCTION immutable_line_queue_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Line queue audit and request receipts are immutable';
        END $$""")
    for table in ("line_queue_updates", "line_queue_audit"):
        op.execute(f"""CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW
            EXECUTE FUNCTION immutable_line_queue_evidence()""")
        op.execute(f"""CREATE TRIGGER {table}_no_truncate
            BEFORE TRUNCATE ON {table} FOR EACH STATEMENT
            EXECUTE FUNCTION immutable_line_queue_evidence()""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM line_queue_entries)
            OR EXISTS(SELECT 1 FROM line_queue_updates)
            OR EXISTS(SELECT 1 FROM line_queue_audit)
            OR EXISTS(SELECT 1 FROM line_queue_policies
                WHERE auto_advance OR revision > 0 OR enabled_by IS NOT NULL
                    OR source_id IS NOT NULL OR stream_id IS NOT NULL
                    OR gateway_id IS NOT NULL OR capture_generation IS NOT NULL
                    OR blocked_reason IS NOT NULL) THEN
            RAISE EXCEPTION 'Used line queue intent and evidence cannot be downgraded';
        END IF;
    END $$""")
    op.drop_table("line_queue_audit")
    op.drop_table("line_queue_updates")
    op.drop_table("line_queue_entries")
    op.drop_table("line_queue_policies")
    op.execute("DROP FUNCTION preserve_line_queue_entry()")
    op.execute("DROP FUNCTION immutable_line_queue_evidence()")
