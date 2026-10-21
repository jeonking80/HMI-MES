"""Normalized alarm lifecycle, qualification checkpoints and transactional outbox."""

from alembic import op

revision = "0010_alarm_store"
down_revision = "0009_work_order_binding"
branch_labels = None
depends_on = None

_DDL = (
    """CREATE TABLE alarm_channels (
        channel_id TEXT PRIMARY KEY,
        line_id TEXT NOT NULL, source_id TEXT NOT NULL, stream_id TEXT NOT NULL,
        asset_id TEXT NOT NULL CHECK(asset_id='Sealer'), rule_id TEXT NOT NULL,
        current_version TEXT NOT NULL,
        last_position NUMERIC(20,0) CHECK(last_position BETWEEN 0 AND 18446744073709551615),
        last_observed_at TIMESTAMPTZ, qualification JSONB NOT NULL,
        UNIQUE(line_id,source_id,stream_id,asset_id,rule_id),
        FOREIGN KEY(source_id,stream_id,line_id) REFERENCES
            source_streams(source_id,stream_id,line_id) ON DELETE RESTRICT,
        FOREIGN KEY(line_id,asset_id) REFERENCES assets(line_id,code) ON DELETE RESTRICT
    )""",
    """CREATE TABLE alarm_rule_versions (
        channel_id TEXT NOT NULL REFERENCES alarm_channels(channel_id) ON DELETE RESTRICT,
        version TEXT NOT NULL, config JSONB NOT NULL,
        actor TEXT NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        changed_at TIMESTAMPTZ NOT NULL, change_id TEXT NOT NULL UNIQUE,
        PRIMARY KEY(channel_id,version)
    )""",
    """ALTER TABLE alarm_channels ADD CONSTRAINT fk_alarm_head
        FOREIGN KEY(channel_id,current_version) REFERENCES alarm_rule_versions(channel_id,version)
        DEFERRABLE INITIALLY DEFERRED""",
    """CREATE TABLE alarm_instances (
        alarm_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, rule_version TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL, returned_at TIMESTAMPTZ, acknowledged_at TIMESTAMPTZ,
        payload JSONB NOT NULL, UNIQUE(channel_id,alarm_id),
        FOREIGN KEY(channel_id,rule_version) REFERENCES
            alarm_rule_versions(channel_id,version) ON DELETE RESTRICT
    )""",
    "CREATE UNIQUE INDEX uq_alarm_open ON alarm_instances(channel_id) WHERE returned_at IS NULL",
    "CREATE INDEX ix_alarm_page ON alarm_instances(channel_id,occurred_at,alarm_id)",
    """CREATE TABLE alarm_actions (
        sequence BIGSERIAL UNIQUE, action_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
        alarm_id TEXT NOT NULL, actor TEXT NOT NULL REFERENCES users(login) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK(kind IN
            ('ACKNOWLEDGE','NOTE_ADD','NOTE_EDIT','RULE_CHANGE_CLOSE')),
        note_id TEXT, previous_action_id TEXT UNIQUE REFERENCES alarm_actions(action_id),
        payload JSONB NOT NULL,
        FOREIGN KEY(channel_id,alarm_id) REFERENCES alarm_instances(channel_id,alarm_id)
            ON DELETE RESTRICT
    )""",
    "CREATE INDEX ix_alarm_action_page ON alarm_actions(channel_id,alarm_id,sequence DESC)",
    "CREATE UNIQUE INDEX uq_alarm_note ON alarm_actions(note_id) WHERE kind='NOTE_ADD'",
    "CREATE INDEX ix_alarm_note_revision ON alarm_actions(note_id,sequence DESC)",
    """CREATE TABLE alarm_receipts (
        input_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES alarm_channels(channel_id),
        input_sha256 TEXT NOT NULL CHECK(input_sha256 ~ '^[0-9a-f]{64}$'),
        intent JSONB NOT NULL, receipt JSONB NOT NULL
    )""",
    """CREATE TABLE alarm_outbox (
        alarm_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, input_id TEXT NOT NULL UNIQUE
            REFERENCES alarm_receipts(input_id) ON DELETE RESTRICT,
        payload JSONB NOT NULL,
        FOREIGN KEY(channel_id,alarm_id) REFERENCES alarm_instances(channel_id,alarm_id)
            ON DELETE RESTRICT
    )""",
    """CREATE FUNCTION alarm_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'alarm evidence is append only'; END $$""",
    *(
        f"CREATE TRIGGER immutable_alarm_{table} BEFORE UPDATE OR DELETE ON alarm_{table} "
        "FOR EACH ROW EXECUTE FUNCTION alarm_append_only()"
        for table in ("rule_versions", "actions", "receipts", "outbox")
    ),
)


def upgrade():
    for sql in _DDL:
        op.execute(sql)


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM alarm_channels) THEN
            RAISE EXCEPTION 'Used alarm state cannot be downgraded';
        END IF; END $$""")
    for table in ("outbox", "receipts", "actions", "instances"):
        op.drop_table("alarm_" + table)
    op.execute("ALTER TABLE alarm_channels DROP CONSTRAINT fk_alarm_head")
    op.drop_table("alarm_rule_versions")
    op.drop_table("alarm_channels")
    op.execute("DROP FUNCTION alarm_append_only()")
