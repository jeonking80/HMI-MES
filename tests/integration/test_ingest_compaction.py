"""Hours, exact compacted identities and actual FK protection on disposable PG17."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from apps.api.bootstrap import LINE_ID
from apps.api.ingest_retention import IngestRetentionExpired, maybe_prune_ingest
from tests.integration.test_ingest_retention import (
    OLD,
    SCOPE,
    batch,
    finish_compaction,
    prune,
    rows,
    submit,
)
from tests.integration.test_ingest_retention import observation_service as observation_service


def test_three_hour_cutoff_replays_recent_ack_and_compacts_old_identity(observation_service):
    service = observation_service
    now = datetime.now(UTC)
    old, recent = batch(), batch(capture_seq=2)
    submit(service, old, now - timedelta(hours=4))
    ack = submit(service, recent, now - timedelta(hours=2))
    result = maybe_prune_ingest(service.engine, retention_hours=3)
    result = finish_compaction(service.engine, result)
    assert result["deleted_count"] == result["compacted_count"] == 1
    assert rows(service.engine, "SELECT batch_id FROM ingest_receipts") == [{"batch_id": "batch-2"}]
    assert submit(service, recent) == ack
    with pytest.raises(IngestRetentionExpired):
        submit(service, old)


@pytest.mark.parametrize("hours", [None, 0, 1, 2, -1])
def test_database_rejects_short_retention_without_mutating_state(observation_service, hours):
    service = observation_service
    submit(service, batch())
    before = rows(service.engine, "SELECT * FROM ingest_retention_state")
    with pytest.raises(DBAPIError), service.engine.begin() as db:
        db.execute(sa.text("SELECT prune_ingest_batches_hours(:hours,200,400)"), {"hours": hours})
    assert rows(service.engine, "SELECT * FROM ingest_retention_state") == before
    assert len(rows(service.engine, "SELECT * FROM ingest_batches")) == 1


def test_default_is_168_hours_and_throttling_is_durable(observation_service):
    service = observation_service
    submit(service, batch(), datetime.now(UTC) - timedelta(hours=167))
    first = maybe_prune_ingest(service.engine)
    assert first["deleted_count"] == first["compacted_count"] == 0
    assert maybe_prune_ingest(service.engine)["status"] == "THROTTLED"
    assert len(rows(service.engine, "SELECT * FROM ingest_batches")) == 1


@pytest.mark.parametrize("identity", ["batch", "capture", "both"])
def test_compacted_identity_is_gateway_global_across_source_rebinding(
    observation_service, identity
):
    service = observation_service
    submit(service, batch())
    assert finish_compaction(service.engine, prune(service.engine))["compacted_count"] == 1
    assert rows(service.engine, "SELECT * FROM ingest_receipts") == []
    service.configure_source(
        gateway_id="gateway-1",
        source_id="other-source",
        stream_id="other-stream",
        line_id=LINE_ID,
        capture_generation=2 if identity == "batch" else 1,
        now=OLD,
    )
    value = batch(
        source_id="other-source",
        stream_id="other-stream",
        capture_generation=2 if identity == "batch" else 1,
        capture_seq=2 if identity == "batch" else 1,
        batch_id="batch-1" if identity in {"batch", "both"} else "unseen-batch",
    )
    with pytest.raises(IngestRetentionExpired):
        service.ingest(
            value,
            **(
                SCOPE
                | {
                    "authenticated_source_id": "other-source",
                    "authenticated_stream_id": "other-stream",
                }
            ),
            now=datetime.now(UTC),
        )
    assert rows(service.engine, "SELECT * FROM ingest_conflicts") == []
    assert rows(service.engine, "SELECT * FROM ingest_batches") == []


def test_only_actual_foreign_key_rows_pin_receipts_and_released_rows_compact(observation_service):
    service = observation_service
    for seq in (1, 2):
        submit(service, batch(capture_seq=seq))
    with service.engine.begin() as db:
        # An extra real FK proves discovery is not a hard-coded alarm table list.
        db.execute(
            sa.text("""CREATE TABLE retained_capture_audit (
            audit_id integer PRIMARY KEY,gateway_id text,batch_id text,
            FOREIGN KEY(gateway_id,batch_id) REFERENCES ingest_receipts(gateway_id,batch_id))""")
        )
        db.execute(sa.text("INSERT INTO retained_capture_audit VALUES(1,'gateway-1','batch-1')"))
    result = prune(service.engine)
    result = finish_compaction(service.engine, result, expected_expired=1)
    assert result["deleted_count"] == 2
    assert result["compacted_count"] == 1
    retained = rows(service.engine, "SELECT batch_id,expired_at FROM ingest_receipts")
    assert len(retained) == 1 and retained[0]["batch_id"] == "batch-1"
    assert retained[0]["expired_at"] is not None
    assert rows(service.engine, "SELECT * FROM retained_capture_audit") == [
        {"audit_id": 1, "gateway_id": "gateway-1", "batch_id": "batch-1"},
    ]
    with service.engine.begin() as db:
        db.execute(sa.text("DELETE FROM retained_capture_audit"))
    result = finish_compaction(service.engine, prune(service.engine))
    assert result["deleted_count"] == 0
    assert result["compacted_count"] == 1
    assert rows(service.engine, "SELECT * FROM ingest_receipts") == []
    for seq in (1, 2):
        with pytest.raises(IngestRetentionExpired):
            submit(service, batch(capture_seq=seq))


def test_compaction_failure_rolls_back_raw_receipts_chunks_ranges_and_scheduler(
    observation_service,
):
    engine = observation_service.engine
    submit(observation_service, batch())
    before = rows(engine, "SELECT * FROM ingest_retention_state")
    with engine.begin() as db:
        db.execute(
            sa.text("""CREATE FUNCTION fail_receipt_compaction() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected compaction failure'; END $$""")
        )
        db.execute(
            sa.text("""CREATE TRIGGER fail_receipt_compaction BEFORE DELETE ON ingest_receipts
            FOR EACH ROW EXECUTE FUNCTION fail_receipt_compaction()""")
        )
    assert maybe_prune_ingest(engine)["status"] == "failed"
    assert rows(engine, "SELECT * FROM ingest_retention_state") == before
    assert len(rows(engine, "SELECT * FROM ingest_batches")) == 1
    assert len(rows(engine, "SELECT * FROM capture_clock_evidence")) == 1
    assert rows(engine, "SELECT expired_at FROM ingest_receipts") == [{"expired_at": None}]
    assert rows(engine, "SELECT * FROM ingest_retention_ranges") == []
    assert rows(engine, "SELECT * FROM ingest_retired_batch_chunks") == []
    assert rows(engine, "SELECT * FROM ingest_retired_batch_segments") == []
    with engine.begin() as db:
        db.execute(sa.text("DROP TRIGGER fail_receipt_compaction ON ingest_receipts"))
    result = finish_compaction(engine, maybe_prune_ingest(engine))
    assert result["deleted_count"] == result["compacted_count"] == 1


