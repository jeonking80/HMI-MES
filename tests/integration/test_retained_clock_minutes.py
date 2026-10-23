"""Retained minutes lose known-time claims after raw expiry, including projected UTC."""

from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic import command

from apps.api.bootstrap import bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.trend_history import TrendRetention, cleanup_trends
from tests.integration.test_trend_history import AT, SCOPE, batch, policy, read, submit


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
