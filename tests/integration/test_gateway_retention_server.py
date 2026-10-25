"""Pruned local IDs still meet the original PostgreSQL receipt/conflict contract."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from apps.api.bootstrap import ASSET_IDS, LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestConflict, IngestionService
from apps.gateway.buffer import BufferLimits, GatewayBuffer
from contracts.models import Parameters
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 25, 0, tzinfo=UTC)
EXAMPLE = Path(__file__).resolve().parents[2] / "contracts/examples/transport-batch.json"
SCOPE = dict(
    authenticated_gateway_id="gateway-1",
    authenticated_source_id="simulator-1",
    authenticated_stream_id="stream-1",
)


def test_pruned_id_replay_uses_server_original_receipt_and_rejects_changed_content(
    migrated, tmp_path
):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated)
    service.configure_source(
        source_id="simulator-1",
        stream_id="stream-1",
        gateway_id="gateway-1",
        line_id=LINE_ID,
        capture_generation=1,
        now=AT,
    )
    service.register_execution(
        execution_id="run-A",
        source_id="simulator-1",
        stream_id="stream-1",
        work_order_id="work-A",
        recipe_version="recipe-1",
        counter_epoch="epoch-1",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    raw = json.loads(EXAMPLE.read_text())["body"]
    raw.update(
        batch_id="retained-server-id", capture_seq="1", captured_at=AT.isoformat(), samples=[]
    )
    raw["events"][0].update(execution_id="run-A", equipment_id=ASSET_IDS["Outfeed"])
    body = BatchBody.model_validate(raw)
    original = GatewayBatch(body=body, content_sha256=batch_digest(body))
    with GatewayBuffer(
        tmp_path / "gateway.sqlite3",
        gateway_id="gateway-1",
        limits=BufferLimits(max_age=timedelta(seconds=1), ledger_retention=timedelta(seconds=1)),
    ) as buffer:
        buffer.enqueue(original, now=AT)
        original_ack = service.ingest(original, **SCOPE, now=AT)
        buffer.acknowledge(original_ack)
        result = buffer.prune_acknowledged(now=AT + timedelta(seconds=2))
        assert result["pruned_rows"] == 1
        assert buffer._db.execute("SELECT count(*) FROM batch_ledger").fetchone()[0] == 0
        # Exact old wire batch gets the committed original receipt from PostgreSQL.
        assert service.ingest(original, **SCOPE, now=AT + timedelta(seconds=2)) == original_ack
        # The historical local ID is absent, but capture sequence must still advance.
        changed_body = body.model_copy(update={"capture_seq": 2})
        changed = GatewayBatch(body=changed_body, content_sha256=batch_digest(changed_body))
        buffer.enqueue(changed, now=AT + timedelta(seconds=2))
        assert buffer.lease_next().batch == changed
        with pytest.raises(IngestConflict):
            service.ingest(changed, **SCOPE, now=AT + timedelta(seconds=2))
        buffer.retry(changed.body.batch_id)
        assert buffer.lease_next().batch == changed
        with migrated.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT count(*) FROM ingest_batches")).scalar_one() == 1
            )
            receipt = connection.execute(sa.text("SELECT ack FROM ingest_batches")).scalar_one()
            assert receipt == original_ack.model_dump(mode="json")
