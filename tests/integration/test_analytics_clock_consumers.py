"""Real retained analytical proof consumers; PLC event times stay original."""

from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import text

from apps.api.bootstrap import bootstrap_reference_data
from apps.api.capture_clock import SameHostClockPolicy
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.measured_clock import issue_probe
from apps.api.reports import read_daily_report
from apps.api.spc import OFFSETS
from apps.api.spc_http import SPCQuery, read_spc
from apps.api.trend_history import TrendRetention, cleanup_trends, read_trends
from contracts.clock_evidence import ClockProbeRequest, MeasuredClockEvidence
from contracts.models import EVENT_ADAPTER, Parameters
from contracts.transport import GatewayBatch, TagSample, batch_digest
from tests.integration.test_trend_history import AT, SCOPE, TAG, batch, policy

GATEWAY = dict(
    authenticated_gateway_id="gateway-1",
    authenticated_source_id=SCOPE["source_id"],
    authenticated_stream_id=SCOPE["stream_id"],
)


def setup_service(engine, *, measured=False, clock_policy=None):
    bootstrap_reference_data(engine)
    service = IngestionService(
        engine, trend_clock_policy=clock_policy, measured_clock_enabled=measured
    )
    service.configure_source(**SCOPE, gateway_id="gateway-1", capture_generation=1, now=AT)
    service.register_execution(
        execution_id="run-a",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        work_order_id="work-a",
        recipe_version="recipe-a",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=5.0),
        counter_epoch="epoch-1",
        now=AT,
    )
    return service


def capture_body(seq, at, *, identity="run-a", value=150.0, events=()):
    original = batch(seq, at, value).body
    extras = tuple(
        TagSample(
            sample_id=f"{tag}-{seq}",
            tag_id=tag,
            source_timestamp=at,
            server_timestamp=None,
            acquired_at=at,
            status_code=0,
            reading=dict(data_type=kind, value=value),
        )
        for tag, kind, value in (
            ("Line.ActiveExecutionId", "String", identity),
            ("Line.State", "Int32", 2),
        )
    )
    return original.model_copy(
        update={
            "samples": original.samples + extras,
            "events": tuple(EVENT_ADAPTER.validate_python(event) for event in events),
        }
    )


def submit(service, body, *, now, evidence=None):
    envelope = GatewayBatch(body=body, content_sha256=batch_digest(body), clock_evidence=evidence)
    return service.ingest(envelope, **GATEWAY, authenticated_key_id="clock-key", now=now)


def measured_capture(service, seq, at, *, identity="run-a", value=150.0, events=()):
    # Explicit asymmetric-host fixture: gateway UTC leads API UTC by 3.7s,
    # 200ms measured RTT; acquisition follows gateway receipt by 100ms.
    original_at = at + timedelta(seconds=3.7)
    body = capture_body(seq, original_at, identity=identity, value=value, events=events)
    capture_time = body.capture_time.model_copy(
        update={
            "monotonic_seconds": 1000.0 + (at - AT).total_seconds(),
        }
    )
    body = body.model_copy(update={"capture_time": capture_time})
    mono = body.capture_time.monotonic_seconds
    request = ClockProbeRequest(
        gateway_id="gateway-1",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        capture_generation=1,
        clock_epoch="clock-1",
        t0_utc=original_at - timedelta(seconds=0.3),
        t0_monotonic_seconds=mono - 0.3,
    )
    with service.engine.begin() as db:
        response = issue_probe(
            db,
            request,
            credential_key_id="clock-key",
            api_received_at=at - timedelta(seconds=0.2),
            now=at - timedelta(seconds=0.2),
        )
    evidence = MeasuredClockEvidence(
        **request.model_dump(),
        nonce=response.nonce,
        api_utc=response.api_utc,
        t1_utc=original_at - timedelta(seconds=0.1),
        t1_monotonic_seconds=mono - 0.1,
    )
    submit(service, body, now=at + timedelta(seconds=0.2), evidence=evidence)
    return body


