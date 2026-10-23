"""Receipt compaction preserves immutable evidence and the ACK transaction."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from apps.gateway.buffer import BatchConflict, BufferLimits, GatewayBuffer
from contracts.transport import BatchBody, DurableAck, GatewayBatch, StreamGap, batch_digest


def test_pressure_compacts_only_verified_receipts_atomically(tmp_path, monkeypatch):
    at = datetime(2026, 10, 23, tzinfo=UTC)
    later = at + timedelta(minutes=31)
    path = tmp_path / "gateway.sqlite3"

    def batch(capture, seqs=(), gaps=(), batch_id=None):
        body = BatchBody(
            batch_id=batch_id or f"batch-{capture}",
            gateway_id="gateway",
            source_id="source",
            stream_id="stream",
            capture_generation=1,
            capture_seq=capture,
            captured_at=at,
            events=[
                {
                    "event_id": f"event-{seq}",
                    "source_id": "source",
                    "stream_id": "stream",
                    "seq": seq,
                    "equipment_id": "Outfeed",
                    "execution_id": "execution",
                    "occurred_at": at,
                    "kind": "PRODUCTION_GOOD",
                    "payload": {"quantity": 1, "counter_epoch": "epoch"},
                }
                for seq in seqs
            ],
            gaps=gaps,
        )
        return GatewayBatch(body=body, content_sha256=batch_digest(body))

    def ack(value, verified, ingested, *, quarantined=False, gaps=()):
        return DurableAck(
            batch_id=value.body.batch_id,
            gateway_id="gateway",
            content_sha256=value.content_sha256,
            durability="COMMITTED",
            committed_at=later,
            events=[
                {
                    "event_id": event.event_id,
                    "seq": event.seq,
                    "disposition": "QUARANTINED" if quarantined else "APPLIED",
                    **(
                        {"reason_code": "UNKNOWN_EXECUTION", "reason": "Await original run"}
                        if quarantined
                        else {}
                    ),
                }
                for event in value.body.events
            ],
            recorded_gaps=value.body.gaps,
            progress={
                "source_id": "source",
                "stream_id": "stream",
                "verified_through_seq": verified,
                "latest_ingested_seq": ingested,
                "unresolved_gaps": gaps,
            },
        )

    def snapshot(buffer, table):
        return [tuple(row) for row in buffer._db.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        confirmed = batch(1, range(1, 21))
        confirmed_ack = ack(confirmed, 20, 20)
        buffer.enqueue(confirmed, now=at)
        buffer.acknowledge(confirmed_ack)
        unconfirmed = batch(2, (21,))
        buffer.enqueue(unconfirmed, now=at)
        buffer.acknowledge(ack(unconfirmed, 20, 21, quarantined=True))
        lost = batch(3, (22,))
        buffer.enqueue(lost, now=at)
        gap = StreamGap(
            from_seq=22,
            through_seq=22,
            irrecoverable=False,
            detected_at=later,
            reason_code="BUFFER_AGE_LIMIT",
            reason="Buffered source events require original-history replay",
            affected_execution_ids=("execution",),
        )
        reporter = batch(4, gaps=(gap,))
        buffer.enqueue(reporter, now=later)
        buffer.acknowledge(ack(reporter, 20, 22, gaps=(gap,)))
        pending = batch(5, (23,))
        incoming = batch(6, (24,))
        buffer.enqueue(pending, now=later)
        buffer.enqueue(incoming, now=later)
        before = {
            table: snapshot(buffer, table)
            for table in (
                "batch_ledger",
                "source_progress",
                "outbound_batches",
                "missing_ranges",
                "loss_intervals",
            )
        }
        buffer.limits = replace(
            BufferLimits(), max_metadata_bytes=buffer.status(now=later).metadata_bytes + 39
        )
        incoming_ack = ack(incoming, 24, 24)

        def fail_before_commit():
            raise RuntimeError("interrupted ACK")

        with monkeypatch.context() as patch:
            patch.setattr(buffer, "_before_ack_commit", fail_before_commit)
            with pytest.raises(RuntimeError, match="interrupted ACK"):
                buffer.acknowledge(incoming_ack)
        assert all(snapshot(buffer, table) == rows for table, rows in before.items())
        buffer.acknowledge(incoming_ack)
        assert buffer.status(now=later).metadata_bytes <= buffer.limits.max_metadata_bytes
        assert buffer.progress("source", "stream").api_progress.verified_through_seq == 24
        assert buffer.capture_position == (1, 6)
        rows = buffer._db.execute(
            "SELECT batch_id,ack FROM batch_ledger ORDER BY ordinal"
        ).fetchall()
        assert len(rows) == 6 and isinstance(rows[0]["ack"], bytes)
        assert buffer._ack_bytes(rows[0]["ack"]) == before["batch_ledger"][0][-2].encode("utf-8")
        assert isinstance(rows[5]["ack"], str)
        assert all(
            rows[index]["ack"] == before["batch_ledger"][index][-2] for index in (1, 2, 3, 4)
        )
        assert snapshot(buffer, "missing_ranges") == before["missing_ranges"]
        assert snapshot(buffer, "loss_intervals") == before["loss_intervals"]
        assert buffer.acknowledge(confirmed_ack) == confirmed_ack
        assert buffer.acknowledge(incoming_ack) == incoming_ack
        assert buffer.lease_next().batch == pending
        with pytest.raises(BatchConflict):
            buffer.enqueue(batch(7, (25,), batch_id="batch-1"), now=later)
        with pytest.raises(BatchConflict):
            buffer.acknowledge(confirmed_ack.model_copy(update={"committed_at": at}))
        limits = buffer.limits
    with GatewayBuffer(path, limits=limits) as reopened:
        assert reopened.acknowledge(confirmed_ack) == confirmed_ack
        assert reopened.enqueue(confirmed, now=later).duplicate
        assert reopened.capture_position == (1, 6)
        assert snapshot(reopened, "missing_ranges") == before["missing_ranges"]
        assert snapshot(reopened, "loss_intervals") == before["loss_intervals"]
