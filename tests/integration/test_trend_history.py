"""History atomicity, retained projection and strict API on disposable PostgreSQL."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from alembic import command

from apps.api.alarm_consumer import ConfiguredClockPolicy
from apps.api.bootstrap import LINE_ID, bootstrap_reference_data
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.trend_history import TrendRetention, cleanup_trends, read_trends
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 23, 12, tzinfo=UTC)
TAG = "Sealer.TemperaturePV"
SCOPE = dict(line_id=LINE_ID, source_id="simulator-1", stream_id="stream-1")


def policy():
    return ConfiguredClockPolicy(
        policy_id="test-clock",
        policy_version="v1",
        reference_id="test-reference",
        method="CONFIGURED_API_REFERENCE",
        guarantee_kind="SAME_HOST_SHARED_KERNEL_UTC",
        guarantee_basis="Disposable test shared UTC reference",
        gateway_id="gateway-1",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        capture_generation=1,
        clock_epoch="clock-1",
        valid_from_api_utc=AT - timedelta(days=2),
        valid_until_api_utc=AT + timedelta(days=2),
        offset_lower_seconds=0.0,
        offset_upper_seconds=0.0,
    )


def batch(seq, at=AT, value=150.0, status=0):
    body = BatchBody(
        schema_version="1.2",
        batch_id=f"batch-{seq}",
        gateway_id="gateway-1",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        capture_generation=1,
        capture_seq=seq,
        captured_at=at,
        capture_time=dict(
            clock_epoch="clock-1", monotonic_seconds=float(seq + 100), utc_read_span_seconds=0.0
        ),
        communication=dict(
            state="GOOD",
            connected=True,
            read_age_seconds=0.0,
            heartbeat_ages_seconds={
                key: 0.0
                for key in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        ),
        samples=[
            dict(
                sample_id=f"sample-{seq}",
                tag_id=TAG,
                source_timestamp=at,
                server_timestamp=None,
                acquired_at=at,
                status_code=status,
                reading=dict(data_type="Double", value=value),
            )
        ],
    )
    return GatewayBatch(body=body, content_sha256=batch_digest(body))


def submit(service, value, now=AT + timedelta(minutes=2)):
    return service.ingest(
        value,
        authenticated_gateway_id="gateway-1",
        authenticated_source_id=SCOPE["source_id"],
        authenticated_stream_id=SCOPE["stream_id"],
        now=now,
    )


@pytest.fixture
def service(migrated):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated, trend_clock_policy=policy())
    service.configure_source(**SCOPE, gateway_id="gateway-1", capture_generation=1, now=AT)
    return service


def read(service, resolution="minute", start=AT, end=AT + timedelta(minutes=2)):
    with service.engine.connect() as connection:
        return read_trends(
            connection,
            scope=SCOPE,
            tags=[TAG],
            start=start,
            end=end,
            resolution=resolution,
            now=AT + timedelta(minutes=3),
        )["series"][0]["points"]


def scalar(service, sql):
    with service.engine.connect() as connection:
        return connection.execute(sa.text(sql)).scalar_one()


def test_atomic_retry_delayed_missing_and_time_unknown(service, monkeypatch):
    def fail(connection):
        assert connection.execute(sa.text("SELECT count(*) FROM analog_history_raw")).scalar() == 1
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(service, "_before_commit", fail)
    with pytest.raises(RuntimeError):
        submit(service, batch(1))
    assert scalar(service, "SELECT count(*) FROM analog_history_raw") == 0
    assert scalar(service, "SELECT count(*) FROM ingest_batches") == 0
    monkeypatch.setattr(service, "_before_commit", lambda connection: None)
    submit(service, batch(1))
    submit(service, batch(1))
    assert scalar(service, "SELECT count(*) FROM analog_history_raw") == 1
    assert read(service)[0]["value"] == 150.0
    assert read(service)[1]["quality"] == "MISSING"
    submit(service, batch(2, AT + timedelta(seconds=12), 154.0))
    assert read(service)[0]["value"] == 152.0
    assert read(service)[0]["valid_count"] == 2
    service.trend_clock_policy = None
    submit(service, batch(3, AT + timedelta(seconds=24), 999.0))
    assert read(service)[0]["value"] == 152.0
    assert read(service, "second")[24]["quality"] == "TIME_UNKNOWN"
    assert read(service, "second")[24]["value"] is None


def test_completed_minute_retained_and_cleanup_bounded(service):
    for seq in range(1, 5):
        submit(service, batch(seq, AT + timedelta(seconds=seq), float(seq)))
    with service.engine.begin() as connection:
        counts = cleanup_trends(
            connection, now=AT + timedelta(days=8), retention=TrendRetention(delete_limit=2)
        )
    assert counts == (2, 0)
    assert scalar(service, "SELECT count(*) FROM ingest_batches") == 4
    # Whole minute must use retained projection, not remaining partial raw rows.
    assert read(service)[0]["value"] == 2.5
    assert read(service)[0]["valid_count"] == 4


def test_migration_roundtrip_and_schema14_gate(engine):
    with engine.connect() as connection:
        config = migration_config(connection)
        command.upgrade(config, "0015_analog_history")
        command.downgrade(config, "0014_mixed_alarm_runtime")
        command.upgrade(config, "0015_analog_history")
        command.downgrade(config, "0014_mixed_alarm_runtime")
    bootstrap_reference_data(engine)
    service = IngestionService(engine)
    service.configure_source(**SCOPE, gateway_id="gateway-1", capture_generation=1, now=AT)
    submit(service, batch(1))
    assert scalar(service, "SELECT count(*) FROM ingest_batches") == 1


@pytest.mark.asyncio
async def test_viewer_http_and_strict_boundaries(service):
    from apps.api.auth import UserAuth
    from apps.api.user_http import UserHTTPConfig, create_user_app
    from tests.integration.test_screen_http import ORIGIN, account

    auth = UserAuth(service.engine, allowed_origins={ORIGIN})
    session = account(auth, "trend-viewer", {"viewer"})
    app = create_user_app(auth, UserHTTPConfig(origin=ORIGIN))
    params = list(SCOPE.items()) + [
        ("tag", TAG),
        ("from", AT.isoformat()),
        ("to", (AT + timedelta(hours=1)).isoformat()),
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        assert (await client.get("/api/v1/trends", params=params)).status_code == 401
        client.cookies.set("hmi_session", session.token)
        response = await client.get("/api/v1/trends", params=params)
        assert response.status_code == 200, response.text
        assert len(response.json()["series"][0]["points"]) == 3600
        assert response.headers["cache-control"] == "no-store"
        for extra in [[("unknown", "x")], [("line_id", LINE_ID)], [("tag", TAG)]]:
            assert (await client.get("/api/v1/trends", params=params + extra)).status_code == 422
        for resolution, delta in [
            ("second", timedelta(hours=1, microseconds=1)),
            ("minute", timedelta(days=7, microseconds=1)),
        ]:
            query = list(SCOPE.items()) + [
                ("tag", TAG),
                ("from", AT.isoformat()),
                ("to", (AT + delta).isoformat()),
                ("resolution", resolution),
            ]
            assert (await client.get("/api/v1/trends", params=query)).status_code == 422


def test_cross_minute_clock_reversal_remains_unknown(service):
    submit(service, batch(1, AT + timedelta(seconds=59)))
    submit(service, batch(2, AT + timedelta(seconds=58)))
    submit(service, batch(3, AT + timedelta(minutes=1, seconds=1)))
    points = read(service, "second")
    assert points[59]["quality"] == "GOOD"
    assert points[58]["quality"] == "TIME_UNKNOWN"
    assert points[61]["quality"] == "TIME_UNKNOWN"


def test_boundary_projection_and_raw_latest_capture(service):
    submit(service, batch(1, AT + timedelta(seconds=1), 1.0))
    submit(service, batch(2, AT + timedelta(seconds=1, microseconds=500000), 3.0))
    assert read(service, "second")[1]["value"] == 3.0
    assert read(service, start=AT + timedelta(seconds=2))[0]["value"] is None
    assert read(service, start=AT + timedelta(seconds=2))[0]["expected_count"] == 60


def test_future_timestamps_do_not_escape_retention(service):
    submit(service, batch(1, AT + timedelta(days=1)))
    with service.engine.begin() as connection:
        counts = cleanup_trends(
            connection,
            now=AT + timedelta(days=8, minutes=3),
            retention=TrendRetention(delete_limit=1),
        )
    assert counts == (1, 0)


@pytest.mark.parametrize("delayed", [False, True])
def test_digital_only_clock_reversal_invalidates_later_analog(service, delayed):
    def digital(seq, seconds):
        original = batch(seq, AT + timedelta(seconds=seconds))
        value = original.body.model_dump(mode="python")
        value["samples"][0]["tag_id"] = "Line.State"
        value["samples"][0]["reading"] = {"data_type": "Int32", "value": 3}
        body = BatchBody.model_validate(value)
        return GatewayBatch(body=body, content_sha256=batch_digest(body))

    # Digital-only predecessors must remain usable even when a later analog
    # capture has replaced the registry's last position before delayed delivery.
    submit(service, digital(1, 59))
    if delayed:
        submit(service, batch(3, AT + timedelta(seconds=61)))
        assert read(service, "second")[61]["quality"] == "GOOD"
    submit(service, digital(2, 58))
    if not delayed:
        submit(service, batch(3, AT + timedelta(seconds=61)))
    assert read(service, "second")[61]["quality"] == "TIME_UNKNOWN"
    assert read(service, "second")[61]["value"] is None


@pytest.mark.parametrize("coordinate", [100.0, 101.0])
def test_monotonic_clock_must_increase_with_capture_sequence(service, coordinate):
    submit(service, batch(1, AT + timedelta(seconds=1)))
    original = batch(2, AT + timedelta(seconds=2))
    value = original.body.model_dump(mode="python")
    value["capture_time"]["monotonic_seconds"] = coordinate
    body = BatchBody.model_validate(value)
    submit(service, GatewayBatch(body=body, content_sha256=batch_digest(body)))
    assert read(service, "second")[2]["quality"] == "TIME_UNKNOWN"
    submit(service, batch(3, AT + timedelta(seconds=3)))
    assert read(service, "second")[3]["quality"] == "TIME_UNKNOWN"


@pytest.mark.parametrize("preexisting_schema14", [False, True])
@pytest.mark.parametrize("clock_kind", ["utc", "monotonic"])
def test_clock_registry_rebuild_preserves_immutable_reversal(
    service, preexisting_schema14, clock_kind
):
    def migrate(revision, downgrade=False):
        with service.engine.connect() as connection:
            action = command.downgrade if downgrade else command.upgrade
            action(migration_config(connection), revision)

    if preexisting_schema14:
        migrate("0014_mixed_alarm_runtime", downgrade=True)
    for seq, seconds in [(1, 59), (2, 58 if clock_kind == "utc" else 60), (3, 61)]:
        value = batch(seq, AT + timedelta(seconds=seconds))
        if seq == 2 and clock_kind == "monotonic":
            raw = value.body.model_dump(mode="python")
            raw["capture_time"]["monotonic_seconds"] = 100.0
            body = BatchBody.model_validate(raw)
            value = GatewayBatch(body=body, content_sha256=batch_digest(body))
        submit(service, value)
    if not preexisting_schema14:
        assert read(service, "second")[61]["quality"] == "TIME_UNKNOWN"
        migrate("0014_mixed_alarm_runtime", downgrade=True)
    migrate("head")
    submit(service, batch(4, AT + timedelta(seconds=62)))
    assert read(service, "second")[62]["quality"] == "TIME_UNKNOWN"
    assert scalar(service, "SELECT reversal_seq FROM analog_history_clock_state") == 2


def test_reversal_found_after_raw_retention_invalidates_retained_minutes(service):
    # Minute AT is projected from good captures 1 and 3, then its raw rows expire.
    submit(service, batch(1, AT + timedelta(seconds=1), 1.0))
    submit(service, batch(3, AT + timedelta(seconds=3), 3.0))
    assert read(service)[0]["quality"] == "PARTIAL"
    assert read(service)[0]["value"] == 2.0
    with service.engine.begin() as connection:
        cleanup_trends(connection, now=AT + timedelta(days=8), retention=TrendRetention())
    assert scalar(service, "SELECT count(*) FROM analog_history_raw") == 0
    # A delayed capture 2 timed after capture 3 proves the clock reversed at 3;
    # the retained minute must stop presenting capture 3 as good evidence.
    submit(service, batch(2, AT + timedelta(seconds=5), 2.0), now=AT + timedelta(days=8, minutes=1))
    point = read(service)[0]
    assert point["quality"] == "TIME_UNKNOWN"
    assert point["value"] is None and point["min"] is None and point["max"] is None
    assert point["valid_count"] == 0