def test_same_host_trends_keep_good_durable_proof_after_raw_cleanup(migrated):
    configured = SameHostClockPolicy(
        gateway_id="gateway-1",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        capture_generation=1,
        valid_from_api_utc=AT - timedelta(days=1),
        valid_until_api_utc=AT + timedelta(days=1),
    )
    service = setup_service(migrated, clock_policy=configured)
    submit(service, capture_body(1, AT), now=AT + timedelta(seconds=0.1))
    with migrated.begin() as db:
        trends = read_trends(
            db,
            scope=SCOPE,
            tags=[TAG],
            start=AT,
            end=AT + timedelta(seconds=1),
            resolution="second",
            now=AT + timedelta(seconds=2),
        )
        assert trends["series"][0]["points"][0]["quality"] == "GOOD"
        assert db.execute(text("SELECT count(*) FROM alarm_capture_decisions")).scalar_one() == 0
        assert db.execute(text("SELECT status FROM capture_clock_evidence")).scalar_one() == "GOOD"
        db.execute(text("UPDATE analog_history_raw SET aggregated=true"))
        cleanup_trends(db, now=AT + timedelta(hours=7), retention=TrendRetention(raw_days=0.25))
        assert db.execute(text("SELECT count(*) FROM analog_history_raw")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM capture_clock_evidence")).scalar_one() == 1


def test_measured_gateway_skew_never_moves_plc_production_dates(migrated):
    service = setup_service(migrated, measured=True)
    midnight = AT.replace(hour=15)
    event = dict(
        kind="PRODUCTION_GOOD",
        event_id="plc-event",
        seq=1,
        equipment_id="Outfeed",
        execution_id="run-a",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        occurred_at=midnight + timedelta(seconds=1),
        payload=dict(quantity=7, counter_epoch="epoch-1"),
    )
    body = measured_capture(service, 1, midnight - timedelta(seconds=1), events=[event])
    with migrated.begin() as db:
        before = read_daily_report(
            db, SCOPE["line_id"], AT.date(), now=midnight + timedelta(seconds=5)
        )
        after = read_daily_report(
            db, SCOPE["line_id"], AT.date() + timedelta(days=1), now=midnight + timedelta(seconds=5)
        )
        original = db.execute(text("SELECT body FROM ingest_batches")).scalar_one()
    assert before["totals"]["good"] == "0" and after["totals"]["good"] == "7"
    assert original == body.model_dump(mode="json")


def test_projected_spc_first20_prefix_and_original_identity_simultaneity(migrated):
    service = setup_service(migrated, measured=True)
    for minute in range(40):
        for offset_index, offset in enumerate(OFFSETS):
            measured_capture(
                service,
                minute * 5 + offset_index + 1,
                AT + timedelta(minutes=minute, seconds=offset),
                value=148.0 + offset_index,
            )
    query = SPCQuery(**SCOPE, execution_id="run-a", tag=TAG)
    now = AT + timedelta(minutes=40)
    with migrated.begin() as db:
        original = read_spc(db, query, now=now)
        assert original["status"] == "OK"
        assert original["baseline"]["from"] == AT.isoformat().replace("+00:00", "Z")
        assert len(original["subgroups"]) == 40
        assert original["limits"]["xbar"]["ucl"] == pytest.approx(152.308)
        raw = (
            db.execute(
                text(
                    "SELECT acquired_at,canonical_utc,observation FROM analog_history_raw "
                    "ORDER BY capture_seq LIMIT 1"
                )
            )
            .mappings()
            .one()
        )
        assert raw["acquired_at"] == AT + timedelta(seconds=3.7)
        assert raw["canonical_utc"] == AT
        assert raw["observation"]["sample"]["acquired_at"] == (
            AT + timedelta(seconds=3.7)
        ).isoformat().replace("+00:00", "Z")
        db.execute(
            text("DELETE FROM analog_history_raw WHERE canonical_utc<:cutoff"),
            {"cutoff": AT + timedelta(minutes=15)},
        )
        retained = read_spc(db, query, now=now)
        assert len(retained["subgroups"]) == 25
        assert retained["status"] == "INSUFFICIENT_DATA"
        assert retained["baseline_limitation"] == "ORIGINAL_PREFIX_RAW_UNAVAILABLE"
        assert retained["baseline"] is None and retained["limits"] is None


def test_projected_spc_foreign_execution_is_never_attributed(migrated):
    service = setup_service(migrated, measured=True)
    # Original-coordinate simultaneity is required before projection. The 40
    # complete positive groups above prove original timestamps still match after
    # a +3.7s shift; a simultaneous different execution must remain excluded.
    for index, offset in enumerate(OFFSETS):
        at = AT + timedelta(seconds=offset)
        measured_capture(service, index + 1, at, identity="different-run", value=148.0 + index)
    with migrated.begin() as db:
        result = read_spc(
            db, SPCQuery(**SCOPE, execution_id="run-a", tag=TAG), now=AT + timedelta(minutes=1)
        )
    assert result["subgroups"] == []
    assert result["excluded"]["reasons"]["MISSING"] == 1


def test_schema17_legacy_raw_spc_fallback(engine):
    with engine.connect() as db:
        command.upgrade(migration_config(db), "0017_guest_viewer")
    service = setup_service(engine, clock_policy=policy())
    for index, offset in enumerate(OFFSETS):
        at = AT + timedelta(seconds=offset)
        submit(
            service,
            capture_body(index + 1, at, value=148.0 + index),
            now=at + timedelta(seconds=0.1),
        )
    with engine.begin() as db:
        result = read_spc(
            db, SPCQuery(**SCOPE, execution_id="run-a", tag=TAG), now=AT + timedelta(minutes=1)
        )
    assert len(result["subgroups"]) == 1


def test_explicit_unknown_never_borrows_good_legacy_alarm_assessment(migrated):
    from tests.integration.test_alarm_consumer import AT as alarm_at
    from tests.integration.test_alarm_consumer import capture, ingest
    from tests.integration.test_alarm_consumer import state as alarm_fixture

    # The old alarm path has an explicit trusted reference, while analytical
    # clock policy is deliberately absent. Its immutable UNKNOWN must win.
    state = alarm_fixture.__wrapped__(migrated)
    ingest(state, capture(1, seconds=0))
    with migrated.begin() as db:
        assert (
            db.execute(text("SELECT status FROM capture_clock_evidence")).scalar_one() == "UNKNOWN"
        )
        assert (
            db.execute(
                text("""SELECT count(*) FROM alarm_capture_decisions
          WHERE context->'clock_assessment' IS NOT NULL
            AND context->'clock_assessment'<>'null'::jsonb""")
            ).scalar_one()
            > 0
        )
        daily = read_daily_report(
            db, SCOPE["line_id"], alarm_at.date(), now=alarm_at + timedelta(seconds=2)
        )
    assert daily["durations_seconds"]["running"] == 0
