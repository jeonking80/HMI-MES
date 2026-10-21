"""Gateway queue atomicity and actual process-crash evidence, without remote commit claims."""

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.gateway.buffer import (
    BatchConflict,
    BufferCorruption,
    BufferFull,
    BufferLimits,
    GatewayBuffer,
)
from contracts.models import UINT64_MAX
from contracts.transport import BatchBody, DurableAck, GatewayBatch, batch_digest

AT = datetime(2026, 10, 20, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def make_batch(seq=1, *, event_seqs=None, samples=True, gaps=(), **changes):
    event_seqs = (seq,) if event_seqs is None else event_seqs
    arguments = {
        "batch_id": f"batch-{seq}",
        "gateway_id": "gateway-A",
        "source_id": "source-A",
        "stream_id": "stream-A",
        "capture_generation": 1,
        "capture_seq": seq,
        "captured_at": AT,
        "samples": [
            {
                "sample_id": f"sample-{seq}",
                "tag_id": "Line.GoodTotal",
                "source_timestamp": None,
                "server_timestamp": AT - timedelta(seconds=1),
                "acquired_at": AT,
                "status_code": 0x80000000,
                "reading": {"data_type": "UInt64", "value": UINT64_MAX},
            }
        ]
        if samples
        else (),
        "events": [
            {
                "event_id": f"event-{number}",
                "source_id": "source-A",
                "stream_id": "stream-A",
                "seq": number,
                "equipment_id": "Outfeed",
                "execution_id": "execution-A",
                "occurred_at": AT,
                "kind": "PRODUCTION_GOOD",
                "payload": {"quantity": 1, "counter_epoch": "epoch-A"},
            }
            for number in event_seqs
        ],
        "gaps": gaps,
    }
    arguments.update(changes)
    body = BatchBody(**arguments)
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


def make_ack(batch, *, verified=None, ingested=None, quarantine=False, unresolved=(), now=None):
    max_seq = max((event.seq for event in batch.body.events), default=0)
    return DurableAck(
        batch_id=batch.body.batch_id,
        gateway_id=batch.body.gateway_id,
        content_sha256=batch.content_sha256,
        durability="COMMITTED",
        committed_at=now or AT + timedelta(seconds=batch.body.capture_seq),
        events=[
            {
                "event_id": event.event_id,
                "seq": event.seq,
                "disposition": "QUARANTINED" if quarantine else "APPLIED",
                **(
                    {"reason_code": "UNKNOWN_EXECUTION", "reason": "Await execution binding"}
                    if quarantine
                    else {}
                ),
            }
            for event in batch.body.events
        ],
        recorded_gaps=batch.body.gaps,
        progress={
            "source_id": batch.body.source_id,
            "stream_id": batch.body.stream_id,
            "verified_through_seq": max_seq if verified is None else verified,
            "latest_ingested_seq": max_seq if ingested is None else ingested,
            "unresolved_gaps": unresolved,
        },
    )


@pytest.fixture
def buffer(tmp_path):
    with GatewayBuffer(tmp_path / "buffer.sqlite3", gateway_id="gateway-A") as database:
        yield database


def test_default_limits_identity_schema_durability_and_single_owner(buffer):
    assert buffer.limits.max_age == timedelta(minutes=30)
    assert buffer.limits.max_bytes == 100_000_000
    assert buffer.capture_position == (1, 0)
    assert buffer._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert buffer._db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert buffer._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert buffer._db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert buffer.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(BlockingIOError):
        GatewayBuffer(buffer.path)
    alias = buffer.path.parent / "alias.sqlite3"
    alias.symlink_to(buffer.path)
    with pytest.raises(BlockingIOError):
        GatewayBuffer(alias)
    identity = buffer.gateway_id
    buffer.close()
    with GatewayBuffer(alias) as reopened:
        assert reopened.gateway_id == identity
    with pytest.raises(ValueError, match="identity"):
        GatewayBuffer(alias, gateway_id="replacement")


def test_foreign_database_and_hardlink_are_rejected_without_changing_contents(tmp_path):
    foreign = tmp_path / "simulator.sqlite3"
    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE executions (execution_id TEXT)")
        connection.execute("PRAGMA user_version=2")
    original = foreign.read_bytes()
    with pytest.raises(ValueError, match="another app"):
        GatewayBuffer(foreign)
    assert foreign.read_bytes() == original
    assert not Path(str(foreign) + "-wal").exists()
    with GatewayBuffer(tmp_path / "gateway.sqlite3") as database:
        path = database.path
    link = tmp_path / "hardlink.sqlite3"
    os.link(path, link)
    with pytest.raises(ValueError, match="Hardlink"):
        GatewayBuffer(link)


def test_exact_payload_retries_inflight_restart_and_ack_release_are_durable(buffer):
    batch = make_batch()
    assert buffer.enqueue(batch, now=AT).status == "QUEUED"
    assert buffer.capture_position == (1, 1)
    progress = buffer.progress("source-A", "stream-A")
    assert (
        progress.latest_read_seq,
        progress.api_progress.latest_ingested_seq,
        progress.api_progress.verified_through_seq,
    ) == (1, 0, 0)
    first = buffer.lease_next()
    assert first.batch == batch and first.attempts == 1 and first.status == "INFLIGHT"
    assert buffer.lease_next() is None
    buffer.retry(batch.body.batch_id)
    second = buffer.lease_next()
    assert second.batch.model_dump_json() == first.batch.model_dump_json()
    assert second.attempts == 2
    path = buffer.path
    buffer.close()
    with GatewayBuffer(path) as reopened:
        third = reopened.lease_next()
        assert third.batch == batch and third.attempts == 3
        sample = third.batch.body.samples[0]
        assert sample.reading.value == UINT64_MAX
        assert sample.status_code == 0x80000000
        assert sample.source_timestamp is None
        assert sample.server_timestamp == AT - timedelta(seconds=1)
        ack = make_ack(batch)
        assert reopened.acknowledge(ack) == ack
        assert reopened.lease_next() is None
        assert reopened.progress("source-A", "stream-A").api_progress.verified_through_seq == 1
        assert reopened.acknowledge(ack) == ack
    with GatewayBuffer(path) as reopened:
        assert reopened.lease_next() is None
        assert reopened.enqueue(batch, now=AT + timedelta(hours=1)).status == "ACKNOWLEDGED"
        assert reopened.capture_position == (1, 1)
        assert reopened.acknowledge(ack) == ack


def test_duplicate_and_conflicting_payloads_capture_and_identity(buffer):
    batch = make_batch()
    buffer.enqueue(batch, now=AT)
    assert buffer.enqueue(batch, now=AT).duplicate
    changed = make_batch(captured_at=AT + timedelta(seconds=1))
    with pytest.raises(BatchConflict, match="different"):
        buffer.enqueue(changed, now=AT)
    with pytest.raises(BatchConflict, match="sequence"):
        buffer.enqueue(make_batch(3), now=AT)
    with pytest.raises(ValueError, match="another gateway"):
        buffer.enqueue(make_batch(2, gateway_id="foreign"), now=AT)
    with pytest.raises(BufferFull, match="CAPTURE"):
        buffer.enqueue(make_batch(2, capture_generation=2), now=AT)
    assert buffer.capture_position == (1, 1)
    assert buffer.lease_next().batch == batch


@pytest.mark.parametrize(
    "field,value",
    [
        ("batch_id", "wrong"),
        ("gateway_id", "wrong"),
        ("content_sha256", "0" * 64),
        ("events", ()),
        (
            "progress",
            {
                "source_id": "wrong",
                "stream_id": "stream-A",
                "latest_ingested_seq": 1,
                "verified_through_seq": 1,
            },
        ),
    ],
)
def test_wrong_receipt_does_not_delete_or_advance(buffer, field, value):
    batch = make_batch()
    buffer.enqueue(batch, now=AT)
    ack = make_ack(batch).model_copy(update={field: value})
    with pytest.raises(ValueError):
        buffer.acknowledge(ack)
    assert buffer.progress("source-A", "stream-A").api_progress.latest_ingested_seq == 0
    assert buffer.lease_next().batch == batch


def test_pending_or_omitted_durability_is_not_a_release_receipt(buffer):
    batch = make_batch()
    buffer.enqueue(batch, now=AT)
    for durability in ("PENDING", None):
        ack = make_ack(batch).model_copy(update={"durability": durability})
        with pytest.raises(ValidationError):
            buffer.acknowledge(ack)
    assert buffer.lease_next().batch == batch


def test_quarantine_releases_owned_payload_but_never_claims_application(buffer):
    batch = make_batch()
    buffer.enqueue(batch, now=AT)
    ack = make_ack(batch, verified=0, quarantine=True)
    buffer.acknowledge(ack)
    assert buffer.lease_next() is None
    progress = buffer.progress("source-A", "stream-A")
    assert progress.latest_read_seq == progress.api_progress.latest_ingested_seq == 1
    assert progress.api_progress.verified_through_seq == 0


def test_old_receipt_cannot_rewind_newer_frontiers_or_reopen_repaired_gap(buffer):
    first, second = make_batch(), make_batch(2)
    buffer.enqueue(first, now=AT)
    buffer.enqueue(second, now=AT)
    buffer.acknowledge(make_ack(second, verified=2, ingested=2))
    buffer.acknowledge(make_ack(first, verified=1, ingested=1))
    progress = buffer.progress("source-A", "stream-A")
    assert (
        progress.api_progress.verified_through_seq,
        progress.api_progress.latest_ingested_seq,
    ) == (2, 2)
    assert buffer.lease_next() is None


def test_maximum_thousand_events_and_uint64_round_trip(buffer):
    batch = make_batch(event_seqs=tuple(range(UINT64_MAX - 999, UINT64_MAX + 1)))
    buffer.enqueue(batch, now=AT, latest_read_seq=UINT64_MAX)
    restored = buffer.lease_next().batch
    assert len(restored.body.events) == 1000
    assert restored == batch
    assert '"18446744073709551615"' in restored.model_dump_json()
    assert buffer.progress("source-A", "stream-A").latest_read_seq == UINT64_MAX
    buffer.acknowledge(make_ack(batch, verified=0, ingested=UINT64_MAX))
    assert buffer.progress("source-A", "stream-A").api_progress.verified_through_seq == 0


def test_age_boundary_records_exact_event_holes_sample_loss_and_keeps_recent(tmp_path):
    limits = BufferLimits(max_age=timedelta(seconds=30))
    with GatewayBuffer(tmp_path / "age.sqlite3", gateway_id="gateway-A", limits=limits) as database:
        first = make_batch(event_seqs=(1, 2, 5))
        database.enqueue(first, now=AT)
        database.enqueue(make_batch(2, event_seqs=(6,)), now=AT + timedelta(seconds=29))
        assert database.status(now=AT + timedelta(seconds=30)).age_limit_reached
        result = database.enqueue(make_batch(3, event_seqs=(7,)), now=AT + timedelta(seconds=30))
        assert result.source_requery_required
        assert len(result.losses) == 1
        loss = result.losses[0]
        assert loss.reason_code == "BUFFER_AGE_LIMIT"
        assert loss.event_ranges == ((1, 2), (5, 5))
        assert loss.affected_execution_ids == ("execution-A",)
        assert loss.samples[0]["tag_id"] == "Line.GoodTotal"
        assert loss.samples[0]["source_timestamp"] is None
        assert "reading" not in loss.samples[0]
        assert database.lease_next().batch.body.batch_id == "batch-2"
        assert database.progress("source-A", "stream-A").api_progress.verified_through_seq == 0
        assert database.losses() == result.losses
    with GatewayBuffer(database.path, limits=limits) as reopened:
        assert reopened.losses() == result.losses
        assert reopened.enqueue(first, now=AT + timedelta(hours=1)).status == "EVICTED"


def test_byte_boundary_recorded_eviction_and_oversize_backpressure(tmp_path):
    with GatewayBuffer(tmp_path / "measure.sqlite3", gateway_id="gateway-A") as measure:
        measure.enqueue(make_batch(), now=AT)
        one = measure.status(now=AT).queue_bytes
    limits = BufferLimits(max_bytes=one * 2)
    with GatewayBuffer(
        tmp_path / "bytes.sqlite3", gateway_id="gateway-A", limits=limits
    ) as database:
        database.enqueue(make_batch(), now=AT)
        database.enqueue(make_batch(2), now=AT)
        assert database.status(now=AT).queue_bytes == limits.max_bytes
        result = database.enqueue(make_batch(3), now=AT)
        assert result.losses[0].batch_id == "batch-1"
        assert result.losses[0].reason_code == "BUFFER_BYTE_LIMIT"
        assert database.status(now=AT).queue_bytes <= limits.max_bytes
        with pytest.raises(BufferFull, match="BATCH_TOO_LARGE"):
            database.enqueue(make_batch(4, event_seqs=tuple(range(4, 30))), now=AT)
        assert database.capture_position == (1, 3)
        assert len(database.losses()) == 1


def test_metadata_pressure_rolls_back_would_be_eviction_and_capture_position(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway-A") as database:
        first = make_batch()
        database.enqueue(first, now=AT)
        metadata = database.status(now=AT).metadata_bytes
    limits = BufferLimits(max_age=timedelta(seconds=1), max_metadata_bytes=metadata)
    with GatewayBuffer(path, limits=limits) as database:
        with pytest.raises(BufferFull, match="METADATA_FULL"):
            database.enqueue(make_batch(2), now=AT + timedelta(seconds=1))
        assert database.capture_position == (1, 1)
        assert database.losses() == ()
        assert database.lease_next().batch == first
        with pytest.raises(BufferFull, match="METADATA_FULL"):
            database.acknowledge(make_ack(first))
        assert database.progress("source-A", "stream-A").api_progress.latest_ingested_seq == 0


def test_inflight_pressure_is_explicit_and_never_drops_request_being_sent(tmp_path):
    limits = BufferLimits(max_age=timedelta(seconds=1))
    with GatewayBuffer(
        tmp_path / "inflight.sqlite3", gateway_id="gateway-A", limits=limits
    ) as database:
        first = make_batch()
        database.enqueue(first, now=AT)
        database.lease_next()
        with pytest.raises(BufferFull, match="INFLIGHT_PRESSURE"):
            database.enqueue(make_batch(2), now=AT + timedelta(seconds=1))
        assert database.losses() == ()
        assert database.capture_position == (1, 1)
        database.retry(first.body.batch_id)
        assert database.enqueue(make_batch(2), now=AT + timedelta(seconds=1)).losses


@pytest.mark.parametrize(
    "hook,operation,committed",
    [
        ("_before_enqueue_commit", "enqueue", False),
        ("_after_enqueue_commit", "enqueue", True),
        ("_before_ack_commit", "ack", False),
        ("_after_ack_commit", "ack", True),
    ],
)
def test_actual_process_exit_before_after_enqueue_and_ack_is_atomic(
    tmp_path, hook, operation, committed
):
    path = tmp_path / "crash.sqlite3"
    batch = make_batch()
    ack = make_ack(batch)
    if operation == "ack":
        with GatewayBuffer(path, gateway_id="gateway-A") as database:
            database.enqueue(batch, now=AT)
            database.lease_next()
    batch_file, ack_file = tmp_path / "batch.json", tmp_path / "ack.json"
    batch_file.write_text(batch.model_dump_json())
    ack_file.write_text(ack.model_dump_json())
    script = """
import os, sys
from pathlib import Path
from datetime import datetime, UTC
from apps.gateway.buffer import GatewayBuffer
from contracts.transport import GatewayBatch, DurableAck
class CrashingBuffer(GatewayBuffer):
    pass
setattr(CrashingBuffer, sys.argv[4], lambda self: os._exit(73))
with CrashingBuffer(sys.argv[1], gateway_id="gateway-A") as database:
    if sys.argv[5] == "enqueue":
        batch = GatewayBatch.model_validate_json(Path(sys.argv[2]).read_text())
        database.enqueue(batch, now=datetime(2026,10,20,12,tzinfo=UTC))
    else:
        database.acknowledge(DurableAck.model_validate_json(Path(sys.argv[3]).read_text()))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(path), str(batch_file), str(ack_file), hook, operation],
        cwd=ROOT,
        capture_output=True,
        timeout=20,
    )
    assert process.returncode == 73, process.stderr.decode()
    with GatewayBuffer(path) as reopened:
        restored = reopened.lease_next()
        if operation == "enqueue":
            assert (restored is not None) == committed
            assert reopened.capture_position == (1, int(committed))
            assert reopened.progress("source-A", "stream-A").latest_read_seq == int(committed)
        else:
            assert (restored is None) == committed
            assert reopened.progress(
                "source-A", "stream-A"
            ).api_progress.verified_through_seq == int(committed)
            assert reopened.capture_position == (1, 1)
            if committed:
                assert reopened.acknowledge(ack) == ack
        if restored is not None:
            assert restored.batch == batch
        assert reopened._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not reopened._db.execute("PRAGMA foreign_key_check").fetchall()


def test_enqueue_exception_rolls_back_payload_read_cursor_and_recorded_losses(buffer, monkeypatch):
    buffer.enqueue(make_batch(), now=AT)
    monkeypatch.setattr(
        buffer, "_before_enqueue_commit", lambda: (_ for _ in ()).throw(RuntimeError("stop"))
    )
    with pytest.raises(RuntimeError):
        buffer.enqueue(make_batch(2), now=AT + timedelta(minutes=30))
    assert buffer.losses() == ()
    assert buffer.capture_position == (1, 1)
    assert buffer.progress("source-A", "stream-A").latest_read_seq == 1
    assert buffer.lease_next().batch.body.batch_id == "batch-1"


def test_persisted_payload_tamper_is_gated_and_reopen_fails(buffer):
    buffer.enqueue(make_batch(), now=AT)
    body = json.loads(buffer._db.execute("SELECT body FROM outbound_batches").fetchone()[0])
    body["body"]["samples"][0]["status_code"] = 0
    buffer._db.execute("UPDATE outbound_batches SET body=?", (json.dumps(body),))
    with pytest.raises(BufferCorruption):
        buffer.lease_next()
    with pytest.raises(BufferCorruption, match="gated"):
        buffer.enqueue(make_batch(2), now=AT)
    path = buffer.path
    buffer.close()
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path)


def test_capture_exhaustion_and_invalid_limits_do_not_wrap(buffer):
    for changes in ({"max_bytes": 0}, {"max_metadata_bytes": True}, {"max_age": timedelta(0)}):
        with pytest.raises(ValueError):
            replace(BufferLimits(), **changes)
    buffer._db.execute("UPDATE gateway_identity SET last_capture_seq=?", (str(UINT64_MAX),))
    with pytest.raises(BufferFull, match="CAPTURE"):
        buffer.enqueue(make_batch(1), now=AT)
    assert buffer.capture_position == (1, UINT64_MAX)
    assert buffer.lease_next() is None


def test_delayed_equal_frontier_receipt_cannot_reopen_repaired_hole(buffer):
    from contracts.transport import StreamGap

    gap = StreamGap(
        from_seq=2,
        through_seq=2,
        irrecoverable=False,
        detected_at=AT,
        reason_code="SOURCE_HOLE",
        reason="Await replay",
    )
    old = make_batch(1, event_seqs=(3,))
    repair = make_batch(2, event_seqs=(2,))
    tail = make_batch(3, event_seqs=(5,))
    for batch in (old, repair, tail):
        buffer.enqueue(batch, now=AT)
    buffer.acknowledge(make_ack(repair, verified=0, ingested=3, now=AT + timedelta(seconds=5)))
    delayed = make_ack(
        old,
        verified=0,
        ingested=3,
        quarantine=True,
        unresolved=(gap,),
        now=AT + timedelta(seconds=10),
    )
    buffer.acknowledge(delayed)
    assert buffer.progress("source-A", "stream-A").api_progress.unresolved_gaps == ()
    assert buffer.acknowledge(delayed) == delayed
    tail_gap = gap.model_copy(update={"from_seq": 4, "through_seq": 4})
    buffer.acknowledge(make_ack(tail, verified=0, ingested=5, unresolved=(gap, tail_gap)))
    assert buffer.progress("source-A", "stream-A").api_progress.unresolved_gaps == (tail_gap,)


def test_delayed_receipt_preserves_only_still_missing_overlap(buffer):
    from contracts.transport import StreamGap

    gap = StreamGap(
        from_seq=2,
        through_seq=4,
        irrecoverable=False,
        detected_at=AT,
        reason_code="SOURCE_HOLE",
        reason="Await replay",
    )
    original = make_batch(1, event_seqs=(5,))
    repair = make_batch(2, event_seqs=(3,))
    buffer.enqueue(original, now=AT)
    buffer.enqueue(repair, now=AT)
    remaining = (
        gap.model_copy(update={"through_seq": 2}),
        gap.model_copy(update={"from_seq": 4}),
    )
    buffer.acknowledge(make_ack(repair, verified=0, ingested=5, unresolved=remaining))
    buffer.acknowledge(
        make_ack(original, verified=0, ingested=5, unresolved=(gap,), quarantine=True)
    )
    assert buffer.progress("source-A", "stream-A").api_progress.unresolved_gaps == remaining
    path = buffer.path
    buffer.close()
    with GatewayBuffer(path) as reopened:
        assert reopened.progress("source-A", "stream-A").api_progress.unresolved_gaps == remaining


def test_delayed_receipt_cannot_downgrade_irrecoverable_gap_evidence(buffer):
    from contracts.transport import StreamGap

    permanent = StreamGap(
        from_seq=1,
        through_seq=1,
        irrecoverable=True,
        detected_at=AT,
        reason_code="SOURCE_RETENTION_GAP",
        reason="Original source history expired",
        affected_execution_ids=("execution-A",),
    )
    delayed_gap = permanent.model_copy(
        update={
            "irrecoverable": False,
            "reason_code": "MISSING_SOURCE_EVENTS",
            "reason": "Await source replay",
            "affected_execution_ids": ("execution-B",),
        }
    )
    old = make_batch(1, event_seqs=(2,))
    report = make_batch(2, event_seqs=(), samples=False, gaps=(permanent,))
    buffer.enqueue(old, now=AT)
    buffer.enqueue(report, now=AT)
    buffer.acknowledge(make_ack(report, verified=0, ingested=2, unresolved=(permanent,)))
    buffer.acknowledge(make_ack(old, verified=0, ingested=2, unresolved=(delayed_gap,)))
    expected = permanent.model_copy(
        update={"affected_execution_ids": ("execution-A", "execution-B")}
    )
    assert buffer.progress("source-A", "stream-A").api_progress.unresolved_gaps == (expected,)


def test_gap_report_link_survives_restart_ack_and_repeated_eviction(tmp_path):
    limits = BufferLimits(max_age=timedelta(seconds=1))
    path = tmp_path / "reports.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway-A", limits=limits) as database:
        database.enqueue(make_batch(), now=AT)
        database.enqueue(make_batch(2, event_seqs=(), samples=True), now=AT + timedelta(seconds=1))
        gaps = database.pending_event_gaps("source-A", "stream-A")
        assert [(gap.from_seq, gap.through_seq) for gap in gaps] == [(1, 1)]
        report = make_batch(3, event_seqs=(), samples=False, gaps=gaps)
        database.enqueue(report, now=AT + timedelta(seconds=1))
        assert database.pending_event_gaps("source-A", "stream-A") == ()
    with GatewayBuffer(path, limits=limits) as database:
        assert database.pending_event_gaps("source-A", "stream-A") == ()
        database.enqueue(make_batch(4, event_seqs=()), now=AT + timedelta(seconds=2))
        assert database.pending_event_gaps("source-A", "stream-A") == gaps
        report2 = make_batch(5, event_seqs=(), samples=False, gaps=gaps)
        database.enqueue(report2, now=AT + timedelta(seconds=2))
        database.acknowledge(make_ack(report2))
        assert database.pending_event_gaps("source-A", "stream-A") == ()
    with GatewayBuffer(path, limits=limits) as database:
        assert database.pending_event_gaps("source-A", "stream-A") == ()
        assert len(database.losses()) == 3


@pytest.mark.parametrize(
    "tamper", ["missing_payload", "receipt", "capture", "interval", "reporter"]
)
def test_reopen_rejects_inconsistent_ledger_and_loss_ownership(tmp_path, tamper):
    limits = BufferLimits(max_age=timedelta(seconds=1))
    path = tmp_path / "corrupt-ledger.sqlite3"
    with GatewayBuffer(path, gateway_id="gateway-A", limits=limits) as database:
        database.enqueue(make_batch(), now=AT)
        database.enqueue(make_batch(2), now=AT + timedelta(seconds=1))
        if tamper == "missing_payload":
            database._db.execute("DELETE FROM outbound_batches WHERE batch_id='batch-2'")
        elif tamper == "receipt":
            database._db.execute("UPDATE batch_ledger SET ack='{}' WHERE batch_id='batch-2'")
        elif tamper == "capture":
            database._db.execute(
                "UPDATE gateway_identity SET last_capture_seq='00000000000000000003'"
            )
        elif tamper == "interval":
            database._db.execute("DELETE FROM loss_intervals")
        else:
            database._db.execute("UPDATE loss_intervals SET report_batch_id='batch-2'")
    with pytest.raises(BufferCorruption):
        GatewayBuffer(path, limits=limits)


def test_capture_time_survives_reopen_retry_and_loss_metadata(tmp_path):
    from tests.contract.test_transport import communication

    batch = make_batch(
        schema_version="1.2",
        communication=communication(),
        capture_time={
            "clock_epoch": "collector-before-restart",
            "monotonic_seconds": 10.0,
            "utc_read_span_seconds": 0.125,
        },
    )
    path = tmp_path / "capture-time.db"
    with GatewayBuffer(path, gateway_id="gateway-A") as buffer:
        buffer.enqueue(batch, now=AT, latest_read_seq=1)
        assert buffer.lease_next().batch == batch
    with GatewayBuffer(path, gateway_id="gateway-A") as buffer:
        replay = buffer.lease_next()
        assert replay.batch == batch
        loss = buffer._record_loss(replay, now=AT, reason="TEST_CAPTURE_LOSS")
        assert loss.capture_time == batch.body.capture_time
        encoded = buffer._db.execute("SELECT body FROM missing_ranges").fetchone()[0]
        assert buffer._loss_from_json(encoded).capture_time == batch.body.capture_time
        old = json.loads(encoded)
        old.pop("capture_time")
        assert buffer._loss_from_json(json.dumps(old)).capture_time is None
