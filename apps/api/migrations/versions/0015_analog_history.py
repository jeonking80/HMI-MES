"""Bounded-retention analog raw evidence and versioned minute projections."""

from alembic import op

revision = "0015_analog_history"
down_revision = "0014_mixed_alarm_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE analog_history_clock_state (
        source_id text NOT NULL, stream_id text NOT NULL, gateway_id text NOT NULL,
        capture_generation numeric(20,0) NOT NULL, clock_epoch text NOT NULL,
        last_seq numeric(20,0) NOT NULL, last_at timestamptz NOT NULL,
        reversal_seq numeric(20,0),
        PRIMARY KEY(source_id,stream_id,gateway_id,capture_generation,clock_epoch),
        FOREIGN KEY(source_id,stream_id) REFERENCES source_streams(source_id,stream_id)
            ON DELETE RESTRICT
    )""")
    op.execute("""CREATE TABLE analog_history_raw (
        line_id text NOT NULL REFERENCES lines(id) ON DELETE RESTRICT,
        source_id text NOT NULL, stream_id text NOT NULL,
        gateway_id text NOT NULL,
        capture_generation numeric(20,0) NOT NULL CHECK
            (capture_generation BETWEEN 0 AND 18446744073709551615),
        capture_seq numeric(20,0) NOT NULL CHECK
            (capture_seq BETWEEN 1 AND 18446744073709551615),
        tag_id text NOT NULL, sample_id text NOT NULL, acquired_at timestamptz NOT NULL,
        stored_at timestamptz NOT NULL, batch_id text NOT NULL, observation jsonb NOT NULL,
        clock_assessment jsonb NOT NULL, aggregated boolean NOT NULL DEFAULT false,
        PRIMARY KEY (source_id,stream_id,gateway_id,capture_generation,capture_seq,tag_id),
        UNIQUE (source_id,stream_id,sample_id),
        FOREIGN KEY (source_id,stream_id) REFERENCES source_streams(source_id,stream_id)
            ON DELETE RESTRICT
    )""")
    op.execute("""CREATE INDEX ix_analog_history_raw_lookup ON analog_history_raw
        (line_id,source_id,stream_id,tag_id,acquired_at)""")
    op.execute("""CREATE INDEX ix_analog_history_raw_cleanup ON analog_history_raw
        (LEAST(acquired_at,stored_at))""")
    op.execute("""CREATE INDEX ix_analog_history_raw_pending ON analog_history_raw
        (line_id,source_id,stream_id,acquired_at) WHERE NOT aggregated""")
    op.execute("""CREATE TABLE analog_history_minutes (
        line_id text NOT NULL REFERENCES lines(id) ON DELETE RESTRICT,
        source_id text NOT NULL, stream_id text NOT NULL, tag_id text NOT NULL,
        minute timestamptz NOT NULL CHECK (minute=date_trunc('minute',minute)),
        policy_version text NOT NULL, point jsonb NOT NULL,
        PRIMARY KEY(line_id,source_id,stream_id,tag_id,minute,policy_version),
        FOREIGN KEY (source_id,stream_id) REFERENCES source_streams(source_id,stream_id)
            ON DELETE RESTRICT
    )""")
    op.execute("CREATE INDEX ix_analog_history_minutes_cleanup ON analog_history_minutes(minute)")


def downgrade():
    op.drop_table("analog_history_minutes")
    op.drop_table("analog_history_raw")
    op.drop_table("analog_history_clock_state")