def test_sparse_capture_ranges_and_segments_never_expire_unseen_identifiers(
    observation_service,
):
    service = observation_service
    for seq in (1, 3, 257):
        submit(service, batch(capture_seq=seq))
    assert finish_compaction(service.engine, prune(service.engine))["compacted_count"] == 3
    ranges = rows(
        service.engine,
        "SELECT from_capture_seq,through_capture_seq FROM ingest_retention_ranges "
        "ORDER BY from_capture_seq",
    )
    assert [(r["from_capture_seq"], r["through_capture_seq"]) for r in ranges] == [
        (1, 1),
        (3, 3),
        (257, 257),
    ]
    segments = rows(service.engine, "SELECT text_ids FROM ingest_retired_batch_segments")
    assert sorted(identity for row in segments for identity in row["text_ids"]) == [
        "batch-1",
        "batch-257",
        "batch-3",
    ]
    assert rows(service.engine, "SELECT * FROM ingest_retired_batch_chunks") == []
    value = batch(capture_seq=2, batch_id="unseen-batch")
    ack = submit(service, value, datetime.now(UTC))
    assert submit(service, value) == ack


def test_direct_sql_cannot_reinsert_compacted_identity(observation_service):
    service = observation_service
    submit(service, batch())
    assert finish_compaction(service.engine, prune(service.engine))["compacted_count"] == 1
    with pytest.raises(DBAPIError, match="RETENTION_EXPIRED"), service.engine.begin() as db:
        db.execute(
            sa.text("""INSERT INTO ingest_batches
            (gateway_id,batch_id,source_id,stream_id,capture_generation,capture_seq,
             content_sha256,body,ack,stored_at)
            VALUES ('gateway-1','batch-1','simulator-1','stream-1',1,99,:digest,
                    CAST(:body AS jsonb),'{}'::jsonb,clock_timestamp())"""),
            {"digest": batch().content_sha256, "body": batch().body.model_dump_json()},
        )
    assert rows(service.engine, "SELECT * FROM ingest_receipts") == []
