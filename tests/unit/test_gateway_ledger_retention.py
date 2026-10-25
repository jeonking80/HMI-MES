"""SQLite prefix retention, restart evidence, and accelerated daily capture volume."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from apps.gateway.buffer import (
    BatchConflict,
    BufferCorruption,
    BufferFull,
    BufferLimits,
    GatewayBuffer,
)
from contracts.transport import BatchBody, DurableAck, GatewayBatch, StreamGap, batch_digest

AT = datetime(2026, 10, 23, tzinfo=UTC)
OLD = AT + timedelta(days=8)


def batch(capture, *, at=AT, gaps=()):
    body = BatchBody(
        batch_id=f"retention-{capture:06d}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=capture,
        captured_at=at,
        events=()
        if gaps
        else (
            {
                "event_id": f"event-{capture:06d}",
                "source_id": "source",
                "stream_id": "stream",
                "seq": capture,
                "equipment_id": "Outfeed",
                "execution_id": "execution",
                "occurred_at": at,
                "kind": "PRODUCTION_GOOD",
                "payload": {"quantity": 1, "counter_epoch": "epoch"},
            },
        ),
        gaps=gaps,
    )
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


def receipt(value, *, at=AT, verified=None, gaps=()):
    seq = value.body.capture_seq
    return DurableAck(
        batch_id=value.body.batch_id,
        gateway_id="gateway",
        content_sha256=value.content_sha256,
        durability="COMMITTED",
        committed_at=at,
        events=[
            {
                "event_id": event.event_id,
                "seq": event.seq,
                "disposition": "APPLIED",
            }
            for event in value.body.events
        ],
        recorded_gaps=value.body.gaps,
        progress={
            "source_id": "source",
            "stream_id": "stream",
            "verified_through_seq": seq if verified is None else verified,
            "latest_ingested_seq": seq,
            "unresolved_gaps": gaps,
        },
    )


def append_confirmed(buffer, capture, *, at=AT):
    value = batch(capture, at=at)
    ack = receipt(value, at=at)
    buffer.enqueue(value, now=at)
    buffer.acknowledge(ack)
    return value, ack


def rows(buffer, table):
    return [tuple(row) for row in buffer._db.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def checkpoint(buffer):
    return dict(buffer._db.execute("SELECT * FROM ledger_checkpoint").fetchone())


def test_confirmed_old_prefix_reduces_accounted_metadata_and_reopens(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        first, first_ack = append_confirmed(buffer, 1)
        append_confirmed(buffer, 2)
        retained, retained_ack = append_confirmed(buffer, 3, at=OLD)
        progress = buffer.progress("source", "stream")
        before = buffer.status(now=OLD).metadata_bytes
        initial = checkpoint(buffer)
        result = buffer.prune_acknowledged(now=OLD, limit=1)
        assert result["pruned_rows"] == 1
        assert result["metadata_bytes_before"] == before
        assert result["metadata_bytes_after"] == buffer.status(now=OLD).metadata_bytes
        assert result["metadata_bytes_reclaimed"] == before - result["metadata_bytes_after"]
        middle = checkpoint(buffer)
        assert int(middle["last_capture_seq"]) == 1
        assert middle["deleted_count"] == 1
        assert middle["cumulative_sha256"] != initial["cumulative_sha256"]
        assert buffer.prune_acknowledged(now=OLD, limit=100)["pruned_rows"] == 1
        final = checkpoint(buffer)
        assert int(final["last_capture_seq"]) == 2
        assert final["deleted_count"] == 2
        assert final["cumulative_sha256"] != middle["cumulative_sha256"]
        assert buffer.status(now=OLD).metadata_bytes < before
        assert buffer.capture_position == (1, 3)
        assert buffer.progress("source", "stream") == progress
        assert buffer.status(now=OLD).metadata_bytes == sum(
            buffer._db.execute(f"SELECT coalesce(sum(metadata_bytes),0) FROM {table}").fetchone()[0]
            for table in (
                "batch_ledger",
                "source_progress",
                "missing_ranges",
                "loss_intervals",
                "ledger_checkpoint",
            )
        )
        assert [row["batch_id"] for row in buffer._db.execute("SELECT * FROM batch_ledger")] == [
            retained.body.batch_id
        ]
        assert buffer.acknowledge(retained_ack) == retained_ack
        with pytest.raises(BatchConflict):
            buffer.enqueue(first, now=OLD)
        with pytest.raises(ValueError):
            buffer.acknowledge(first_ack)
        assert buffer.prune_acknowledged(now=OLD)["pruned_rows"] == 0
        assert checkpoint(buffer) == final
    with GatewayBuffer(path) as reopened:
        assert checkpoint(reopened) == final
        assert reopened.capture_position == (1, 3)
        assert reopened.progress("source", "stream") == progress
        assert reopened.enqueue(retained, now=OLD).duplicate
        append_confirmed(reopened, 4, at=OLD)
        assert reopened.capture_position == (1, 4)


@pytest.mark.parametrize("blocker", ["young", "queued"])
def test_first_ineligible_capture_stops_contiguous_pruning(tmp_path, blocker):
    with GatewayBuffer(tmp_path / "gateway.sqlite3", gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
        second = batch(2)
        buffer.enqueue(second, now=AT)
        if blocker == "young":
            buffer.acknowledge(receipt(second, at=OLD))
        append_confirmed(buffer, 3)
        protected = rows(buffer, "batch_ledger")[1:]
        outbound = rows(buffer, "outbound_batches")
        assert buffer.prune_acknowledged(now=OLD)["pruned_rows"] == 1
        assert rows(buffer, "batch_ledger") == protected
        assert rows(buffer, "outbound_batches") == outbound
        assert int(checkpoint(buffer)["last_capture_seq"]) == 1


def test_eviction_and_loss_report_references_stop_prefix_deletion(tmp_path):
    later = AT + timedelta(minutes=31)
    with GatewayBuffer(tmp_path / "gateway.sqlite3", gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
        buffer.enqueue(batch(2), now=AT)
        gap = StreamGap(
            from_seq=2,
            through_seq=2,
            irrecoverable=False,
            detected_at=later,
            reason_code="BUFFER_AGE_LIMIT",
            reason="Buffered source events require original-history replay",
            affected_execution_ids=("execution",),
        )
        report = batch(3, at=later, gaps=(gap,))
        buffer.enqueue(report, now=later)
        buffer.acknowledge(receipt(report, at=later, verified=1, gaps=(gap,)))
        append_confirmed(buffer, 4, at=later)
        protected = rows(buffer, "batch_ledger")[1:]
        references = {table: rows(buffer, table) for table in ("missing_ranges", "loss_intervals")}
        assert references["missing_ranges"] and references["loss_intervals"]
        assert (
            buffer._db.execute("SELECT report_batch_id FROM loss_intervals").fetchone()[0]
            == report.body.batch_id
        )
        assert buffer.prune_acknowledged(now=OLD)["pruned_rows"] == 1
        assert rows(buffer, "batch_ledger") == protected
        assert all(rows(buffer, table) == before for table, before in references.items())
        assert int(checkpoint(buffer)["last_capture_seq"]) == 1


def test_pruning_hook_failure_rolls_back_rows_checkpoint_and_metadata(tmp_path, monkeypatch):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        for capture in range(1, 4):
            append_confirmed(buffer, capture)
        tables = ("batch_ledger", "ledger_checkpoint", "source_progress")
        before = {table: rows(buffer, table) for table in tables}
        before_bytes = buffer.status(now=OLD).metadata_bytes

        def interrupted():
            raise RuntimeError("interrupted ledger prune")

        with monkeypatch.context() as patch:
            patch.setattr(buffer, "_before_prune_commit", interrupted)
            with pytest.raises(RuntimeError, match="interrupted ledger prune"):
                buffer.prune_acknowledged(now=OLD)
        assert all(rows(buffer, table) == expected for table, expected in before.items())
        assert buffer.status(now=OLD).metadata_bytes == before_bytes
    with GatewayBuffer(path) as reopened:
        assert all(rows(reopened, table) == expected for table, expected in before.items())
        assert reopened.prune_acknowledged(now=OLD)["pruned_rows"] == 3


@pytest.mark.parametrize(
    "assignment",
    [
        "last_capture_seq='00000000000000000002'",
        "cumulative_sha256='" + "0" * 64 + "'",
        "checkpoint_sha256='" + "f" * 64 + "'",
        "metadata_bytes=metadata_bytes+1",
        "deleted_count=deleted_count+1",
    ],
)
def test_corrupt_checkpoint_fails_closed_on_reopen(tmp_path, assignment):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
        append_confirmed(buffer, 2, at=OLD)
        assert buffer.prune_acknowledged(now=OLD)["pruned_rows"] == 1
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE ledger_checkpoint SET {assignment}")
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path)


def test_existing_v1_database_gets_empty_checkpoint_without_losing_evidence(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        value, ack = append_confirmed(buffer, 1)
        buffer.enqueue(batch(2), now=AT)
        tables = ("gateway_identity", "batch_ledger", "outbound_batches", "source_progress")
        before = {table: rows(buffer, table) for table in tables}
    # A complete pre-retention v1 schema differs only by the absent checkpoint table.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE ledger_checkpoint")
        connection.execute("PRAGMA user_version=1")
    with GatewayBuffer(path) as migrated:
        assert all(rows(migrated, table) == expected for table, expected in before.items())
        assert checkpoint(migrated)["deleted_count"] == 0
        assert int(checkpoint(migrated)["last_capture_seq"]) == 0
        assert migrated.acknowledge(ack) == ack
        assert migrated.enqueue(value, now=AT).duplicate
        assert migrated.prune_acknowledged(now=OLD)["pruned_rows"] == 1
        assert migrated.lease_next().batch.body.capture_seq == 2


def test_malformed_live_checkpoint_gates_prune_and_later_writes(tmp_path):
    with GatewayBuffer(tmp_path / "gateway.sqlite3", gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
        buffer._db.execute("UPDATE ledger_checkpoint SET cumulative_sha256=?", (b"bad",))
        with pytest.raises(BufferCorruption):
            buffer.prune_acknowledged(now=OLD)
        with pytest.raises(BufferCorruption, match="gated"):
            buffer.enqueue(batch(2), now=OLD)


def test_v2_missing_checkpoint_cannot_be_recreated_as_legacy(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE ledger_checkpoint")
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path)


def test_checkpoint_does_not_hide_a_gap_in_the_remaining_chain(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
        append_confirmed(buffer, 2)
        append_confirmed(buffer, 3)
        assert buffer.prune_acknowledged(now=OLD, limit=1)["pruned_rows"] == 1
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM batch_ledger WHERE batch_id='retention-000002'")
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path)


def test_pressure_pruning_remains_committed_when_next_enqueue_is_too_large(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        for capture in range(1, 4):
            append_confirmed(buffer, capture)
        buffer.limits = replace(
            buffer.limits,
            max_bytes=1,
            max_metadata_bytes=buffer.status(now=OLD).metadata_bytes,
        )
        with pytest.raises(BufferFull, match="BATCH_TOO_LARGE"):
            buffer.enqueue(batch(4, at=OLD), now=OLD)
        assert checkpoint(buffer)["deleted_count"] == 3
        assert not rows(buffer, "batch_ledger")
        assert buffer.capture_position == (1, 3)
        limits = buffer.limits
    with GatewayBuffer(path, limits=limits) as reopened:
        assert checkpoint(reopened)["deleted_count"] == 3
        assert reopened.capture_position == (1, 3)


def test_accelerated_86400_actual_captures_have_bounded_metadata(tmp_path, record_property):
    """One logical capture/second over 24h; this does not claim 24h elapsed operation."""
    path = tmp_path / "gateway.sqlite3"
    limits = BufferLimits(ledger_retention=timedelta(seconds=10), max_metadata_bytes=300_000)
    series = []
    with GatewayBuffer(path, gateway_id="gateway", limits=limits) as buffer:
        for capture in range(1, 86_401):
            now = AT + timedelta(seconds=capture)
            append_confirmed(buffer, capture, at=now)
            if capture % 100 == 0:
                buffer.prune_acknowledged(now=now, limit=1000)
            if capture % 1000 == 0 or capture == 86_400:
                metadata = buffer.status(now=now).metadata_bytes
                retained = buffer._db.execute("SELECT count(*) FROM batch_ledger").fetchone()[0]
                series.append({"captures": capture, "metadata_bytes": metadata, "rows": retained})
                assert metadata < limits.max_metadata_bytes
                assert retained <= 11
        assert buffer.capture_position == (1, 86_400)
        assert checkpoint(buffer)["deleted_count"] >= 86_389
        assert not buffer.losses()
        assert buffer.progress("source", "stream").api_progress.verified_through_seq == 86_400
    metadata_values = [sample["metadata_bytes"] for sample in series]
    assert len(series) == 87
    assert max(metadata_values) - min(metadata_values) < 4096
    assert metadata_values[-1] < metadata_values[0] * 2
    record_property("logical_capture_seconds", 86_400)
    record_property("actual_enqueue_and_ack_count", 86_400)
    record_property("metadata_series", json.dumps(series, separators=(",", ":")))
    with GatewayBuffer(path, limits=limits) as reopened:
        assert reopened.capture_position == (1, 86_400)
        assert (
            reopened.status(now=AT + timedelta(days=1)).metadata_bytes < limits.max_metadata_bytes
        )
