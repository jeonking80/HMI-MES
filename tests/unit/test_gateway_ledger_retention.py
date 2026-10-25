"""SQLite range retention, restart evidence, and accelerated daily capture volume."""

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


def ranges(buffer):
    return [
        dict(row)
        for row in buffer._db.execute(
            "SELECT * FROM ledger_pruned_ranges ORDER BY from_capture_seq"
        )
    ]


def spans(buffer):
    return [
        (int(row["from_capture_seq"]), int(row["through_capture_seq"])) for row in ranges(buffer)
    ]


def test_confirmed_old_prefix_reduces_accounted_metadata_and_reopens(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        first, first_ack = append_confirmed(buffer, 1)
        append_confirmed(buffer, 2)
        retained, retained_ack = append_confirmed(buffer, 3, at=OLD)
        progress = buffer.progress("source", "stream")
        before = buffer.status(now=OLD).metadata_bytes
        assert not ranges(buffer)
        result = buffer.prune_acknowledged(now=OLD, limit=1)
        assert result["pruned_rows"] == 1
        assert result["metadata_bytes_before"] == before
        assert result["metadata_bytes_after"] == buffer.status(now=OLD).metadata_bytes
        assert result["metadata_bytes_reclaimed"] == before - result["metadata_bytes_after"]
        middle = ranges(buffer)[0]
        assert int(middle["through_capture_seq"]) == 1
        assert middle["deleted_count"] == 1
        assert len(middle["cumulative_sha256"]) == 64
        assert buffer.prune_acknowledged(now=OLD, limit=100)["pruned_rows"] == 1
        final = ranges(buffer)[0]
        assert int(final["through_capture_seq"]) == 2
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
                "ledger_pruned_ranges",
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
        assert ranges(buffer) == [final]
    with GatewayBuffer(path) as reopened:
        assert ranges(reopened) == [final]
        assert reopened.capture_position == (1, 3)
        assert reopened.progress("source", "stream") == progress
        assert reopened.enqueue(retained, now=OLD).duplicate
        append_confirmed(reopened, 4, at=OLD)
        assert reopened.capture_position == (1, 4)


@pytest.mark.parametrize("blocker", ["young", "queued"])
def test_ineligible_capture_is_preserved_while_later_eligible_rows_are_pruned(tmp_path, blocker):
    with GatewayBuffer(tmp_path / "gateway.sqlite3", gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
        second = batch(2)
        buffer.enqueue(second, now=AT)
        if blocker == "young":
            buffer.acknowledge(receipt(second, at=OLD))
        append_confirmed(buffer, 3)
        protected = rows(buffer, "batch_ledger")[1:2]
        outbound = rows(buffer, "outbound_batches")
        assert buffer.prune_acknowledged(now=OLD)["pruned_rows"] == 2
        assert rows(buffer, "batch_ledger") == protected
        assert rows(buffer, "outbound_batches") == outbound
        assert spans(buffer) == [(1, 1), (3, 3)]


def test_eviction_and_loss_report_references_survive_later_pruning(tmp_path):
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
        protected = rows(buffer, "batch_ledger")[1:3]
        references = {table: rows(buffer, table) for table in ("missing_ranges", "loss_intervals")}
        assert references["missing_ranges"] and references["loss_intervals"]
        assert (
            buffer._db.execute("SELECT report_batch_id FROM loss_intervals").fetchone()[0]
            == report.body.batch_id
        )
        assert buffer.prune_acknowledged(now=OLD)["pruned_rows"] == 2
        assert rows(buffer, "batch_ledger") == protected
        assert all(rows(buffer, table) == before for table, before in references.items())
        assert spans(buffer) == [(1, 1), (4, 4)]


def test_pruning_hook_failure_rolls_back_rows_ranges_and_metadata(tmp_path, monkeypatch):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        for capture in range(1, 4):
            append_confirmed(buffer, capture)
        tables = ("batch_ledger", "ledger_pruned_ranges", "source_progress")
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
        "through_capture_seq='00000000000000000002'",
        "cumulative_sha256='" + "0" * 64 + "'",
        "range_sha256='" + "f" * 64 + "'",
        "metadata_bytes=metadata_bytes+1",
        "deleted_count=deleted_count+1",
    ],
)
def test_corrupt_range_fails_closed_on_reopen(tmp_path, assignment):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
        append_confirmed(buffer, 2, at=OLD)
        assert buffer.prune_acknowledged(now=OLD)["pruned_rows"] == 1
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE ledger_pruned_ranges SET {assignment}")
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path)


