"""Canonical clock projection preserves merged report and ended-run business fixes."""

from datetime import timedelta

from apps.api.spc import OFFSETS
from apps.api.spc_http import SPCQuery, read_spc
from tests.integration.test_analytics_clock_consumers import (
    AT,
    SCOPE,
    TAG,
    measured_capture,
    setup_service,
)


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
