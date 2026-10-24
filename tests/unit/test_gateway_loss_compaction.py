"""Loss metadata compression retains exact evidence and transactional ownership."""

import json
import zlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from apps.gateway.buffer import BufferCorruption, BufferLimits, GatewayBuffer
from contracts.transport import BatchBody, DurableAck, GatewayBatch, batch_digest

AT = datetime(2026, 10, 24, tzinfo=UTC)
LATER = AT + timedelta(minutes=31)


def _batch(capture, *, seqs=(), gaps=(), samples=False):
    body = BatchBody(
        batch_id=f"batch-{capture}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=capture,
        captured_at=AT,
        events=[
            {
                "event_id": f"event-{seq}",
                "source_id": "source",
                "stream_id": "stream",
                "seq": seq,
                "equipment_id": "Outfeed",
                "execution_id": "execution",
                "occurred_at": AT,
                "kind": "PRODUCTION_GOOD",
                "payload": {"quantity": 1, "counter_epoch": "epoch"},
            }
            for seq in seqs
        ],
        samples=[
            {
                "sample_id": f"sample-{index}",
                "tag_id": f"Line.GoodTotal.{index}",
                "source_timestamp": None,
                "server_timestamp": AT,
                "acquired_at": AT,
                "status_code": 0x80000000,
                "reading": {"data_type": "UInt64", "value": index},
            }
            for index in range(40)
        ]
        if samples
        else (),
        gaps=gaps,
    )
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


def _ack(batch, verified, ingested, gaps=()):
    return DurableAck(
        batch_id=batch.body.batch_id,
        gateway_id="gateway",
        content_sha256=batch.content_sha256,
        durability="COMMITTED",
        committed_at=LATER,
        recorded_gaps=batch.body.gaps,
        events=[
            {"event_id": event.event_id, "seq": event.seq, "disposition": "APPLIED"}
            for event in batch.body.events
        ],
        progress={
            "source_id": "source",
            "stream_id": "stream",
            "verified_through_seq": verified,
            "latest_ingested_seq": ingested,
            "unresolved_gaps": gaps,
        },
    )


def _seed(buffer, *, confirmed=False):
    offset = 0
    receipt = None
    if confirmed:
        first = _batch(1, seqs=range(1, 21))
        buffer.enqueue(first, now=AT)
        receipt = _ack(first, 20, 20)
        buffer.acknowledge(receipt)
        offset = 1
    lost = _batch(1 + offset, seqs=(1 + 20 * offset,), samples=True)
    buffer.enqueue(lost, now=AT)
    reporter = _batch(2 + offset)
    buffer.enqueue(reporter, now=LATER)
    gaps = buffer.pending_event_gaps("source", "stream")
    # This reporter has not claimed the loss; the next report must claim its original intervals.
    buffer.acknowledge(_ack(reporter, 20 * offset, 20 * offset + 1, gaps))
    return lost, reporter, receipt, gaps


