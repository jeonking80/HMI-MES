"""Immutable capture decisions, independent alarm positions and startup fencing."""

from alembic import op

revision = "0011_alarm_capture_context"
down_revision = "0010_alarm_store"
branch_labels = None
depends_on = None

_DDL = (
    """ALTER TABLE alarm_channels ADD CONSTRAINT uq_alarm_channel_source
        UNIQUE(channel_id,source_id,stream_id)""",
    """ALTER TABLE source_streams ADD CONSTRAINT uq_alarm_capture_source
        UNIQUE(source_id,stream_id,gateway_id,capture_generation)""",
    """ALTER TABLE ingest_batches ADD CONSTRAINT uq_alarm_capture_batch
        UNIQUE(gateway_id,batch_id,source_id,stream_id,capture_generation,capture_seq,content_sha256)""",
    "CREATE INDEX ix_alarm_rule_time ON alarm_rule_versions(channel_id,changed_at DESC)",
    """CREATE TABLE alarm_capture_frontiers (
        channel_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, stream_id TEXT NOT NULL,
        gateway_id TEXT NOT NULL, capture_generation NUMERIC(20,0) NOT NULL,
        allocator_position NUMERIC(20,0) NOT NULL CHECK
            (allocator_position BETWEEN 0 AND 18446744073709551615),
        highest_capture_seq NUMERIC(20,0) NOT NULL CHECK
            (highest_capture_seq BETWEEN 0 AND 18446744073709551615),
        activation_fence_seq NUMERIC(20,0) NOT NULL CHECK
            (activation_fence_seq BETWEEN 0 AND 18446744073709551615),
        consumer_epoch TEXT NOT NULL, config_sha256 TEXT NOT NULL,
        requires_restart BOOLEAN NOT NULL,
        UNIQUE(channel_id,gateway_id,capture_generation),
        FOREIGN KEY(channel_id,source_id,stream_id) REFERENCES
            alarm_channels(channel_id,source_id,stream_id) ON DELETE RESTRICT,
        FOREIGN KEY(source_id,stream_id,gateway_id,capture_generation) REFERENCES
            source_streams(source_id,stream_id,gateway_id,capture_generation) ON DELETE RESTRICT
    )""",
    """CREATE TABLE alarm_capture_decisions (
        channel_id TEXT NOT NULL, gateway_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        source_id TEXT NOT NULL, stream_id TEXT NOT NULL,
        capture_generation NUMERIC(20,0) NOT NULL, capture_seq NUMERIC(20,0) NOT NULL,
        content_sha256 TEXT NOT NULL, assigned_position NUMERIC(20,0) NOT NULL CHECK
            (assigned_position BETWEEN 1 AND 18446744073709551615),
        consumer_epoch TEXT NOT NULL, rule_version TEXT,
        disposition TEXT NOT NULL CHECK(disposition IN
            ('LIVE','HISTORY_ONLY','UNKNOWN','UNSUPPORTED','QUARANTINED')),
        reason TEXT NOT NULL, context JSONB, policy JSONB, receipt JSONB NOT NULL,
        PRIMARY KEY(channel_id,gateway_id,batch_id),
        UNIQUE(channel_id,gateway_id,capture_generation,capture_seq),
        UNIQUE(channel_id,assigned_position),
        FOREIGN KEY(channel_id,source_id,stream_id) REFERENCES
            alarm_channels(channel_id,source_id,stream_id) ON DELETE RESTRICT,
        FOREIGN KEY(channel_id,gateway_id,capture_generation) REFERENCES
            alarm_capture_frontiers(channel_id,gateway_id,capture_generation) ON DELETE RESTRICT,
        FOREIGN KEY(channel_id,rule_version) REFERENCES
            alarm_rule_versions(channel_id,version) ON DELETE RESTRICT,
        FOREIGN KEY(gateway_id,batch_id,source_id,stream_id,capture_generation,capture_seq,
                    content_sha256) REFERENCES ingest_batches(gateway_id,batch_id,source_id,
                    stream_id,capture_generation,capture_seq,content_sha256) ON DELETE RESTRICT
    )""",
    """CREATE TRIGGER immutable_alarm_capture_decisions BEFORE UPDATE OR DELETE
        ON alarm_capture_decisions FOR EACH ROW EXECUTE FUNCTION alarm_append_only()""",
)


def upgrade():
    for sql in _DDL:
        op.execute(sql)


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM alarm_capture_frontiers)
           OR EXISTS(SELECT 1 FROM alarm_capture_decisions) THEN
            RAISE EXCEPTION 'Used alarm capture context cannot be downgraded';
        END IF; END $$""")
    op.drop_table("alarm_capture_decisions")
    op.drop_table("alarm_capture_frontiers")
    op.drop_index("ix_alarm_rule_time", table_name="alarm_rule_versions")
    op.execute("ALTER TABLE ingest_batches DROP CONSTRAINT uq_alarm_capture_batch")
    op.execute("ALTER TABLE source_streams DROP CONSTRAINT uq_alarm_capture_source")
    op.execute("ALTER TABLE alarm_channels DROP CONSTRAINT uq_alarm_channel_source")
