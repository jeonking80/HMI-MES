"""Final PLC evidence is insufficient until durable MES events match."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from apps.api.auth import Principal
from apps.api.bootstrap import ASSET_IDS, LINE_ID, bootstrap_reference_data
from apps.api.commands import (
    apply_result,
    apply_submission,
    claim_next,
    enqueue_command,
)
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.settlement import (
    SettlementScopeError,
    get_settlement,
    reconcile_execution,
    record_final_summary,
)
from contracts.models import (
    CommandRequest,
    CommandResult,
    CommandSubmission,
    Parameters,
    RunSummary,
)
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 20, 12, 1, tzinfo=UTC)
SCOPE = dict(
    authenticated_gateway_id="gateway-1",
    authenticated_source_id="simulator-1",
    authenticated_stream_id="stream-1",
)
EXAMPLE = Path(__file__).resolve().parents[2] / "contracts/examples/transport-batch.json"


@pytest.fixture
def service(migrated):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated)
    for suffix in ("1", "foreign"):
        service.configure_source(
            source_id=f"simulator-{suffix}",
            stream_id=f"stream-{suffix}",
            gateway_id=f"gateway-{suffix}",
            line_id=LINE_ID,
            capture_generation=1,
            now=AT,
        )
        service.register_execution(
            execution_id=f"run-{suffix}",
            source_id=f"simulator-{suffix}",
            stream_id=f"stream-{suffix}",
            work_order_id=f"job-{suffix}",
            recipe_version="recipe-1",
            counter_epoch="epoch-1",
            parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
            now=AT,
        )
    return service


def summary(**changes):
    body = dict(
        source_id="simulator-1",
        stream_id="stream-1",
        execution_id="run-1",
        state="ENDED",
        as_of=AT,
        ended_at=AT,
        counter_epoch="epoch-1",
        recipe_version="recipe-1",
        parameters=dict(temperature_c=150.0, speed_m_per_min=6.0),
        input_quantity=2,
        good_quantity=2,
        reject_quantity=0,
        scrap_quantity=0,
        wip_quantity=0,
        last_event_seq=2,
        final=True,
    )
    body.update(changes)
    return RunSummary.model_validate(body)


def ingest(service, *seqs, execution_id="run-1", gaps=(), **changes):
    body = json.loads(EXAMPLE.read_text())["body"]
    events = []
    for seq in seqs:
        item = json.loads(EXAMPLE.read_text())["body"]["events"][0]
        item.update(
            seq=str(seq),
            event_id=f"event-{seq}",
            execution_id=execution_id,
            equipment_id=ASSET_IDS["Outfeed"],
        )
        item["payload"]["quantity"] = "1"
        item.update(changes)
        events.append(item)
    body.update(
        batch_id="batch-" + "-".join(map(str, seqs)) + str(execution_id),
        capture_seq=str(max(seqs, default=9)),
        events=events,
        samples=[],
        gaps=list(gaps),
    )
    body = BatchBody.model_validate(body)
    return service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)), **SCOPE, now=AT
    )


def record(service, value=None, **scope):
    with service.engine.begin() as connection:
        return record_final_summary(connection, value or summary(), **(SCOPE | scope), now=AT)


def reconcile(service):
    with service.engine.begin() as connection:
        return reconcile_execution(connection, "run-1", **SCOPE, now=AT)


def scalar(service, sql):
    with service.engine.connect() as connection:
        return connection.execute(sa.text(sql)).scalar_one()


def test_pending_out_of_order_then_committed_backfill_settles(service):
    assert record(service).status == "PENDING"
    ack = ingest(service, 2)
    assert ack.durability == "COMMITTED" and ack.progress.verified_through_seq == 0
    assert reconcile(service).status == "PENDING"
    ack = ingest(service, 1)
    assert ack.progress.verified_through_seq == 2
    assert reconcile(service).status == "SETTLED"
    assert reconcile(service) == reconcile(service)
    assert service.get_totals("run-foreign")["good_quantity"] == 0
    assert (
        scalar(
            service,
            "SELECT verified_through_seq FROM stream_progress WHERE source_id='simulator-1'",
        )
        == 2
    )


def test_equal_final_later_poll_idempotent_conflict_latched_original_immutable(service):
    ingest(service, 1, 2)
    first = record(service)
    assert first.status == "SETTLED"
    assert record(service, summary(as_of=AT + timedelta(hours=1))).summary == first.summary
    conflict = summary(last_event_seq=3)
    assert record(service, conflict).status == "CONFLICT"
    assert record(service, conflict).status == "CONFLICT"
    assert record(service).status == "CONFLICT"
    assert reconcile(service).status == "CONFLICT"
    assert scalar(service, "SELECT count(*) FROM run_summary_conflicts") == 1
    with service.engine.begin() as connection:
        assert get_settlement(connection, "run-1", **SCOPE).summary == first.summary


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"input_quantity": 3, "good_quantity": 3}, "QUANTITY_MISMATCH"),
        ({"recipe_version": "other"}, "EXECUTION_CONFIGURATION_MISMATCH"),
        ({"counter_epoch": "other"}, "EXECUTION_CONFIGURATION_MISMATCH"),
        (
            {"parameters": dict(temperature_c=160.0, speed_m_per_min=6.0)},
            "EXECUTION_CONFIGURATION_MISMATCH",
        ),
    ],
)
def test_mismatches_do_not_rewrite_registered_execution_or_totals(service, changes, reason):
    ingest(service, 1, 2)
    result = record(service, summary(**changes))
    assert (result.status, result.reason_code) == ("MISMATCH", reason)
    assert service.get_totals("run-1")["good_quantity"] == 2
    assert (
        scalar(
            service, "SELECT recipe_version FROM production_executions WHERE execution_id='run-1'"
        )
        == "recipe-1"
    )


def test_nonfinal_and_foreign_scope_rejected_without_evidence(service):
    with pytest.raises(ValueError, match="actual final"):
        record(service, summary(final=False, state="ENDING", ended_at=None))
    for scope in (
        {"authenticated_gateway_id": "foreign"},
        {
            "authenticated_source_id": "simulator-foreign",
            "authenticated_stream_id": "stream-foreign",
            "authenticated_gateway_id": "gateway-foreign",
        },
    ):
        with pytest.raises(SettlementScopeError):
            record(service, **scope)
    with pytest.raises(SettlementScopeError):
        record(service, summary(execution_id="run-foreign"))
    assert scalar(service, "SELECT count(*) FROM run_reconciliations") == 0


def test_quarantined_history_cannot_settle_even_zero_counts(service):
    ack = ingest(service, 1, execution_id="unregistered")
    assert ack.events[0].disposition == "QUARANTINED"
    result = record(service, summary(input_quantity=0, good_quantity=0, last_event_seq=1))
    assert result.status == "PENDING"
    assert result.reason_code == "QUARANTINED_EVENTS"


@pytest.mark.parametrize(
    "flag,status,reason",
    [
        ("reconciliation_required", "PENDING", "SOURCE_RECONCILIATION_REQUIRED"),
        ("conflicted", "CONFLICT", "SOURCE_IDENTITY_CONFLICT"),
    ],
)
def test_source_flags_block_and_conflict_never_auto_clears(service, flag, status, reason):
    ingest(service, 1, 2)
    with service.engine.begin() as connection:
        connection.execute(
            sa.text(f"UPDATE source_streams SET {flag}=true WHERE source_id='simulator-1'")
        )
    result = record(service)
    assert (result.status, result.reason_code) == (status, reason)
    with service.engine.begin() as connection:
        connection.execute(
            sa.text(f"UPDATE source_streams SET {flag}=false WHERE source_id='simulator-1'")
        )
    assert reconcile(service).status == ("CONFLICT" if flag == "conflicted" else "SETTLED")


def test_declared_gap_blocks_even_with_equal_totals(service):
    ingest(
        service,
        2,
        gaps=(
            {
                "irrecoverable": True,
                "detected_at": AT,
                "from_seq": "1",
                "through_seq": "1",
                "reason_code": "RETENTION_EXPIRED",
                "reason": "History expired",
            },
        ),
    )
    result = record(service, summary(input_quantity=1, good_quantity=1))
    assert result.status == "PENDING"
    assert result.reason_code in {"STREAM_GAP", "SOURCE_RECONCILIATION_REQUIRED"}


def test_late_original_execution_event_revokes_previously_settled_check(service):
    ingest(service, 1, 2)
    assert record(service).status == "SETTLED"
    ingest(service, 3)
    result = reconcile(service)
    assert (result.status, result.reason_code) == ("MISMATCH", "EVENT_AFTER_FINAL_BOUNDARY")


def recovery_event(service, seq, previous_state, state, *, execution_id="run-1"):
    return ingest(
        service,
        seq,
        execution_id=execution_id,
        kind="STATE_CHANGED",
        payload={
            "previous_state": previous_state,
            "state": state,
            "control_version": str(seq),
        },
    )


@pytest.mark.parametrize("execution_id", ["run-1", None])
def test_applied_stopped_recovery_suffix_preserves_immutable_final(service, execution_id):
    ingest(service, 1, 2)
    original = record(service).summary
    recovery_event(service, 3, "STOPPED", "RECOVERY_REQUIRED", execution_id=execution_id)
    assert reconcile(service).status == "MISMATCH"
    recovery_event(service, 4, "RECOVERY_REQUIRED", "STOPPED", execution_id=execution_id)
    result = reconcile(service)
    assert (result.status, result.reason_code) == ("SETTLED", "MATCHED")
    assert result.summary == original
    assert result.summary.last_event_seq == 2
    assert service.get_totals("run-1")["good_quantity"] == 2


@pytest.mark.parametrize(
    "previous_state,state",
    [("STOPPED", "RUNNING"), ("RUNNING", "STOPPED"), ("STOPPED", "RECOVERY_REQUIRED")],
)
def test_non_recovery_or_unfinished_suffix_cannot_settle(service, previous_state, state):
    ingest(service, 1, 2)
    assert record(service).status == "SETTLED"
    recovery_event(service, 3, previous_state, state, execution_id=None)
    result = reconcile(service)
    assert (result.status, result.reason_code) == ("MISMATCH", "EVENT_AFTER_FINAL_BOUNDARY")


def test_other_execution_production_after_final_is_not_a_recovery_suffix(service):
    ingest(service, 1, 2)
    assert record(service).status == "SETTLED"
    service.register_execution(
        execution_id="run-next",
        source_id="simulator-1",
        stream_id="stream-1",
        work_order_id="job-next",
        recipe_version="recipe-1",
        counter_epoch="epoch-1",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    ingest(service, 3, execution_id="run-next")
    assert reconcile(service).status == "MISMATCH"


def test_missing_recovery_suffix_event_cannot_settle(service):
    ingest(service, 1, 2)
    assert record(service).status == "SETTLED"
    recovery_event(service, 4, "RECOVERY_REQUIRED", "STOPPED", execution_id=None)
    assert reconcile(service).status == "MISMATCH"


@pytest.mark.parametrize("change", ["payload", "scope", "quarantined"])
def test_invalid_or_quarantined_recovery_suffix_cannot_settle(service, change):
    ingest(service, 1, 2)
    assert record(service).status == "SETTLED"
    recovery_event(service, 3, "STOPPED", "RECOVERY_REQUIRED")
    recovery_event(service, 4, "RECOVERY_REQUIRED", "STOPPED")
    with service.engine.begin() as connection:
        if change == "payload":
            sql = "UPDATE event_inbox SET body=body #- '{payload,control_version}' WHERE seq=4"
        elif change == "scope":
            sql = (
                "UPDATE event_inbox SET body=jsonb_set(body,'{source_id}',"
                "'\"other-source\"'::jsonb) WHERE seq=4"
            )
        else:
            sql = (
                "UPDATE event_inbox SET applied_at=NULL,reason_code='TEST_QUARANTINE',"
                "reason='Unresolved suffix event' WHERE seq=4"
            )
        connection.execute(sa.text(sql))
    assert reconcile(service).status == "MISMATCH"


@pytest.mark.parametrize("reserved", [False, True])
@pytest.mark.parametrize("kind", ["END_RUN", "START"])
def test_unresolved_original_command_blocks_final_summary(service, reserved, kind):
    ingest(service, 1, 2)
    with service.engine.begin() as connection:
        enqueue_command(
            connection,
            CommandRequest(
                command_id="end",
                target_id="Line",
                execution_id="run-1",
                command={"kind": kind},
                requested_by="operator",
                requested_at=AT,
                expires_at=AT + timedelta(seconds=5),
                expected_control_version=1,
            ),
            principal=Principal("operator", frozenset({"operator"}), False),
            line_id=LINE_ID,
            source_id="simulator-1",
            stream_id="stream-1",
            now=AT,
        )
    if reserved:
        with service.engine.begin() as connection:
            assert claim_next(connection, **SCOPE, now=AT).action == "SUBMIT"
    result = record(service)
    assert (result.status, result.reason_code) == ("PENDING", "EXECUTION_COMMAND_UNRESOLVED")
    assert scalar(service, "SELECT status FROM command_requests") == (
        "OUTCOME_UNKNOWN" if reserved else "REQUESTED"
    )


def test_caller_rollback_discards_summary_conflict_and_status_together(service):
    ingest(service, 1, 2)
    with pytest.raises(RuntimeError):
        with service.engine.begin() as connection:
            record_final_summary(connection, summary(), **SCOPE, now=AT)
            raise RuntimeError("rollback")
    assert scalar(service, "SELECT count(*) FROM run_reconciliations") == 0
    assert record(service).status == "SETTLED"
    with pytest.raises(RuntimeError):
        with service.engine.begin() as connection:
            record_final_summary(connection, summary(last_event_seq=3), **SCOPE, now=AT)
            raise RuntimeError("rollback conflict")
    assert reconcile(service).status == "SETTLED"
    assert scalar(service, "SELECT count(*) FROM run_summary_conflicts") == 0


def test_migration_roundtrip_preserves_original_ingestion_and_commands(service):
    # Exercise the historical reconciliation-cache roundtrip before creating
    # immutable schema18 clock proof. Used schema18 downgrade is tested separately.
    with service.engine.connect() as connection:
        command.downgrade(migration_config(connection), "0017_guest_viewer")
    ingest(service, 1, 2)
    record(service)
    with service.engine.connect() as connection:
        command.downgrade(migration_config(connection), "0005_command_dispatch")
    assert scalar(service, "SELECT count(*) FROM production_events") == 2
    assert scalar(service, "SELECT count(*) FROM production_executions") == 2
    assert scalar(service, "SELECT to_regclass('run_reconciliations')") is None
    with service.engine.connect() as connection:
        command.upgrade(migration_config(connection), "head")
    assert reconcile(service).reason_code == "FINAL_SUMMARY_REQUIRED"


def test_concurrent_identical_final_and_retry_remain_one_boundary(service):
    ingest(service, 1, 2)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: record(service), range(3)))
    assert all(result.status == "SETTLED" for result in results)
    assert scalar(service, "SELECT count(*) FROM run_reconciliations") == 1
    assert scalar(service, "SELECT count(*) FROM run_summary_conflicts") == 0


def test_good_reject_and_scrap_compared_individually(service):
    ingest(service, 1, kind="PRODUCTION_REJECT")
    ingest(
        service,
        2,
        kind="WIP_SCRAP",
        payload={
            "quantity": "1",
            "counter_epoch": "epoch-1",
            "reason_code": "END_SCRAP",
            "reason": "Stopped",
        },
    )
    result = record(service, summary(good_quantity=0, reject_quantity=1, scrap_quantity=1))
    assert result.status == "SETTLED"


def test_failed_ingestion_cannot_be_finalized_by_matching_source_summary(service, monkeypatch):
    def fail(connection):
        raise RuntimeError("ingestion before commit")

    monkeypatch.setattr(service, "_before_commit", fail)
    with pytest.raises(RuntimeError):
        ingest(service, 1, 2)
    assert record(service).status == "PENDING"
    assert scalar(service, "SELECT count(*) FROM production_events") == 0
    monkeypatch.undo()
    ingest(service, 1, 2)
    assert reconcile(service).status == "SETTLED"


@pytest.mark.parametrize("kind", ["START", "SET_PARAMETERS"])
def test_terminal_original_command_collision_remains_pending(service, kind):
    ingest(service, 1, 2)
    command_body = {"kind": kind}
    if kind == "SET_PARAMETERS":
        command_body["args"] = {
            "recipe_version": "recipe-1",
            "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
        }
    with service.engine.begin() as connection:
        enqueue_command(
            connection,
            CommandRequest(
                command_id="original",
                target_id="Line",
                execution_id="run-1",
                command=command_body,
                requested_by="production",
                requested_at=AT,
                expires_at=AT + timedelta(seconds=5),
                expected_control_version=1,
            ),
            principal=Principal("production", frozenset({"operator", "production_manager"}), False),
            line_id=LINE_ID,
            source_id="simulator-1",
            stream_id="stream-1",
            now=AT,
        )
    with service.engine.begin() as connection:
        assert claim_next(connection, **SCOPE, now=AT).action == "SUBMIT"
    with service.engine.begin() as connection:
        apply_result(
            connection,
            CommandResult(
                command_id="original",
                status="SUCCEEDED",
                delivered_at=AT,
                updated_at=AT,
                completed_at=AT,
                applied_control_version=2,
                evidence={"state": "RUNNING", "execution_id": "run-1"},
            ),
            **SCOPE,
            now=AT,
        )
    assert record(service).status == "SETTLED"
    with service.engine.begin() as connection:
        apply_submission(
            connection,
            CommandSubmission(
                command_id="original",
                disposition="REJECTED",
                received_at=AT,
                reason_code="COMMAND_ID_CONFLICT",
                reason="Other canonical request owns ID",
            ),
            **SCOPE,
            now=AT,
        )
    result = reconcile(service)
    assert (result.status, result.reason_code) == ("PENDING", "EXECUTION_COMMAND_UNRESOLVED")
    assert scalar(service, "SELECT status FROM command_requests") == "SUCCEEDED"
    assert scalar(service, "SELECT collision_latched FROM command_requests") is True


def test_unrelated_execution_unknown_command_does_not_invalidate_historical_settlement(service):
    ingest(service, 1, 2)
    assert record(service).status == "SETTLED"
    service.register_execution(
        execution_id="run-next",
        source_id="simulator-1",
        stream_id="stream-1",
        work_order_id="job-next",
        recipe_version="recipe-1",
        counter_epoch="epoch-next",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    with service.engine.begin() as connection:
        enqueue_command(
            connection,
            CommandRequest(
                command_id="foreign",
                target_id="Line",
                execution_id="run-next",
                command={"kind": "START"},
                requested_by="operator",
                requested_at=AT,
                expires_at=AT + timedelta(seconds=5),
                expected_control_version=1,
            ),
            principal=Principal("operator", frozenset({"operator"}), False),
            line_id=LINE_ID,
            source_id="simulator-1",
            stream_id="stream-1",
            now=AT,
        )
    with service.engine.begin() as connection:
        assert claim_next(connection, **SCOPE, now=AT).action == "SUBMIT"
    assert scalar(service, "SELECT status FROM command_requests") == "OUTCOME_UNKNOWN"
    assert reconcile(service).status == "SETTLED"
