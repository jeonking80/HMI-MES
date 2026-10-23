"""Append-only scoped downtime annotations, audit evidence and original receipts."""

from alembic import op

revision = "0016_downtime_reasons"
down_revision = "0015_analog_history"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE downtime_reason_history (
        id BIGSERIAL PRIMARY KEY,
        request_id UUID NOT NULL UNIQUE,
        execution_id TEXT NOT NULL,
        line_id TEXT NOT NULL, source_id TEXT NOT NULL, stream_id TEXT NOT NULL,
        segment_from TIMESTAMPTZ NOT NULL, segment_to TIMESTAMPTZ NOT NULL,
        reason_code TEXT NOT NULL CHECK(reason_code IN
            ('MATERIAL_WAIT','EQUIPMENT','CHANGEOVER','PLANNED','UNCLASSIFIED')),
        note TEXT NOT NULL CHECK(char_length(note)<=200),
        actor TEXT NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        recorded_at TIMESTAMPTZ NOT NULL,
        original_intent JSONB NOT NULL, receipt JSONB NOT NULL,
        CHECK(segment_to > segment_from)
    )""")
    op.execute("""CREATE INDEX ix_downtime_reason_scope ON downtime_reason_history
        (execution_id,line_id,source_id,stream_id,segment_from,id DESC)""")
    op.execute("""CREATE FUNCTION immutable_downtime_reason() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'Downtime annotation audit and receipts are immutable'; END $$""")
    op.execute("""CREATE TRIGGER immutable_downtime_reason
        BEFORE UPDATE OR DELETE ON downtime_reason_history FOR EACH ROW
        EXECUTE FUNCTION immutable_downtime_reason()""")
    op.execute("""CREATE TRIGGER immutable_downtime_reason_truncate
        BEFORE TRUNCATE ON downtime_reason_history FOR EACH STATEMENT
        EXECUTE FUNCTION immutable_downtime_reason()""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM downtime_reason_history) THEN
            RAISE EXCEPTION 'Used downtime reason history cannot be downgraded';
        END IF;
    END $$""")
    op.drop_table("downtime_reason_history")
    op.execute("DROP FUNCTION immutable_downtime_reason()")
