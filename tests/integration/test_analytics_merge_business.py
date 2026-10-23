"""Canonical clock projection preserves merged report and ended-run business fixes."""

from datetime import timedelta

from sqlalchemy import event as sqlalchemy_event

from apps.api.measured_clock import issue_probe
from apps.api.reports import read_execution_reports, read_execution_timeline
from apps.api.spc import OFFSETS
from apps.api.spc_http import SPCQuery, read_spc
from contracts.clock_evidence import ClockProbeRequest, MeasuredClockEvidence
from contracts.models import Parameters
from contracts.transport import BatchBody
from tests.integration.test_analytics_clock_consumers import (
    AT,
    SCOPE,
    TAG,
    capture_body,
    measured_capture,
    setup_service,
    submit,
)


def test_measured_sampleless_failure_cuts_state_and_stream_cache_keeps_provenance(migrated):
    service = setup_service(migrated, measured=True)
    measured_capture(service, 1, AT)
    canonical_at = AT + timedelta(seconds=1)
    original_at = canonical_at + timedelta(seconds=3.7)
    body = capture_body(2, original_at).model_dump(mode="python")
    body["capture_time"]["monotonic_seconds"] = 1001.0
    body["samples"] = []
    body["communication"]["state"] = "ALARM"
    body["communication"]["heartbeat_ages_seconds"] = {
        key: 5.0 for key in body["communication"]["heartbeat_ages_seconds"]
    }
    body["events"] = [
        dict(
            kind="PRODUCTION_GOOD",
            event_id="loss-event",
            seq=1,
            equipment_id="Outfeed",
            execution_id="run-a",
            source_id=SCOPE["source_id"],
            stream_id=SCOPE["stream_id"],
            occurred_at=canonical_at,
            payload=dict(quantity=1, counter_epoch="epoch-1"),
        )
    ]
    body = BatchBody.model_validate(body)
    probe = ClockProbeRequest(
        gateway_id="gateway-1",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        capture_generation=1,
        clock_epoch="clock-1",
        t0_utc=original_at - timedelta(seconds=0.3),
        t0_monotonic_seconds=1000.7,
    )
    with migrated.begin() as db:
        response = issue_probe(
            db,
            probe,
            credential_key_id="clock-key",
            api_received_at=canonical_at - timedelta(seconds=0.2),
            now=canonical_at - timedelta(seconds=0.2),
        )
    proof = MeasuredClockEvidence(
        **probe.model_dump(),
        nonce=response.nonce,
        api_utc=response.api_utc,
        t1_utc=original_at - timedelta(seconds=0.1),
        t1_monotonic_seconds=1000.9,
    )
    submit(service, body, now=canonical_at + timedelta(seconds=0.2), evidence=proof)
    service.register_execution(
        execution_id="run-b",
        source_id=SCOPE["source_id"],
        stream_id=SCOPE["stream_id"],
        work_order_id="work-b",
        recipe_version="recipe-b",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=5.0),
        counter_epoch="epoch-2",
        now=AT,
    )
    scans = []

    def observe(db, cursor, sql, parameters, context, many):
        if "max(mono) OVER history" in sql:
            scans.append(sql)

    sqlalchemy_event.listen(migrated, "before_cursor_execute", observe)
    try:
        with migrated.begin() as db:
            listing = read_execution_reports(
                db, SCOPE["line_id"], AT, AT + timedelta(minutes=1), now=AT + timedelta(seconds=3)
            )
    finally:
        sqlalchemy_event.remove(migrated, "before_cursor_execute", observe)
    assert len(scans) == 1
    assert len(listing["items"]) == 2
    assert all(
        (item["source_id"], item["stream_id"]) == (SCOPE["source_id"], SCOPE["stream_id"])
        for item in listing["items"]
    )
    with migrated.begin() as db:
        timeline = read_execution_timeline(db, "run-a", **SCOPE, now=AT + timedelta(seconds=3))
    assert timeline["durations_seconds"] == dict(running=1, stopped=0, unknown=2, total=3)
    assert timeline["segments"][-1]["unknown_reason"] == "COMMUNICATION_LOST"
    assert timeline["segments"][-1]["from"] == canonical_at.isoformat().replace("+00:00", "Z")


def test_actual_ended_run_clamps_projected_spc_and_expired_window(migrated):
    service = setup_service(migrated, measured=True)
    ended = AT + timedelta(minutes=1)
    for minute in range(2):
        for index, offset in enumerate(OFFSETS):
            events = []
            if minute == 1 and index == 4:
                events = [
                    dict(
                        kind="STATE_CHANGED",
                        event_id="confirmed-end",
                        seq=1,
                        equipment_id="Line",
                        execution_id="run-a",
                        source_id=SCOPE["source_id"],
                        stream_id=SCOPE["stream_id"],
                        occurred_at=ended,
                        payload=dict(previous_state="STOPPING", state="STOPPED", control_version=9),
                    )
                ]
            measured_capture(
                service,
                minute * 5 + index + 1,
                AT + timedelta(minutes=minute, seconds=offset),
                value=148.0 + index,
                events=events,
            )
    query = SPCQuery(**SCOPE, execution_id="run-a", tag=TAG)
    with migrated.begin() as db:
        bounded = read_spc(db, query, now=AT + timedelta(minutes=2))
        expired = read_spc(db, query, now=AT + timedelta(days=8))
    assert len(bounded["subgroups"]) == 1
    assert bounded["subgroups"][0]["t"] == AT.isoformat().replace("+00:00", "Z")
    assert bounded["status"] == "INSUFFICIENT_DATA"
    assert expired["subgroups"] == []
    assert expired["baseline_limitation"] == "EXECUTION_OUTSIDE_RAW_RETENTION"
    assert expired["limits"] is None
