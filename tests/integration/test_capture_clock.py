"""Durable clock proof, exact source binding and original analog samples on PostgreSQL."""

from dataclasses import replace
from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError

from apps.api.bootstrap import bootstrap_reference_data
from apps.api.capture_clock import SameHostClockPolicy, read_capture_clock
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.trend_history import TrendRetention, cleanup_trends, raw_observations
from contracts.transport import BatchBody, GatewayBatch, batch_digest
from tests.integration.test_trend_history import AT, SCOPE, TAG, batch, submit


def same_host():
    return SameHostClockPolicy(
        gateway_id="gateway-1",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        capture_generation=1,
        valid_from_api_utc=AT - timedelta(days=1),
        valid_until_api_utc=AT + timedelta(days=1),
    )


@pytest.fixture
def service(migrated):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated, trend_clock_policy=same_host())
    service.configure_source(**SCOPE, gateway_id="gateway-1", capture_generation=1, now=AT)
    return service


def decision(service, seq):
    with service.engine.connect() as connection:
        return read_capture_clock(connection, "gateway-1", f"batch-{seq}")


def modified(original, update):
    data = original.body.model_dump(mode="python")
    update(data)
    body = BatchBody.model_validate(data)
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


def test_proof_survives_raw_cleanup_and_dynamic_restart_epoch(service):
    value = batch(1)
    submit(service, value)
    restart = modified(
        batch(2, AT + timedelta(seconds=1)),
        lambda data: data["capture_time"].update(
            clock_epoch="clock-after-restart", monotonic_seconds=1.0
        ),
    )
    submit(service, restart)
    assert decision(service, 1).status == decision(service, 2).status == "GOOD"
    assert decision(service, 1).canonical_api_utc == AT
    with service.engine.begin() as connection:
        raw = connection.execute(
            sa.text("SELECT acquired_at,observation FROM analog_history_raw WHERE capture_seq=1")
        ).one()
        assert raw.acquired_at == AT
        assert raw.observation["sample"] == value.body.samples[0].model_dump(mode="json")
        cleanup_trends(connection, now=AT + timedelta(days=8), retention=TrendRetention())
        assert connection.execute(sa.text("SELECT count(*) FROM analog_history_raw")).scalar() == 0
    assert decision(service, 1).status == "GOOD"
    assert decision(service, 2).status == "GOOD"


def test_disabled_mismatch_future_and_expired_stay_unknown(service):
    service.trend_clock_policy = None
    submit(service, batch(1))
    assert decision(service, 1).status == "UNKNOWN"
    assert decision(service, 1).canonical_api_utc is None
    service.trend_clock_policy = replace(same_host(), capture_generation=2)
    submit(service, batch(2, AT + timedelta(seconds=1)))
    assert decision(service, 2).status == "UNKNOWN"
    service.trend_clock_policy = same_host()
    submit(service, batch(3, AT + timedelta(minutes=3)))
    assert decision(service, 3).reason_code == "FUTURE_CAPTURE"
    service.trend_clock_policy = replace(same_host(), valid_until_api_utc=AT + timedelta(seconds=1))
    submit(service, batch(4, AT + timedelta(minutes=4)))
    assert decision(service, 4).status == "UNKNOWN"


def test_atomic_all_capture_and_immutable_evidence(service, monkeypatch):
    digital = modified(
        batch(1),
        lambda data: data["samples"][0].update(
            tag_id="Line.State", reading={"data_type": "Int32", "value": 3}
        ),
    )

    def fail(connection):
        assert (
            connection.execute(sa.text("SELECT count(*) FROM capture_clock_evidence")).scalar() == 1
        )
        raise RuntimeError("clock proof transaction rollback")

    monkeypatch.setattr(service, "_before_commit", fail)
    with pytest.raises(RuntimeError):
        submit(service, digital)
    assert decision(service, 1) is None
    monkeypatch.setattr(service, "_before_commit", lambda connection: None)
    submit(service, digital)
    submit(service, digital)
    assert decision(service, 1).status == "GOOD"
    for sql in (
        "UPDATE capture_clock_evidence SET status='UNKNOWN'",
        "DELETE FROM capture_clock_evidence",
        "TRUNCATE capture_clock_evidence",
    ):
        with pytest.raises(DBAPIError), service.engine.begin() as connection:
            connection.execute(sa.text(sql))


def test_reversal_invalidates_effective_proof_without_mutating_original_decision(service):
    submit(service, batch(1, AT + timedelta(seconds=1)))
    submit(service, batch(3, AT + timedelta(seconds=61)))
    assert decision(service, 3).status == "GOOD"
    submit(service, batch(2, AT + timedelta(seconds=62)))
    assert decision(service, 3).status == "UNKNOWN"
    assert decision(service, 3).reason_code == "CLOCK_REVERSAL"
    with service.engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT status FROM capture_clock_evidence WHERE capture_seq=3")
            ).scalar()
            == "GOOD"
        )
        rows = raw_observations(connection, SCOPE, TAG, AT, AT + timedelta(minutes=2))
        assert next(r for r in rows if r.capture_seq == 3).time_quality == "UNKNOWN"


def test_schema17_compatibility_and_empty_migration_roundtrip(engine):
    with engine.connect() as connection:
        config = migration_config(connection)
        command.upgrade(config, "0018_clock_evidence")
        command.downgrade(config, "0017_guest_viewer")
        command.upgrade(config, "0018_clock_evidence")
        command.downgrade(config, "0017_guest_viewer")
    bootstrap_reference_data(engine)
    service = IngestionService(engine)
    service.configure_source(**SCOPE, gateway_id="gateway-1", capture_generation=1, now=AT)
    submit(service, batch(1))
    with engine.connect() as connection:
        assert read_capture_clock(connection, "gateway-1", "batch-1") is None
        assert connection.execute(sa.text("SELECT count(*) FROM ingest_batches")).scalar() == 1


def test_projection_preserves_original_nonzero_offset_sample(service):
    from tests.integration.test_trend_history import policy

    service.trend_clock_policy = policy().model_copy(
        update={"offset_lower_seconds": 0.9, "offset_upper_seconds": 1.1}
    )
    submit(service, batch(1))
    assert decision(service, 1).canonical_api_utc == AT + timedelta(seconds=1)
    with service.engine.connect() as connection:
        original = connection.execute(
            sa.text("SELECT acquired_at,canonical_utc,observation FROM analog_history_raw")
        ).one()
        assert original.acquired_at == AT
        assert original.canonical_utc == AT + timedelta(seconds=1)
        assert original.observation["sample"]["acquired_at"] == "2026-10-23T12:00:00Z"
        projected = raw_observations(
            connection, SCOPE, TAG, AT + timedelta(seconds=1), AT + timedelta(seconds=2)
        )
        assert projected[0].sample.acquired_at == AT + timedelta(seconds=1)


def test_used_evidence_blocks_lossy_downgrade(service):
    submit(service, batch(1))
    with pytest.raises(DBAPIError), service.engine.connect() as connection:
        command.downgrade(migration_config(connection), "0017_guest_viewer")
    assert decision(service, 1).status == "GOOD"
