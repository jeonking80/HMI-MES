"""Retained minutes lose known-time claims after raw expiry, including projected UTC."""

from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic import command

from apps.api.bootstrap import bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.trend_history import TrendRetention, cleanup_trends
from contracts.transport import BatchBody, GatewayBatch, batch_digest
from tests.integration.test_trend_history import AT, SCOPE, TAG, batch, policy, read, submit


@pytest.mark.parametrize(
    "revision,offset",
    [
        ("0015_analog_history", 0.0),
        ("0017_guest_viewer", 0.0),
        ("0018_clock_evidence", 0.0),
        ("0018_clock_evidence", 1.0),
    ],
)
def test_delayed_reversal_invalidates_original_or_projected_retained_minute(
    engine, revision, offset
):
    with engine.connect() as connection:
        command.upgrade(migration_config(connection), revision)
    bootstrap_reference_data(engine)
    configured = policy().model_copy(
        update={"offset_lower_seconds": offset, "offset_upper_seconds": offset}
    )
    service = IngestionService(engine, trend_clock_policy=configured)
    service.configure_source(**SCOPE, gateway_id="gateway-1", capture_generation=1, now=AT)
    submit(service, batch(1, AT + timedelta(seconds=58.5), 1.0))
    submit(service, batch(3, AT + timedelta(seconds=59.5), 3.0))
    target = 1 if offset else 0
    assert read(service)[target]["value"] == (3.0 if offset else 2.0)
    with engine.begin() as connection:
        cleanup_trends(connection, now=AT + timedelta(days=8), retention=TrendRetention())
        assert connection.execute(sa.text("SELECT count(*) FROM analog_history_raw")).scalar() == 0
    # Capture 2 proves capture 3 reversed. Its raw sample has expired, while the
    # old immutable proof still identifies the minute that used projected UTC.
    submit(
        service,
        batch(2, AT + timedelta(seconds=60.5), 2.0),
        now=AT + timedelta(days=8, minutes=1),
    )
    points = read(service)
    assert points[target]["quality"] == "TIME_UNKNOWN"
    assert points[target]["value"] is None
    assert points[target]["min"] is None and points[target]["max"] is None
    assert points[target]["valid_count"] == 0
    if offset:
        # Original minute 0 contains only the still-valid capture 1 after UTC
        # projection. Invalidating by raw time would blank this wrong minute.
        assert points[0]["value"] == 1.0


@pytest.mark.parametrize(
    "revision,offset",
    [
        ("0015_analog_history", 0.0),
        ("0017_guest_viewer", 0.0),
        ("0018_clock_evidence", 0.0),
        ("0018_clock_evidence", 1.0),
    ],
)
def test_partial_cleanup_never_resurrects_superseded_good_raw_or_minute(engine, revision, offset):
    with engine.connect() as connection:
        command.upgrade(migration_config(connection), revision)
    bootstrap_reference_data(engine)
    configured = policy().model_copy(
        update={"offset_lower_seconds": offset, "offset_upper_seconds": offset}
    )
    service = IngestionService(engine, trend_clock_policy=configured)
    service.configure_source(**SCOPE, gateway_id="gateway-1", capture_generation=1, now=AT)
    original = batch(1, AT + timedelta(seconds=57.5), 1.0)
    data = original.body.model_dump(mode="python")
    data["samples"] = list(data["samples"])
    data["samples"].append(
        dict(
            data["samples"][0],
            sample_id="pressure-1",
            tag_id="Sealer.AirPressure",
            reading={"data_type": "Double", "value": 0.6},
        )
    )
    first = BatchBody.model_validate(data)
    original = GatewayBatch(body=first, content_sha256=batch_digest(first))
    submit(service, original)
    submit(service, batch(3, AT + timedelta(seconds=58), 3.0))
    submit(service, batch(4, AT + timedelta(seconds=57.1), 4.0))
    # Complete raw selection histories preserve valid pre-cutoff values.
    assert read(service)[0]["quality"] == "PARTIAL"
    assert read(service)[0]["valid_count"] > 0
    with engine.begin() as connection:
        assert cleanup_trends(
            connection, now=AT + timedelta(days=8), retention=TrendRetention(delete_limit=1)
        ) == (1, 0)
        assert connection.execute(
            sa.text("""SELECT capture_seq FROM analog_history_raw
            WHERE tag_id=:tag ORDER BY capture_seq"""),
            dict(tag=TAG),
        ).scalars().all() == [1, 3]
        assert (
            connection.execute(
                sa.text("""SELECT observation->>'time_quality'
            FROM analog_history_raw WHERE tag_id='Sealer.AirPressure'""")
            ).scalar_one()
            == "GOOD"
        )
        assert connection.execute(
            sa.text("""SELECT observation->'sample'
            FROM analog_history_raw WHERE tag_id=:tag AND capture_seq=1"""),
            dict(tag=TAG),
        ).scalar_one() == original.body.samples[0].model_dump(mode="json")
    # Check the vulnerable interval immediately after cleanup, before another
    # ingestion request can repair or recompute any projection.
    assert read(service, "second")[57 + int(offset)]["quality"] == "TIME_UNKNOWN"
    assert read(service)[0]["quality"] == "TIME_UNKNOWN"
    submit(
        service, batch(2, AT + timedelta(seconds=58.1), 2.0), now=AT + timedelta(days=8, minutes=1)
    )
    point = read(service)[0]
    assert point["quality"] == "TIME_UNKNOWN"
    assert point["value"] is None and point["valid_count"] == 0
