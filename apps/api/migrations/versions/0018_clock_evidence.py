"""Durable analytical capture clock decisions and inert measured-probe storage."""

from alembic import op

revision = "0018_clock_evidence"
down_revision = "0017_guest_viewer"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE capture_clock_evidence (
        gateway_id text NOT NULL, batch_id text NOT NULL,
        line_id text NOT NULL REFERENCES lines(id) ON DELETE RESTRICT,
        source_id text NOT NULL, stream_id text NOT NULL,
        capture_generation numeric(20,0) NOT NULL CHECK
            (capture_generation BETWEEN 0 AND 18446744073709551615),
        capture_seq numeric(20,0) NOT NULL CHECK
            (capture_seq BETWEEN 1 AND 18446744073709551615),
        clock_epoch text,
        batch_sha256 text NOT NULL CHECK(batch_sha256 ~ '^[0-9a-f]{64}$'),
        input_evidence_sha256 text CHECK(input_evidence_sha256 ~ '^[0-9a-f]{64}$'),
        method text NOT NULL CHECK(method IN
            ('NONE','CONFIGURED_API_REFERENCE','MEASURED_ROUND_TRIP')),
        status text NOT NULL CHECK(status IN ('GOOD','UNKNOWN')),
        reason_code text NOT NULL,
        original_capture_utc timestamptz NOT NULL,
        canonical_api_utc timestamptz,
        offset_lower_seconds double precision,
        offset_upper_seconds double precision,
        interval_lower_utc timestamptz, interval_upper_utc timestamptz,
        assessed_at timestamptz NOT NULL,
        normalization_version text NOT NULL,
        proof jsonb NOT NULL CHECK(jsonb_typeof(proof)='object'),
        PRIMARY KEY(gateway_id,batch_id),
        FOREIGN KEY(gateway_id,batch_id) REFERENCES ingest_batches(gateway_id,batch_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(source_id,stream_id) REFERENCES source_streams(source_id,stream_id)
            ON DELETE RESTRICT,
        CHECK ((offset_lower_seconds IS NULL AND offset_upper_seconds IS NULL) OR
            (offset_lower_seconds IS NOT NULL AND offset_upper_seconds IS NOT NULL AND
             offset_lower_seconds>'-Infinity'::double precision AND
             offset_upper_seconds<'Infinity'::double precision AND
             offset_lower_seconds<=offset_upper_seconds)),
        CHECK ((interval_lower_utc IS NULL AND interval_upper_utc IS NULL) OR
             (interval_lower_utc IS NOT NULL AND interval_upper_utc IS NOT NULL
              AND interval_lower_utc<=interval_upper_utc)),
        CHECK ((status='UNKNOWN' AND canonical_api_utc IS NULL) OR
            (status='GOOD' AND canonical_api_utc IS NOT NULL AND clock_epoch IS NOT NULL
             AND method<>'NONE' AND offset_lower_seconds IS NOT NULL
             AND offset_upper_seconds IS NOT NULL AND interval_lower_utc IS NOT NULL
             AND interval_upper_utc IS NOT NULL
             AND canonical_api_utc BETWEEN interval_lower_utc AND interval_upper_utc))
    )""")
    op.execute("""CREATE INDEX ix_capture_clock_canonical ON capture_clock_evidence
        (line_id,source_id,stream_id,canonical_api_utc)""")
    op.execute("""CREATE INDEX ix_capture_clock_original ON capture_clock_evidence
        (line_id,source_id,stream_id,original_capture_utc)""")
    op.execute("""CREATE FUNCTION immutable_capture_clock_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Capture clock evidence is immutable';
        END $$""")
    op.execute("""CREATE TRIGGER capture_clock_evidence_immutable
        BEFORE UPDATE OR DELETE ON capture_clock_evidence FOR EACH ROW
        EXECUTE FUNCTION immutable_capture_clock_evidence()""")
    op.execute("""CREATE TRIGGER capture_clock_evidence_no_truncate
        BEFORE TRUNCATE ON capture_clock_evidence FOR EACH STATEMENT
        EXECUTE FUNCTION immutable_capture_clock_evidence()""")
    # No route issues or accepts these nonces until CX-11 is explicitly enabled.
    # Proof rows snapshot measurement facts and never depend on retained probe rows.
    op.execute("""CREATE TABLE capture_clock_probes (
        nonce_sha256 text PRIMARY KEY CHECK(nonce_sha256 ~ '^[0-9a-f]{64}$'),
        credential_key_id text NOT NULL,
        gateway_id text NOT NULL, source_id text NOT NULL, stream_id text NOT NULL,
        capture_generation numeric(20,0) NOT NULL CHECK
            (capture_generation BETWEEN 0 AND 18446744073709551615),
        clock_epoch text NOT NULL,
        api_received_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
        gateway_send_utc timestamptz NOT NULL,
        gateway_send_monotonic double precision NOT NULL CHECK
            (gateway_send_monotonic>=0 AND gateway_send_monotonic<'Infinity'::double precision),
        consumed_batch_id text, consumed_input_sha256 text CHECK
            (consumed_input_sha256 ~ '^[0-9a-f]{64}$'),
        consumed_at timestamptz,
        CHECK(expires_at>api_received_at AND
            expires_at<=api_received_at+interval '60 seconds'),
        CHECK((consumed_batch_id IS NULL AND consumed_input_sha256 IS NULL
                AND consumed_at IS NULL) OR
              (consumed_batch_id IS NOT NULL AND consumed_input_sha256 IS NOT NULL
                AND consumed_at IS NOT NULL)),
        FOREIGN KEY(source_id,stream_id) REFERENCES source_streams(source_id,stream_id)
            ON DELETE RESTRICT
    )""")
    op.execute("CREATE INDEX ix_capture_clock_probe_expiry ON capture_clock_probes(expires_at)")
    op.execute("""CREATE INDEX ix_capture_clock_probe_scope ON capture_clock_probes
        (gateway_id,source_id,stream_id,capture_generation,clock_epoch,expires_at)""")
    op.execute("ALTER TABLE analog_history_raw ADD COLUMN canonical_utc timestamptz")
    op.execute("""CREATE INDEX ix_analog_history_raw_canonical ON analog_history_raw
        (line_id,source_id,stream_id,tag_id,canonical_utc)""")
    # Readers must also cover legacy/UNKNOWN rows with original claimed UTC.
    # Index the actual expression: a plain canonical index cannot bound those
    # range probes, and cleanup must not scan all retained rows per intake.
    op.execute("""CREATE INDEX ix_analog_history_raw_analytical ON analog_history_raw
        (line_id,source_id,stream_id,tag_id,COALESCE(canonical_utc,acquired_at))""")
    op.execute("""CREATE INDEX ix_analog_history_raw_analytical_cleanup ON analog_history_raw
        (LEAST(COALESCE(canonical_utc,acquired_at),stored_at))""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM capture_clock_evidence) OR
            EXISTS(SELECT 1 FROM capture_clock_probes) THEN
            RAISE EXCEPTION 'Used analytical clock evidence cannot be downgraded';
        END IF;
    END $$""")
    op.drop_index("ix_analog_history_raw_analytical_cleanup", table_name="analog_history_raw")
    op.drop_index("ix_analog_history_raw_analytical", table_name="analog_history_raw")
    op.drop_index("ix_analog_history_raw_canonical", table_name="analog_history_raw")
    op.drop_column("analog_history_raw", "canonical_utc")
    op.drop_table("capture_clock_probes")
    op.drop_table("capture_clock_evidence")
    op.execute("DROP FUNCTION immutable_capture_clock_evidence()")