def test_existing_v1_database_gets_empty_ranges_without_losing_evidence(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        value, ack = append_confirmed(buffer, 1)
        buffer.enqueue(batch(2), now=AT)
        tables = ("gateway_identity", "batch_ledger", "outbound_batches", "source_progress")
        before = {table: rows(buffer, table) for table in tables}
    # A complete pre-retention v1 schema has neither checkpoint nor ranges.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE ledger_pruned_ranges")
        connection.execute("PRAGMA user_version=1")
    with GatewayBuffer(path) as migrated:
        assert all(rows(migrated, table) == expected for table, expected in before.items())
        assert not ranges(migrated)
        assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert migrated.acknowledge(ack) == ack
        assert migrated.enqueue(value, now=AT).duplicate
        assert migrated.prune_acknowledged(now=OLD)["pruned_rows"] == 1
        assert migrated.lease_next().batch.body.capture_seq == 2


def test_malformed_live_range_gates_prune_and_later_writes(tmp_path):
    with GatewayBuffer(tmp_path / "gateway.sqlite3", gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
        assert buffer.prune_acknowledged(now=OLD)["pruned_rows"] == 1
        buffer._db.execute("UPDATE ledger_pruned_ranges SET cumulative_sha256=?", (b"bad",))
        with pytest.raises(BufferCorruption):
            buffer.prune_acknowledged(now=OLD)
        with pytest.raises(BufferCorruption, match="gated"):
            buffer.enqueue(batch(2), now=OLD)


def test_v3_missing_ranges_cannot_be_recreated_as_legacy(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE ledger_pruned_ranges")
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path)


def test_range_does_not_hide_a_gap_in_the_remaining_chain(tmp_path):
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
        assert ranges(buffer)[0]["deleted_count"] == 3
        assert not rows(buffer, "batch_ledger")
        assert buffer.capture_position == (1, 3)
        limits = buffer.limits
    with GatewayBuffer(path, limits=limits) as reopened:
        assert ranges(reopened)[0]["deleted_count"] == 3
        assert reopened.capture_position == (1, 3)


def test_accelerated_86400_actual_captures_have_bounded_metadata(tmp_path, record_property):
    """One logical capture/second, with the production retention/queue-age ratio of 4:1."""
    path = tmp_path / "gateway.sqlite3"
    limits = BufferLimits(
        max_age=timedelta(seconds=2),
        ledger_retention=timedelta(seconds=8),
        max_metadata_bytes=300_000,
    )
    series = []
    with GatewayBuffer(path, gateway_id="gateway", limits=limits) as buffer:
        for capture in range(1, 4):
            buffer.enqueue(batch(capture), now=AT)
        for count in range(1, 86_401):
            capture = count + 3
            now = AT + timedelta(seconds=count + 2)
            append_confirmed(buffer, capture, at=now)
            if count == 1:
                losses = rows(buffer, "missing_ranges")
                intervals = rows(buffer, "loss_intervals")
                exceptions = rows(buffer, "batch_ledger")[:3]
                assert len(losses) == len(intervals) == 3
                assert all(
                    row["state"] == "EVICTED"
                    for row in buffer._db.execute(
                        "SELECT state FROM batch_ledger WHERE capture_seq<=?", (f"{3:020d}",)
                    )
                )
            if count % 100 == 0:
                buffer.prune_acknowledged(now=now, limit=1000)
            if count % 1000 == 0 or count == 86_400:
                metadata = buffer.status(now=now).metadata_bytes
                retained = buffer._db.execute("SELECT count(*) FROM batch_ledger").fetchone()[0]
                series.append({"captures": count, "metadata_bytes": metadata, "rows": retained})
                assert metadata < limits.max_metadata_bytes
                assert retained <= 3 + 9
        assert buffer.capture_position == (1, 86_403)
        assert ranges(buffer)[0]["deleted_count"] >= 86_391
        assert spans(buffer)[0][0] == 4
        assert rows(buffer, "batch_ledger")[:3] == exceptions
        assert rows(buffer, "missing_ranges") == losses
        assert rows(buffer, "loss_intervals") == intervals
        assert len(buffer.losses()) == 3
        assert buffer.progress("source", "stream").api_progress.verified_through_seq == 86_403
    metadata_values = [sample["metadata_bytes"] for sample in series]
    assert len(series) == 87
    assert max(metadata_values) - min(metadata_values) < 4096
    assert metadata_values[-1] < metadata_values[0] * 2
    record_property("logical_capture_seconds", 86_400)
    record_property("actual_enqueue_and_ack_count", 86_400)
    record_property("retention_to_max_age_ratio", 4)
    record_property("retained_evicted_exceptions", 3)
    record_property("metadata_series", json.dumps(series, separators=(",", ":")))
    with GatewayBuffer(path, limits=limits) as reopened:
        assert reopened.capture_position == (1, 86_403)
        assert reopened.status(now=AT + timedelta(days=1, seconds=2)).metadata_bytes < 300_000
        assert rows(reopened, "missing_ranges") == losses
        assert rows(reopened, "loss_intervals") == intervals


def test_production_evicted_block_leaves_two_ranges_and_all_loss_evidence(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    later = AT + timedelta(minutes=31)
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        for capture in range(1, 102):
            append_confirmed(buffer, capture)
        for capture in range(102, 555):
            buffer.enqueue(batch(capture), now=AT)
        for capture in range(555, 601):
            append_confirmed(buffer, capture, at=later)
        exceptions = rows(buffer, "batch_ledger")[101:554]
        evidence = {table: rows(buffer, table) for table in ("missing_ranges", "loss_intervals")}
        assert len(exceptions) == len(evidence["missing_ranges"]) == 453
        assert len(evidence["loss_intervals"]) == 453
        assert buffer.prune_acknowledged(now=OLD)["pruned_rows"] == 147
        assert spans(buffer) == [(1, 101), (555, 600)]
        assert rows(buffer, "batch_ledger") == exceptions
        assert all(rows(buffer, table) == before for table, before in evidence.items())
        final = ranges(buffer)
    with GatewayBuffer(path) as reopened:
        assert ranges(reopened) == final
        assert rows(reopened, "batch_ledger") == exceptions
        assert all(rows(reopened, table) == before for table, before in evidence.items())
        assert reopened.capture_position == (1, 600)
        assert len(reopened.losses()) == 453


def test_two_ranges_merge_after_intervening_young_ack_becomes_eligible(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
        append_confirmed(buffer, 2, at=OLD)
        append_confirmed(buffer, 3)
        assert buffer.prune_acknowledged(now=OLD)["pruned_rows"] == 2
        assert spans(buffer) == [(1, 1), (3, 3)]
        original_hashes = {row["cumulative_sha256"] for row in ranges(buffer)}
        assert buffer.prune_acknowledged(now=OLD + timedelta(hours=2))["pruned_rows"] == 1
        assert spans(buffer) == [(1, 3)]
        assert ranges(buffer)[0]["deleted_count"] == 3
        assert ranges(buffer)[0]["cumulative_sha256"] not in original_hashes
    with GatewayBuffer(path) as reopened:
        assert spans(reopened) == [(1, 3)]


def legacy_database(path, version):
    """Build the exact v1/v2 table shape from durable records, without runtime migration."""
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        append_confirmed(buffer, 1)
        append_confirmed(buffer, 2)
        append_confirmed(buffer, 3, at=OLD)
        checkpoint_values = None
        if version == 2:
            cumulative = buffer._checkpoint_digest({"capture_generation": f"{1:020d}"})
            for row in buffer._db.execute(
                "SELECT * FROM batch_ledger WHERE capture_seq<=? ORDER BY ordinal", (f"{2:020d}",)
            ):
                cumulative = buffer._checkpoint_digest(
                    {
                        "previous_sha256": cumulative,
                        **{
                            key: row[key]
                            for key in (
                                "capture_generation",
                                "capture_seq",
                                "batch_id",
                                "content_sha256",
                                "envelope_sha256",
                                "source_id",
                                "stream_id",
                            )
                        },
                        "ack": buffer._load_ack(row["ack"]).model_dump(mode="json"),
                    }
                )
            buffer._db.execute("DELETE FROM batch_ledger WHERE capture_seq<=?", (f"{2:020d}",))
            checkpoint_values = buffer._checkpoint_values(
                generation=1,
                sequence=2,
                cumulative=cumulative,
                deleted=2,
                updated=OLD.isoformat(),
            )
        buffer._db.execute("DROP TABLE ledger_pruned_ranges")
        if version == 2:
            buffer._db.execute(
                "CREATE TABLE ledger_checkpoint (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "capture_generation TEXT NOT NULL,last_capture_seq TEXT NOT NULL,"
                "cumulative_sha256 TEXT NOT NULL,"
                "deleted_count INTEGER NOT NULL CHECK(deleted_count>=0),"
                "updated_at TEXT NOT NULL,checkpoint_sha256 TEXT NOT NULL,"
                "metadata_bytes INTEGER NOT NULL CHECK(metadata_bytes>=0))"
            )
            buffer._write_checkpoint(checkpoint_values)
        buffer._db.execute(f"PRAGMA user_version={version}")
        return {
            table: rows(buffer, table)
            for table in ("gateway_identity", "batch_ledger", "source_progress")
        }


def test_v2_checkpoint_migrates_to_one_range_without_losing_evidence(tmp_path):
    path = tmp_path / "gateway.sqlite3"
    before = legacy_database(path, 2)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        old = dict(connection.execute("SELECT * FROM ledger_checkpoint").fetchone())
    with GatewayBuffer(path) as migrated:
        assert all(rows(migrated, table) == expected for table, expected in before.items())
        assert spans(migrated) == [(1, 2)]
        proof = ranges(migrated)[0]
        assert proof["deleted_count"] == old["deleted_count"]
        assert proof["cumulative_sha256"] == migrated._proof_digest("v2-prefix", old)
        assert proof["cumulative_sha256"] != old["cumulative_sha256"]
        assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not migrated._db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='ledger_checkpoint'"
        ).fetchone()
        assert migrated.prune_acknowledged(now=OLD + timedelta(hours=2))["pruned_rows"] == 1
        assert spans(migrated) == [(1, 3)]
    with GatewayBuffer(path) as reopened:
        assert spans(reopened) == [(1, 3)]


@pytest.mark.parametrize("version", [1, 2])
def test_ranges_migration_failure_rolls_back_schema_version_and_evidence(
    tmp_path, monkeypatch, version
):
    path = tmp_path / "gateway.sqlite3"
    before = legacy_database(path, version)
    with sqlite3.connect(path) as connection:
        schema = connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
        checkpoint_rows = (
            connection.execute("SELECT * FROM ledger_checkpoint").fetchall() if version == 2 else []
        )

    def interrupted(self):
        raise RuntimeError("interrupted ranges migration")

    with monkeypatch.context() as patch:
        patch.setattr(GatewayBuffer, "_before_ranges_migration_commit", interrupted)
        with pytest.raises(RuntimeError, match="interrupted ranges migration"):
            GatewayBuffer(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert (
            connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
            == schema
        )
        for table, expected in before.items():
            assert (
                connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == expected
            )
        if version == 2:
            assert (
                connection.execute("SELECT * FROM ledger_checkpoint").fetchall() == checkpoint_rows
            )
    with GatewayBuffer(path) as migrated:
        assert all(rows(migrated, table) == expected for table, expected in before.items())
        assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize("damage", ["overlap", "gap", "generation"])
def test_validly_checksummed_range_still_cannot_break_exact_capture_coverage(tmp_path, damage):
    path = tmp_path / "gateway.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway") as buffer:
        for capture in range(1, 5):
            append_confirmed(buffer, capture)
        assert buffer.prune_acknowledged(now=OLD, limit=2)["pruned_rows"] == 2
        original = ranges(buffer)[0]
        buffer._db.execute("DELETE FROM ledger_pruned_ranges")
        buffer._write_range(
            buffer._range_values(
                generation=2 if damage == "generation" else 1,
                start=1,
                end=3 if damage == "overlap" else 1 if damage == "gap" else 2,
                cumulative=original["cumulative_sha256"],
                recorded=original["recorded_at"],
            )
        )
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path)


def test_retention_default_and_minimum_cover_queue_age():
    assert BufferLimits().ledger_retention == timedelta(hours=2)
    with pytest.raises(ValueError):
        BufferLimits(ledger_retention=timedelta(minutes=29))
    with pytest.raises(ValueError):
        BufferLimits(max_age=timedelta(hours=3))
    assert BufferLimits(ledger_retention=timedelta(minutes=30)).max_age == timedelta(minutes=30)


def test_enqueue_pressure_prunes_later_acks_past_evicted_head_without_losing_evidence(tmp_path):
    later = AT + timedelta(minutes=31)
    with GatewayBuffer(tmp_path / "gateway.sqlite3", gateway_id="gateway") as buffer:
        buffer.enqueue(batch(1), now=AT)
        for capture in range(2, 9):
            append_confirmed(buffer, capture, at=later)
        exception = rows(buffer, "batch_ledger")[0]
        head = buffer._db.execute(
            "SELECT state FROM batch_ledger ORDER BY ordinal LIMIT 1"
        ).fetchone()
        assert head[0] == "EVICTED"
        evidence = {table: rows(buffer, table) for table in ("missing_ranges", "loss_intervals")}
        assert len(evidence["missing_ranges"]) == len(evidence["loss_intervals"]) == 1
        before = buffer.status(now=OLD).metadata_bytes
        buffer.limits = replace(buffer.limits, max_metadata_bytes=before + 100)
        assert before > buffer.limits.max_metadata_bytes * 9 // 10
        assert not ranges(buffer)
        queued = batch(9, at=OLD)
        assert not buffer.enqueue(queued, now=OLD).duplicate
        assert spans(buffer) == [(2, 8)]
        assert rows(buffer, "batch_ledger")[0] == exception
        assert all(rows(buffer, table) == expected for table, expected in evidence.items())
        assert buffer.status(now=OLD).metadata_bytes < before
        assert buffer.lease_next().batch == queued
