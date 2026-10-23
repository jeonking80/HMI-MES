"""Actual OPC/HTTP/PG recovery reads stay inside the API's missing interval.

The 1,800 outbound captures are deliberately preloaded, rather than represented
as an actual 30 minute outage. CX-19 must still measure that elapsed workload.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_gateway_pipeline import make_finished_run, register
from test_gateway_pipeline import pipeline as pipeline

from apps.simulator.storage import StateEventInput
from contracts.models import EventPage, ReadEventsRequest
from contracts.transport import BatchBody, GatewayBatch, batch_digest, gateway_batch_digest


def with_digest(body):
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


@pytest.mark.parametrize("gap_length,backlog", [(2, 1800), (1200, 0)])
async def test_original_gap_limits_replay_without_amplifying_backlog(
    pipeline, gap_length, backlog, record_property
):
    runtime, server, client, pump, service = pipeline
    if gap_length == 2:
        await make_finished_run(runtime, server, client)
        register(service, runtime)
    else:
        # Valid source-history fixtures, persisted through the public atomic
        # append API. No production counter or final-summary mutation occurs.
        runtime.storage.append_control_events(
            tuple(
                StateEventInput(
                    occurred_at=datetime.now(UTC),
                    payload={
                        "previous_state": "STOPPED",
                        "state": "READY",
                        "control_version": index,
                    },
                )
                for index in range(1, 1601)
            )
        )
        await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=2)

    source_events = []
    after = 0
    while True:
        page = await client.read_events(
            ReadEventsRequest(source_id=pump.source_id, stream_id=pump.stream_id, after_seq=after)
        )
        assert isinstance(page, EventPage)
        source_events.extend(page.events)
        after = page.page_last_seq
        if not page.has_more:
            break
    assert len(source_events) > gap_length
    originals = {event.seq: event.model_dump(mode="json") for event in source_events}
    observation = await client.read_tags()
    assert len(observation.samples) == 48
    body = BatchBody(
        schema_version="1.2",
        batch_id="bounds-seed-" + uuid4().hex,
        gateway_id=pump.buffer.gateway_id,
        source_id=pump.source_id,
        stream_id=pump.stream_id,
        capture_generation=1,
        capture_seq=1,
        captured_at=observation.acquired_at,
        capture_time=observation.capture_time,
        communication={
            "state": observation.health.state,
            "connected": observation.health.connected,
            "read_age_seconds": observation.health.read_age_seconds,
            "heartbeat_ages_seconds": dict(observation.health.heartbeat_ages_seconds),
        },
        samples=observation.samples,
        events=source_events[gap_length:],
    )
    seed = with_digest(body)
    pump.buffer.enqueue(seed, now=body.captured_at, latest_read_seq=source_events[-1].seq)
    assert await pump.deliver_once()
    progress = pump.buffer.progress(pump.source_id, pump.stream_id)
    gap = progress.api_progress.unresolved_gaps[0]
    assert (gap.from_seq, gap.through_seq, gap.irrecoverable) == (1, gap_length, False)
    assert progress.api_progress.verified_through_seq == 0

    for index in range(backlog):
        queued_body = body.model_copy(
            update={"batch_id": f"preloaded-{index}", "capture_seq": index + 2, "events": ()}
        )
        pump.buffer.enqueue(with_digest(queued_body), now=body.captured_at)
    before = pump.buffer.status(now=datetime.now(UTC))
    assert before.pending_batches == backlog
    assert not before.age_limit_reached
    source_read = client.read_events
    requests = []
    captured = []
    enqueue = pump._enqueue

    async def track_request(request):
        requests.append(request)
        return await source_read(request)

    def track_enqueue(**kwargs):
        batch = enqueue(**kwargs)
        captured.append(batch)
        return batch

    client.read_events = track_request
    pump._enqueue = track_enqueue
    try:
        for heartbeat in range(10, 13):
            await server.publish_snapshot(acquired_at=datetime.now(UTC), heartbeat=heartbeat)
            result = await pump.capture_once()
            assert result.sample_count == 48 and result.event_count == min(1000, gap_length)
    finally:
        client.read_events = source_read
        pump._enqueue = enqueue
    assert [(request.after_seq, request.limit) for request in requests] == [
        (0, min(1000, gap_length))
    ] * 3
    for batch in captured:
        assert [event.seq for event in batch.body.events] == list(
            range(1, min(1000, gap_length) + 1)
        )
        assert all(
            event.model_dump(mode="json") == originals[event.seq] for event in batch.body.events
        )
        assert batch.content_sha256 == batch_digest(batch.body)
        assert gateway_batch_digest(batch) == gateway_batch_digest(
            GatewayBatch.model_validate_json(batch.model_dump_json())
        )
    after_capture = pump.buffer.status(now=datetime.now(UTC))
    assert after_capture.pending_batches == backlog + 3
    assert after_capture.queue_bytes < 100_000_000 and not pump.buffer.losses()
    added_bytes = after_capture.queue_bytes - before.queue_bytes
    if gap_length == 2:
        # Compare actual accounted queue growth with the old overread's payload
        # lower bound. Row overhead only makes the old queue larger.
        overread_payload_bytes = 0
        for batch in captured:
            overread = with_digest(batch.body.model_copy(update={"events": tuple(source_events)}))
            encoded = json.dumps(
                overread.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            overread_payload_bytes += len(encoded)
        assert added_bytes < overread_payload_bytes
    record_property("preloaded_captures", backlog)
    record_property("gap_events", gap_length)
    record_property("requested_limits", ",".join(str(request.limit) for request in requests))
    record_property("added_queue_bytes", added_bytes)

    # The repair payload travels through actual authenticated HTTP. Only its
    # matching immutable committed ACK can release it or advance API progress.
    replay = captured[0]
    ack = await pump.intake.deliver(replay)
    assert ack.batch_id == replay.body.batch_id and ack.content_sha256 == replay.content_sha256
    pump.buffer.acknowledge(ack)
    repaired = pump.buffer.progress(pump.source_id, pump.stream_id).api_progress
    assert repaired.verified_through_seq == (source_events[-1].seq if gap_length == 2 else 1000)
    if gap_length == 2:
        assert repaired.verified_through_seq == source_events[-1].seq
        assert not repaired.unresolved_gaps
        before_totals = service.get_totals("run-A")
        duplicate_ack = await pump.intake.deliver(replay)
        assert duplicate_ack == ack
        assert service.get_totals("run-A") == before_totals
