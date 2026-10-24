"""Snapshot reads use committed PostgreSQL ingest data and original DTO values."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.ingestion import IngestionService, ingest_batches, source_streams
from apps.api.snapshot import SnapshotNotFound, read_snapshot
from contracts.models import UINT64_MAX
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 20, 12, tzinfo=UTC)
TAGS = {tag["logical_name"]: tag for tag in load_contract()["tags"]}


@pytest.fixture
def service(migrated):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated)
    for suffix in ("1", "foreign"):
        service.configure_source(
            source_id=f"source-{suffix}",
            stream_id=f"stream-{suffix}",
            gateway_id=f"gateway-{suffix}",
            line_id=LINE_ID,
            capture_generation=1,
            now=AT,
        )
    return service


def sample(tag="Line.State", value=2, *, status=0, acquired=AT, source=AT):
    return {
        "sample_id": f"sample-{tag}",
        "tag_id": tag,
        "source_timestamp": source,
        "server_timestamp": None,
        "acquired_at": acquired,
        "status_code": status,
        "reading": {"data_type": TAGS[tag]["data_type"], "value": value},
    }


def submit(
    service,
    *samples,
    seq=1,
    suffix="1",
    captured=AT,
    received=AT,
    generation=1,
    communication=None,
):
    body = BatchBody(
        **({"schema_version": "1.1", "communication": communication} if communication else {}),
        batch_id=f"batch-{generation}-{seq}",
        gateway_id=f"gateway-{suffix}",
        source_id=f"source-{suffix}",
        stream_id=f"stream-{suffix}",
        capture_generation=generation,
        capture_seq=seq,
        captured_at=captured,
        samples=samples,
    )
    service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)),
        authenticated_gateway_id=body.gateway_id,
        authenticated_source_id=body.source_id,
        authenticated_stream_id=body.stream_id,
        now=received,
    )


def read(service, **kwargs):
    with service.engine.connect() as connection:
        return read_snapshot(
            connection,
            line_id=LINE_ID,
            source_id="source-1",
            stream_id="stream-1",
            now=kwargs.get("now", AT),
            prefer_complete_capture=kwargs.get("prefer_complete_capture", False),
        )


def test_never_observed_and_explicit_missing(service):
    result = read(service)
    assert result.health.state == "NEVER_OBSERVED"
    assert len(result.missing_tag_ids) == 48
    assert result.screen.view.samples == ()
    assert result.screen.view.state == "UNKNOWN"
    assert result.screen.view.execution_id is None
    assert result.screen.view.control_restricted
    assert result.health.heartbeat == "UNKNOWN"


def test_latest_capture_wins_over_replay_and_source_timestamps(service):
    submit(service, sample(value=2, source=AT - timedelta(days=1)), seq=9)
    submit(
        service,
        sample(value=0, source=AT + timedelta(days=1)),
        seq=1,
        received=AT + timedelta(seconds=1),
    )
    submit(service, sample(value=5), seq=100, suffix="foreign")
    result = read(service, now=AT + timedelta(seconds=1))
    assert result.screen.view.state == "UNKNOWN"  # legacy capture lacks heartbeat proof
    assert result.screen.view.samples[0].reading.value == 2
    assert result.evidence[0].capture_seq == 9
    assert result.health.state == "DEGRADED"  # Other 47 tags never observed.
    assert result.evidence[0].fresh


@pytest.mark.parametrize("prefer_complete_capture", [False, True])
def test_latest_per_tag_preserves_bad_null_and_uint64_decimal_wire(
    service, prefer_complete_capture
):
    submit(service, sample(), sample("Line.Heartbeat", UINT64_MAX), seq=1)
    submit(service, sample(value=None, status=0x80000000), seq=2)
    result = read(service, prefer_complete_capture=prefer_complete_capture)
    values = {item.tag_id: item for item in result.screen.view.samples}
    assert values["Line.State"].reading.value is None
    assert values["Line.State"].status_code == 0x80000000
    assert result.screen.view.state == "UNKNOWN"
    wire = result.model_dump(mode="json")
    heartbeat = next(
        item for item in wire["screen"]["view"]["samples"] if item["tag_id"] == "Line.Heartbeat"
    )
    assert heartbeat["reading"]["value"] == str(UINT64_MAX)
    assert wire["evidence"][0]["capture_generation"] == "1"
    assert wire["screen"]["revision"] == "0"


def test_partial_capture_retains_old_bad_after_5000_unrelated_captures(service):
    submit(service, sample(value=2), seq=1)
    submit(service, sample(value=None, status=0x80000000), seq=2)
    submit(service, sample("Line.Heartbeat", 1), seq=3)
    # Build durable intake fixtures in one transaction; capture order and bodies
    # remain realistic without running 5,000 separate ingestion transactions.
    with service.engine.begin() as connection:
        template = dict(
            connection.execute(
                sa.select(ingest_batches).where(ingest_batches.c.batch_id == "batch-1-3")
            )
            .mappings()
            .one()
        )
        original = BatchBody.model_validate(template["body"])
        rows = []
        for seq in range(4, 5004):
            body = original.model_copy(
                update={
                    "batch_id": f"batch-1-{seq}",
                    "capture_seq": seq,
                    "samples": tuple(
                        item.model_copy(update={"sample_id": f"heartbeat-{seq}"})
                        for item in original.samples
                    ),
                }
            )
            rows.append(
                dict(
                    template,
                    batch_id=body.batch_id,
                    capture_seq=seq,
                    body=body.model_dump(mode="json"),
                    content_sha256=batch_digest(body),
                )
            )
        connection.execute(ingest_batches.insert(), rows)

    snapshots = []
    for prefer_complete_capture in (False, True):
        result = read(service, prefer_complete_capture=prefer_complete_capture)
        values = {item.tag_id: item for item in result.screen.view.samples}
        evidence = {item.tag_id: item for item in result.evidence}
        assert values["Line.State"].reading.value is None
        assert values["Line.State"].status_code == 0x80000000
        assert evidence["Line.State"].capture_seq == 2
        assert evidence["Line.Heartbeat"].capture_seq == 5003
        assert "Line.State" not in result.missing_tag_ids
        assert result.health.state == "DEGRADED"
        value = result.model_dump()
        value["screen"].pop("sync_id")
        snapshots.append(value)
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("future", [False, True])
def test_arrival_does_not_refresh_old_or_future_capture(service, future):
    captured = AT + timedelta(seconds=1) if future else AT - timedelta(hours=1)
    submit(service, sample(acquired=captured), captured=captured)
    result = read(service)
    assert result.health.state == "STALE"
    assert result.screen.view.state == "UNKNOWN"
    assert not result.evidence[0].fresh


def test_generation_rotation_excludes_previous_capture_evidence(service):
    submit(service, sample(), seq=100)
    with service.engine.begin() as connection:
        connection.execute(
            source_streams.update()
            .where(source_streams.c.source_id == "source-1")
            .values(capture_generation=2)
        )
    assert read(service).health.state == "NEVER_OBSERVED"
    submit(service, sample(value=3), generation=2)
    assert read(service).screen.view.state == "UNKNOWN"  # raw latest state remains in samples


def test_binding_and_caller_transaction_are_respected(service):
    submit(service, sample())
    with service.engine.begin() as connection:
        with pytest.raises(SnapshotNotFound):
            read_snapshot(
                connection,
                line_id="foreign-line",
                source_id="source-1",
                stream_id="stream-1",
                now=AT,
            )
        # Observe this transaction's new binding without erasing immutable
        # intake/proof. Generation 2 has never supplied a capture.
        connection.execute(
            sa.text("UPDATE source_streams SET capture_generation=2 WHERE source_id='source-1'")
        )
        result = read_snapshot(
            connection, line_id=LINE_ID, source_id="source-1", stream_id="stream-1", now=AT
        )
        assert result.health.state == "NEVER_OBSERVED"
        assert connection.in_transaction()


def communication():
    return {
        "state": "GOOD",
        "connected": True,
        "read_age_seconds": 0.0,
        "heartbeat_ages_seconds": {
            "Line.Heartbeat": 0.0,
            "Infeed.LinkHeartbeat": 0.0,
            "Sealer.LinkHeartbeat": 0.0,
            "Outfeed.LinkHeartbeat": 0.0,
        },
    }


def test_monotonic_communication_evidence_ages_without_new_captures(service):
    submit(service, sample(), communication=communication())
    assert read(service).health.heartbeat == "GOOD"
    assert read(service, now=AT + timedelta(seconds=3)).health.heartbeat == "DELAYED"
    assert read(service, now=AT + timedelta(seconds=5)).health.heartbeat == "ALARM"
    assert read(service, now=AT - timedelta(seconds=1)).health.heartbeat == "UNKNOWN"
    assert read(service).screen.view.control_restricted  # other control gates still required


def test_new_legacy_capture_cannot_borrow_older_good_communication(service):
    submit(service, sample(), seq=1, communication=communication())
    submit(service, sample(value=0), seq=2)
    result = read(service)
    assert result.health.heartbeat == "UNKNOWN"
    assert result.health.communication is None


def test_backfill_does_not_replace_latest_communication_evidence(service):
    submit(service, sample(), seq=8, communication=communication())
    old = communication()
    old["state"] = "ALARM"
    old["read_age_seconds"] = 6.0
    submit(service, sample(value=0), seq=1, communication=old)
    assert read(service).health.heartbeat == "GOOD"


def test_future_gateway_clock_cannot_resurrect_old_stored_health(service):
    future = AT + timedelta(seconds=10)
    submit(
        service,
        sample(acquired=future),
        captured=future,
        received=AT,
        communication=communication(),
    )
    assert read(service).health.heartbeat == "UNKNOWN"
    assert read(service, now=future).health.heartbeat == "ALARM"
    assert read(service, now=future).health.state == "STALE"


def test_alarm_heartbeat_masks_derived_state_even_when_raw_tags_are_fresh(service):
    alarm = communication()
    alarm["state"] = "ALARM"
    alarm["heartbeat_ages_seconds"]["Line.Heartbeat"] = 5.0
    submit(service, sample(value=2), communication=alarm)
    result = read(service)
    assert result.screen.view.samples[0].reading.value == 2
    assert result.screen.view.state == "UNKNOWN"
    assert result.health.heartbeat == "ALARM"
