"""Legacy analog intake preserves unknown time quality without capture clock coordinates."""

from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic import command

from apps.api.bootstrap import bootstrap_reference_data
from apps.api.capture_clock import read_capture_clock
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.trend_history import raw_observations
from contracts.transport import BatchBody, GatewayBatch, batch_digest
from tests.integration.test_trend_history import AT, SCOPE, TAG, batch, submit


@pytest.mark.parametrize("revision", ["0015_analog_history", "0017_guest_viewer"])
@pytest.mark.parametrize("wire_version", ["1.0", "1.1"])
def test_legacy_analog_without_capture_clock_remains_unknown(engine, revision, wire_version):
    with engine.connect() as connection:
        command.upgrade(migration_config(connection), revision)
    bootstrap_reference_data(engine)
    service = IngestionService(engine)
    service.configure_source(**SCOPE, gateway_id="gateway-1", capture_generation=1, now=AT)
    raw = batch(1).body.model_dump(mode="python")
    raw["schema_version"] = wire_version
    raw.pop("capture_time")
    if wire_version == "1.0":
        raw.pop("communication")
    body = BatchBody.model_validate(raw)
    value = GatewayBatch(body=body, content_sha256=batch_digest(body))
    first = submit(service, value)
    assert submit(service, value) == first
    with engine.connect() as connection:
        observation = connection.execute(
            sa.text("SELECT observation FROM analog_history_raw")
        ).scalar_one()
        assert observation["time_quality"] == "UNKNOWN"
        assert observation["scope"]["clock_epoch"] == "unproved-clock"
        rows = raw_observations(connection, SCOPE, TAG, AT, AT + timedelta(seconds=1))
        assert len(rows) == 1 and rows[0].time_quality == "UNKNOWN"
        assert read_capture_clock(connection, "gateway-1", "batch-1") is None
