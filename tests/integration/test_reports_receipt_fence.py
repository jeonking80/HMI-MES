"""Unknown-clock receipt barriers and concurrent reversal-cache safety on PostgreSQL."""

from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import event

from apps.api.database import migration_config
from apps.api.reports import (
    build_timeline,
    load_report_evidence,
    read_execution_reports,
    read_execution_timeline,
)
from contracts.transport import BatchBody
from tests.integration.test_analytics_clock_consumers import (
    AT,
    SCOPE,
    capture_body,
    measured_capture,
    setup_service,
    submit,
)
from tests.integration.test_trend_history import policy


def test_unknown_clock_loss_receipt_persists_across_window_and_only_new_good_clears(migrated):
    service = setup_service(migrated, measured=True)
    measured_capture(service, 1, AT)
    body = capture_body(2, AT + timedelta(seconds=4.7)).model_dump(mode="python")
    body["samples"] = []
    body["communication"]["state"] = "ALARM"
    body["communication"]["heartbeat_ages_seconds"] = {
        key: 5.0 for key in body["communication"]["heartbeat_ages_seconds"]
    }
    body["events"] = [
        dict(
            kind="PRODUCTION_GOOD",
            event_id="unlocated-loss",
            seq=1,
            equipment_id="Outfeed",
            execution_id="run-a",
            source_id=SCOPE["source_id"],
            stream_id=SCOPE["stream_id"],
            occurred_at=AT + timedelta(seconds=1),
            payload=dict(quantity=1, counter_epoch="epoch-1"),
        )
    ]
    body["capture_time"]["monotonic_seconds"] = 1001.0
    submit(service, BatchBody.model_validate(body), now=AT + timedelta(seconds=1.2))
    with migrated.begin() as db:
        initial = read_execution_timeline(db, "run-a", **SCOPE, now=AT + timedelta(seconds=3))
        late = build_timeline(
            AT + timedelta(seconds=8),
            AT + timedelta(seconds=10),
            load_report_evidence(
                db,
                SCOPE["line_id"],
                AT + timedelta(seconds=8),
                AT + timedelta(seconds=10),
                scope=SCOPE,
                execution_id="run-a",
            ),
        )
    assert initial["durations_seconds"]["running"] == pytest.approx(1.2)
    assert initial["segments"][-1]["unknown_time_basis"] == "API_RECEIPT_FENCE"
    assert len(late) == 1 and late[0]["class"] == "UNKNOWN"
    assert late[0]["unknown_time_basis"] == "API_RECEIPT_FENCE"
    measured_capture(service, 3, AT + timedelta(seconds=9))
    with migrated.begin() as db:
        recovered = read_execution_timeline(db, "run-a", **SCOPE, now=AT + timedelta(seconds=10))
    assert recovered["durations_seconds"]["running"] == pytest.approx(2.2)
    assert recovered["segments"][-1]["class"] == "RUNNING"


def test_cached_reversal_is_overlaid_from_capture_statement_snapshot(migrated):
    service = setup_service(migrated, clock_policy=policy())
    submit(service, capture_body(1, AT), now=AT + timedelta(seconds=0.1))
    submit(service, capture_body(3, AT + timedelta(seconds=2)), now=AT + timedelta(seconds=2.1))
    injected = []

    def commit_reversal_after_scan(db, cursor, sql, params, context, many):
        if "max(mono) OVER history" in sql and not injected:
            injected.append(True)
            submit(
                service,
                capture_body(2, AT + timedelta(seconds=2.5)),
                now=AT + timedelta(seconds=2.6),
            )

    event.listen(migrated, "after_cursor_execute", commit_reversal_after_scan)
    try:
        with migrated.begin() as db:
            listing = read_execution_reports(
                db, SCOPE["line_id"], AT, AT + timedelta(minutes=1), now=AT + timedelta(seconds=3)
            )
    finally:
        event.remove(migrated, "after_cursor_execute", commit_reversal_after_scan)
    with migrated.begin() as db:
        fresh = read_execution_timeline(db, "run-a", **SCOPE, now=AT + timedelta(seconds=3))
    assert injected
    assert listing["items"][0]["durations_seconds"] == fresh["durations_seconds"]
    assert fresh["durations_seconds"]["unknown"] == 1


def test_head14_report_without_clock_registry_remains_unknown(engine):
    with engine.connect() as db:
        command.upgrade(migration_config(db), "0014_mixed_alarm_runtime")
    service = setup_service(engine)
    submit(service, capture_body(1, AT), now=AT + timedelta(seconds=0.1))
    with engine.begin() as db:
        result = read_execution_timeline(db, "run-a", **SCOPE, now=AT + timedelta(seconds=2))
    assert result["durations_seconds"] == dict(running=0, stopped=0, unknown=2, total=2)


def test_invalidated_measured_report_coordinate_remains_unknown(migrated):
    service = setup_service(migrated, measured=True)
    measured_capture(service, 1, AT)
    measured_capture(service, 3, AT + timedelta(seconds=2))
    measured_capture(service, 2, AT + timedelta(seconds=6))
    with migrated.begin() as db:
        value = read_execution_timeline(db, "run-a", **SCOPE, now=AT + timedelta(seconds=3))
    assert value["durations_seconds"] == dict(running=2, stopped=0, unknown=1, total=3)
    assert value["segments"][-1]["unknown_time_basis"] == "INVALIDATED_CANONICAL_COVERAGE"
    with migrated.begin() as db:
        late = build_timeline(
            AT + timedelta(seconds=6),
            AT + timedelta(seconds=8),
            load_report_evidence(
                db,
                SCOPE["line_id"],
                AT + timedelta(seconds=6),
                AT + timedelta(seconds=8),
                scope=SCOPE,
                execution_id="run-a",
            ),
        )
    assert len(late) == 1 and late[0]["class"] == "UNKNOWN"
    assert late[0]["unknown_time_basis"] == "INVALIDATED_CANONICAL_COVERAGE"


def test_invalidated_measured_latest_spc_slot_cannot_resurrect_old_good(migrated):
    from apps.api.spc import OFFSETS
    from apps.api.spc_http import SPCQuery, read_spc
    from tests.integration.test_analytics_clock_consumers import TAG

    service = setup_service(migrated, measured=True)
    for seq, second in enumerate(OFFSETS, start=1):
        measured_capture(service, seq, AT + timedelta(seconds=second))
    measured_capture(service, 7, AT + timedelta(seconds=48.1))
    measured_capture(service, 6, AT + timedelta(seconds=48.2))
    with migrated.begin() as db:
        value = read_spc(
            db, SPCQuery(**SCOPE, execution_id="run-a", tag=TAG), now=AT + timedelta(minutes=1)
        )
    assert value["subgroups"] == []
    assert value["excluded"]["reasons"]["TIME_UNKNOWN"] == 1