def _snapshot(buffer):
    return {
        table: [tuple(row) for row in buffer._db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in (
            "gateway_identity",
            "batch_ledger",
            "outbound_batches",
            "source_progress",
            "missing_ranges",
            "loss_intervals",
        )
    }


def test_queue_empty_303_bytes_free_enqueue_compacts_loss_before_budget_check(tmp_path):
    path = tmp_path / "buffer.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        lost, reporter, _, gaps = _seed(buffer)
        original = buffer._db.execute("SELECT body FROM missing_ranges").fetchone()[0]
        evidence = buffer.losses()
        buffer.limits = replace(BufferLimits(), max_metadata_bytes=buffer._metadata_bytes() + 303)
        assert buffer.status(now=LATER).pending_batches == 0
        assert buffer.status(now=LATER).queue_bytes == 0
        assert buffer.limits.max_metadata_bytes - buffer._metadata_bytes() == 303
        incoming = _batch(3, gaps=gaps)
        assert not buffer.enqueue(incoming, now=LATER).duplicate
        stored = buffer._db.execute("SELECT body,metadata_bytes FROM missing_ranges").fetchone()
        assert isinstance(stored[0], bytes)
        assert buffer._loss_bytes(stored[0]) == original.encode("utf-8")
        assert stored[1] == len(stored[0]) + 512
        assert buffer.losses() == evidence
        assert buffer.pending_event_gaps("source", "stream") == ()
        owner = buffer._db.execute("SELECT report_batch_id FROM loss_intervals").fetchone()[0]
        assert owner == incoming.body.batch_id
        assert buffer._db.execute("PRAGMA foreign_key_check").fetchall() == []
        buffer._verify()
        buffer.lease_next()
        limits = buffer.limits
    with GatewayBuffer(path, limits=limits, recover_inflight=False) as reopened:
        assert reopened.status(now=LATER).inflight_batches == 1
        assert reopened.enqueue(lost, now=LATER).duplicate
        assert reopened.enqueue(reporter, now=LATER).duplicate
        assert reopened.enqueue(incoming, now=LATER).duplicate
        assert reopened.losses() == evidence
    with GatewayBuffer(path, limits=limits) as reopened:
        assert reopened.status(now=LATER).pending_batches == 1
        assert reopened.lease_next().batch == incoming


def test_force_compaction_exact_bytes_validation_and_atomic_rollback(tmp_path, monkeypatch):
    path = tmp_path / "buffer.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        lost, _, receipt, gaps = _seed(buffer, confirmed=True)
        # Retain noncanonical JSON bytes, including whitespace and multibyte UTF-8 evidence.
        row = buffer._db.execute("SELECT * FROM missing_ranges").fetchone()
        body = json.loads(row["body"])
        body["original_annotation"] = "원문 보존"
        original = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        buffer._db.execute(
            "UPDATE missing_ranges SET body=?,metadata_bytes=?",
            (original.decode("utf-8"), len(original) + 512),
        )
        before = _snapshot(buffer)
        evidence = buffer.losses()
        ack_raw = (
            buffer._db.execute("SELECT ack FROM batch_ledger WHERE batch_id=?", (receipt.batch_id,))
            .fetchone()[0]
            .encode("utf-8")
        )
        buffer.limits = replace(BufferLimits(), max_metadata_bytes=buffer._metadata_bytes() + 303)
        original_compress = zlib.compress
        calls = 0

        def fail_during_compression(value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("interrupted compression")
            return original_compress(value)

        with monkeypatch.context() as patch:
            patch.setattr("apps.gateway.buffer.zlib.compress", fail_during_compression)
            with pytest.raises(RuntimeError, match="interrupted compression"):
                buffer.compact_metadata()
        assert calls == 2
        assert _snapshot(buffer) == before
        result = buffer.compact_metadata()
        assert result["metadata_bytes_before"] == buffer.limits.max_metadata_bytes - 303
        assert result["metadata_bytes_after"] < result["metadata_bytes_before"]
        assert result["ack_rows_compacted"] == result["loss_rows_compacted"] == 1
        after = _snapshot(buffer)
        assert after["gateway_identity"] == before["gateway_identity"]
        assert after["source_progress"] == before["source_progress"]
        assert after["loss_intervals"] == before["loss_intervals"]
        compressed = buffer._db.execute("SELECT body FROM missing_ranges").fetchone()[0]
        assert buffer._loss_bytes(compressed) == original
        assert buffer.losses() == evidence
        assert buffer.pending_event_gaps("source", "stream") == gaps
        assert (
            buffer._ack_bytes(
                buffer._db.execute(
                    "SELECT ack FROM batch_ledger WHERE batch_id=?", (receipt.batch_id,)
                ).fetchone()[0]
            )
            == ack_raw
        )
        assert buffer._db.execute("PRAGMA foreign_key_check").fetchall() == []
        # Reject an unknown version, a truncated stream, a corrupt stream and trailing bytes.
        for malformed in (
            b"HMILOSS2\0" + compressed[9:],
            compressed[:-1],
            compressed[:9] + b"bad zlib",
            compressed + b"trailing",
        ):
            with pytest.raises(BufferCorruption):
                buffer._loss_from_json(malformed)
        with monkeypatch.context() as patch:
            patch.setattr("apps.gateway.buffer._MAX_LOSS_BYTES", len(original) - 1)
            with pytest.raises(BufferCorruption):
                buffer._loss_from_json(compressed)
        buffer._verify()
    with GatewayBuffer(path, recover_inflight=False) as reopened:
        assert reopened.losses() == evidence
        assert reopened.enqueue(lost, now=LATER).duplicate
        assert reopened.acknowledge(receipt) == receipt
        assert reopened.compact_metadata()["loss_rows_compacted"] == 0
        # Full constructor verification rejects persisted compressed corruption.
        reopened._db.execute("UPDATE missing_ranges SET body=?", (compressed + b"trailing",))
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path, recover_inflight=False)
