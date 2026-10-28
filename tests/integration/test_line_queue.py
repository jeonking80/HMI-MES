"""Explicit queue intent, browser protection and actual PLC dispatch on disposable PG."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from apps.api.bootstrap import LINE_ID
from apps.api.commands import apply_result, claim_next
from apps.api.internal import InternalConfig, create_internal_app
from apps.api.line_queue import QueueUpdate, advance_line_once, read_queue, update_queue
from apps.api.settlement import record_final_summary
from apps.api.work_order_workflow import BindIntent
from apps.api.work_orders import RegistrationConflict, WorkOrderDefinition, register_work_order
from apps.simulator.controller import SimulatorController
from apps.simulator.plc import PLCInputs
from apps.simulator.process import ProcessConfig
from apps.simulator.storage import SimulatorStorage
from apps.simulator.tags import project_tags
from contracts.models import CommandRequest, CommandResult, ReadEventsRequest
from contracts.transport import BatchBody, GatewayBatch, batch_digest
from tests.integration.test_internal_api import binding
from tests.integration.test_speed_adjustments import CHECKS
from tests.integration.test_work_order_workflow import (
    AT,
    ORIGIN,
    account,
    bind,
    capture,
    headers,
    intent,
    release,
    scalar,
)
from tests.integration.test_work_order_workflow import state as state

SCOPE = dict(
    authenticated_gateway_id="gateway",
    authenticated_source_id="source",
    authenticated_stream_id="stream",
)
PATH = f"/api/v1/lines/{LINE_ID}/queue"


def definition(*identities, request_id="queue-1", revision=0, enabled=True):
    return QueueUpdate(
        request_id=request_id,
        expected_revision=revision,
        source_id="source",
        stream_id="stream",
        auto_advance=enabled,
        entries=[dict(work_order_id=identity, expected_row_version=2) for identity in identities],
    )


def update(state, value):
    with state.engine.begin() as db:
        return update_queue(db, LINE_ID, value, principal=state.principal, clock=lambda: AT)


def read(state):
    with state.engine.begin() as db:
        return read_queue(db, LINE_ID)


def advance(state):
    with state.engine.begin() as db:
        return advance_line_once(db, **SCOPE, now=state.clock[0])


def test_disabled_default_never_enrolls_existing_released_work(state):
    release(state)
    release(state, "work-B", release_id="release-B")
    queue = read(state)
    assert queue["auto_advance"] is False and queue["entries"] == []
    assert advance(state) is None
    assert scalar(state, "SELECT count(*) FROM command_requests") == 0
    assert scalar(state, "SELECT count(*) FROM line_queue_entries") == 0


def test_reorder_replay_and_stale_revision_are_atomic_and_keep_entry_load_ids(state):
    release(state)
    release(state, "work-B", release_id="release-B")
    first = update(state, definition("work-A", "work-B"))
    assert update(state, definition("work-A", "work-B")) == first
    identities = {
        entry["work_order_id"]: (entry["entry_id"], entry["load_command_id"])
        for entry in first["entries"]
    }
    reordered = update(
        state,
        definition("work-B", "work-A", request_id="queue-2", revision=int(first["revision"])),
    )
    assert [entry["work_order_id"] for entry in reordered["entries"]] == ["work-B", "work-A"]
    for entry in reordered["entries"]:
        assert identities[entry["work_order_id"]] == (entry["entry_id"], entry["load_command_id"])
    with pytest.raises(RegistrationConflict):
        update(state, definition("work-A", request_id="stale"))
    assert read(state) == reordered
    assert scalar(state, "SELECT count(*) FROM line_queue_updates") == 2
    assert scalar(state, "SELECT count(*) FROM command_requests") == 0


def test_audit_failure_rolls_back_policy_entries_and_idempotent_receipt(state):
    release(state)

    def fail(_db, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO line_queue_audit"):
            raise RuntimeError("queue audit interrupted")

    event.listen(state.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="queue audit interrupted"):
            update(state, definition("work-A"))
    finally:
        event.remove(state.engine, "before_cursor_execute", fail)
    assert read(state)["entries"] == []
    assert scalar(state, "SELECT count(*) FROM line_queue_updates") == 0
    assert scalar(state, "SELECT count(*) FROM line_queue_entries") == 0


@pytest.mark.parametrize("role", ["viewer", "operator", "equipment_admin", "production_manager"])
async def test_browser_queue_reads_and_production_manager_only_mutation(state, role):
    release(state)
    session = state.session if role == "production_manager" else account(state.auth, role, role)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("hmi_session", session.token)
        assert (await client.get(PATH)).status_code == 200
        result = await client.put(
            PATH,
            json=definition("work-A").model_dump(mode="json"),
            headers={"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token},
        )
        assert result.status_code == (200 if role == "production_manager" else 403), result.text


async def test_browser_requires_login_and_csrf_for_queue_mutation(state):
    release(state)
    body = definition("work-A").model_dump(mode="json")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=ORIGIN
    ) as client:
        assert (await client.get(PATH)).status_code == 401
        assert (await client.put(PATH, json=body, headers=headers(state))).status_code == 401
        client.cookies.set("hmi_session", state.session.token)
        for protection in ({}, {"Origin": ORIGIN}, {"X-CSRF-Token": "invalid", "Origin": ORIGIN}):
            assert (await client.put(PATH, json=body, headers=protection)).status_code == 403
    assert scalar(state, "SELECT count(*) FROM line_queue_updates") == 0


def safe_capture(state, controller, *, active, **changes):
    # Use the exact projection published by the simulator /snapshot and OPC UA
    # adapters. In particular ENDED executions report Sealer.Ready=False.
    process = controller.storage.load_execution()
    values = {
        **project_tags(
            controller.state,
            process,
            counter_epoch=controller.storage.identity.counter_epoch,
            heartbeat=process.tick if process else 0,
        ),
        "Line.ActiveExecutionId": active,
        **changes,
    }
    capture(state, values=values)


def ingest_events(state, controller):
    events = controller.read_events(
        ReadEventsRequest(source_id="source", stream_id="stream", after_seq=0, limit=1000)
    ).events
    state.capture_seq += 1
    body = BatchBody(
        schema_version="1.1",
        batch_id=f"events-{state.capture_seq}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=state.capture_seq,
        captured_at=AT,
        events=events,
    )
    return state.service.ingest(
        GatewayBatch(body=body, content_sha256=batch_digest(body)), **SCOPE, now=AT
    )


def execute_load(state, controller, receipt):
    request = CommandRequest.model_validate(receipt["load_request"])
    with state.engine.begin() as db:
        work = claim_next(db, **SCOPE, now=AT)
        assert work.action == "SUBMIT"
    submission = controller.submit(request, now=AT, authenticated_gateway_principal="gateway")
    assert submission.submission.disposition == "ACCEPTED"
    controller.tick(now=AT)
    result = controller.get_result(request.command_id)
    assert result.status == "SUCCEEDED"
    with state.engine.begin() as db:
        apply_result(db, result, **SCOPE, now=AT)


def finish_run(state, controller, execution):
    for kind in ("START", "END_RUN"):
        request = CommandRequest(
            command_id=f"{execution}-{kind}",
            target_id="Line",
            execution_id=execution,
            requested_by="manager",
            requested_at=AT,
            expires_at=AT + timedelta(seconds=5),
            expected_control_version=controller.state.control_version,
            command=dict(kind=kind),
        )
        assert controller.submit(request, now=AT).submission.disposition == "ACCEPTED"
        controller.tick(now=AT)
    for _ in range(100):
        summary = controller.get_summary(execution)
        if summary.final:
            break
        controller.tick(now=AT)
    assert summary.final and summary.wip_quantity == 0
    ingest_events(state, controller)
    return summary


async def test_actual_claim_driver_waits_for_exact_settlement_then_loads_two_queued_jobs(
    state, tmp_path
):
    with state.engine.begin() as db:
        register_work_order(
            db,
            WorkOrderDefinition(
                id="work-C",
                item_id="item",
                line_id=LINE_ID,
                recipe_version_id="recipe-v1",
                target_quantity=4,
                planned_date="2026-10-24",
            ),
            actor="manager",
            now=AT,
        )
    for suffix in "ABC":
        release(state, f"work-{suffix}", release_id=f"release-{suffix}")
    update(state, definition("work-B", "work-C"))
    credential, token = binding(
        tmp_path,
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        expires_at=AT + timedelta(hours=1),
    )
    app = create_internal_app(state.service, [credential], config=InternalConfig(clock=lambda: AT))
    with SimulatorStorage(
        tmp_path / "queue.sqlite3", source_id="source", stream_id="stream", counter_epoch="epoch"
    ) as store:
        controller = SimulatorController(
            store,
            now=AT,
            process_config=ProcessConfig(stage_lengths_mm=(1, 1, 1), ambient_temperature_c=150.0),
        )
        controller.recover(now=AT, operator_id="manager", checks=CHECKS)
        controller.set_inputs(
            PLCInputs(temperature_c=150.0),
            now=AT,
            control_mode="REMOTE",
            operation_mode="AUTO",
        )
        capture(state, values={"Line.ControlVersion": controller.state.control_version})
        first = bind(
            state,
            definition=BindIntent(
                **{
                    **intent().model_dump(),
                    "expected_control_version": controller.state.control_version,
                }
            ),
        )
        execute_load(state, controller, first)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            for previous in (first,):
                final = finish_run(state, controller, previous["execution_id"])
                safe_capture(state, controller, active=previous["execution_id"])
                assert advance(state) is None  # Final evidence without summary remains PENDING.
                with state.engine.begin() as db:
                    assert record_final_summary(db, final, **SCOPE, now=AT).status == "SETTLED"
            response = await client.post(
                "/internal/v1/commands/claim",
                json={},
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200, response.text
            work = response.json()["work"]
            assert work is not None and work["action"] == "SUBMIT"
            second = next(entry for entry in read(state)["entries"] if entry["state"] == "BOUND")
            request = CommandRequest.model_validate(second["binding_receipt"]["load_request"])
            assert request.command_id == second["load_command_id"]
            submission = controller.submit(
                request, now=AT, authenticated_gateway_principal="gateway"
            )
            assert submission.submission.disposition == "ACCEPTED"
            controller.tick(now=AT)
            with state.engine.begin() as db:
                result = controller.get_result(request.command_id)
                assert result.status == "SUCCEEDED"
                apply_result(db, result, **SCOPE, now=AT)
            assert advance(state) is None
            final = finish_run(state, controller, request.execution_id)
            safe_capture(state, controller, active=request.execution_id)
            assert advance(state) is None
            with state.engine.begin() as db:
                assert record_final_summary(db, final, **SCOPE, now=AT).status == "SETTLED"
            third = advance(state)
            assert third is not None
            execute_load(state, controller, third)
            assert scalar(state, "SELECT count(*) FROM command_requests") == 3


@pytest.fixture
def completed(state, tmp_path):
    release(state)
    release(state, "work-B", release_id="release-B")
    update(state, definition("work-B"))
    with SimulatorStorage(
        tmp_path / "completed.sqlite3",
        source_id="source",
        stream_id="stream",
        counter_epoch="epoch",
    ) as store:
        controller = SimulatorController(
            store,
            now=AT,
            process_config=ProcessConfig(stage_lengths_mm=(1, 1, 1), ambient_temperature_c=150.0),
        )
        controller.recover(now=AT, operator_id="manager", checks=CHECKS)
        controller.set_inputs(
            PLCInputs(temperature_c=150.0),
            now=AT,
            control_mode="REMOTE",
            operation_mode="AUTO",
        )
        capture(state, values={"Line.ControlVersion": controller.state.control_version})
        first = bind(
            state,
            definition=BindIntent(
                **{
                    **intent().model_dump(),
                    "expected_control_version": controller.state.control_version,
                }
            ),
        )
        execute_load(state, controller, first)
        final = finish_run(state, controller, first["execution_id"])
        with state.engine.begin() as db:
            assert record_final_summary(db, final, **SCOPE, now=AT).status == "SETTLED"
        safe_capture(state, controller, active=first["execution_id"])
        yield state, controller, first


def test_actual_ended_snapshot_loads_next_order_without_sealer_ready(completed):
    state, controller, first = completed
    process = controller.storage.load_execution()
    values = project_tags(
        controller.state,
        process,
        counter_epoch=controller.storage.identity.counter_epoch,
        heartbeat=process.tick,
    )
    assert controller.state.run_state == "ENDED"
    assert values["Line.State"] == 0
    assert values["Line.WipCount"] == 0
    assert values["Sealer.Ready"] is False
    assert values["Line.ActiveExecutionId"] == first["execution_id"]
    capture(state, values=values)
    queued = read(state)["entries"][0]
    receipt = advance(state)
    assert receipt is not None
    assert receipt["command_id"] == queued["load_command_id"]
    assert receipt["load_request"]["command"]["args"]["start_when_ready"] is False
    execute_load(state, controller, receipt)
    assert advance(state) is None
    assert read(state)["entries"][0]["binding_receipt"] == receipt
    assert read(state)["blocked_reason"] is None
    assert scalar(state, "SELECT count(*) FROM command_requests") == 2


@pytest.mark.parametrize(
    "damage",
    [
        "disabled-user",
        "lost-permission",
        "generation",
        "stale",
        "local",
        "manual",
        "running",
        "wip",
        "wrong-active",
        "unsafe",
    ],
)
def test_current_identity_source_and_physical_evidence_block_automatic_bind(completed, damage):
    state, controller, first = completed
    if damage in {"disabled-user", "lost-permission", "generation"}:
        statements = {
            "disabled-user": "UPDATE users SET active=false WHERE login='manager'",
            "lost-permission": "DELETE FROM user_roles WHERE login='manager'",
            "generation": "UPDATE source_streams SET capture_generation=2",
        }
        with state.engine.begin() as db:
            db.execute(text(statements[damage]))
    elif damage == "stale":
        state.clock[0] += timedelta(seconds=30)
    else:
        changes = {
            "local": {"Line.ControlMode": 0},
            "manual": {"Line.OperationMode": 0},
            "running": {"Line.State": 2},
            "wip": {"Line.WipCount": 1},
            "wrong-active": {"Line.ActiveExecutionId": "unrelated"},
            "unsafe": {"Sealer.GuardClosed": False},
        }[damage]
        active = changes.pop("Line.ActiveExecutionId", first["execution_id"])
        safe_capture(state, controller, active=active, **changes)
    assert advance(state) is None
    current = read(state)
    assert current["blocked_reason"] is not None
    if damage not in {"disabled-user", "lost-permission", "generation", "wrong-active"}:
        assert current["auto_advance"] is True
    assert scalar(state, "SELECT count(*) FROM command_requests") == 1
    assert scalar(state, "SELECT count(*) FROM work_order_execution_links") == 1


def test_recoverable_load_condition_is_visible_then_clears_on_same_queue_policy(completed):
    state, controller, first = completed
    original = read(state)
    safe_capture(state, controller, active=first["execution_id"], **{"Line.WipCount": 1})
    assert advance(state) is None
    blocked = read(state)
    assert blocked["blocked_reason"] == "LOAD_CONDITION_NOT_MET:Line.WipCount"
    assert blocked["auto_advance"] is True
    assert blocked["revision"] == original["revision"]
    assert blocked["entries"] == original["entries"]
    safe_capture(state, controller, active=first["execution_id"])
    receipt = advance(state)
    assert receipt["command_id"] == original["entries"][0]["load_command_id"]
    current = read(state)
    assert current["blocked_reason"] is None
    assert current["revision"] == original["revision"]


@pytest.mark.parametrize("invalid_sample", ["bad", "null"])
def test_sealer_readiness_still_requires_known_good_evidence(completed, invalid_sample):
    state, controller, _first = completed
    process = controller.storage.load_execution()
    values = project_tags(
        controller.state,
        process,
        counter_epoch=controller.storage.identity.counter_epoch,
        heartbeat=process.tick,
    )
    capture(state, values=values, **{invalid_sample: "Sealer.Ready"})
    assert advance(state) is None
    assert read(state)["blocked_reason"] == "FRESH_SOURCE_REQUIRED"
    assert read(state)["auto_advance"] is True
    assert scalar(state, "SELECT count(*) FROM command_requests") == 1


@pytest.mark.parametrize("status", ["OUTCOME_UNKNOWN", "FAILED"])
def test_unknown_or_failed_load_keeps_original_command_id_and_never_reissues(completed, status):
    state, _controller, _first = completed
    queued = read(state)["entries"][0]
    second = advance(state)
    assert second is not None
    with state.engine.begin() as db:
        work = claim_next(db, **SCOPE, now=AT)
        assert work.action == "SUBMIT"
        assert work.request.command_id == queued["load_command_id"]
    if status == "FAILED":
        result = CommandResult(
            command_id=queued["load_command_id"],
            status="FAILED",
            delivered_at=AT,
            updated_at=AT,
            completed_at=AT,
            reason_code="PLC_LOAD_FAILED",
            reason="PLC reported failure for the original queued load",
        )
        with state.engine.begin() as db:
            assert apply_result(db, result, **SCOPE, now=AT).status == "FAILED"
    with state.engine.connect() as db:
        recorded = db.execute(
            text("SELECT status FROM command_requests WHERE command_id=:id"),
            {"id": queued["load_command_id"]},
        ).scalar_one()
        assert recorded == status
    for _ in range(3):
        assert advance(state) is None
    current = read(state)["entries"][0]
    assert current["load_command_id"] == queued["load_command_id"]
    assert current["binding_receipt"]["load_request"]["command_id"] == queued["load_command_id"]
    assert scalar(state, "SELECT count(*) FROM command_requests") == 2


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE line_queue_entries SET load_command_id='replacement'",
        "UPDATE line_queue_entries SET expected_row_version=99",
        "UPDATE line_queue_entries SET binding_receipt='{}'::jsonb WHERE state='BOUND'",
        "UPDATE line_queue_entries SET state='WAITING' WHERE state='BOUND'",
        "DELETE FROM line_queue_entries",
        "TRUNCATE line_queue_entries",
    ],
)
def test_database_preserves_entry_identity_and_bound_receipt(completed, statement):
    state, _controller, _first = completed
    assert advance(state) is not None
    before = read(state)
    with pytest.raises(DBAPIError), state.engine.begin() as db:
        db.execute(text(statement))
    assert read(state) == before


def test_edited_order_reenrolls_with_its_new_version_and_keeps_old_evidence(state):
    from apps.api.work_order_edit import EditIntent, edit_work_order
    from tests.integration.test_work_order_edit import BODY

    release(state)
    first = update(state, definition("work-A"))
    removed = update(state, definition(request_id="queue-2", revision=int(first["revision"])))
    assert removed["entries"] == []
    with state.engine.begin() as db:
        edit_work_order(
            db,
            "work-A",
            EditIntent.model_validate(BODY),
            principal=state.principal,
            clock=lambda: AT,
        )
    assert scalar(state, "SELECT row_version FROM work_orders WHERE id='work-A'") == 3
    again = QueueUpdate(
        request_id="queue-3",
        expected_revision=int(removed["revision"]),
        source_id="source",
        stream_id="stream",
        auto_advance=True,
        entries=[dict(work_order_id="work-A", expected_row_version=3)],
    )
    queued = update(state, again)
    [entry] = queued["entries"]
    assert entry["expected_row_version"] == "3" and entry["state"] == "WAITING"
    assert entry["entry_id"] != first["entries"][0]["entry_id"]
    assert entry["load_command_id"] != first["entries"][0]["load_command_id"]
    assert (
        scalar(
            state,
            "SELECT count(*) FROM line_queue_entries WHERE work_order_id='work-A' "
            "AND state='REMOVED' AND expected_row_version=2",
        )
        == 1
    )
    # Removing and re-adding at the same version reuses that entry and its LOAD id.
    out = update(state, definition(request_id="queue-4", revision=int(queued["revision"])))
    back = update(
        state,
        again.model_copy(
            update={"request_id": "queue-5", "expected_revision": int(out["revision"])}
        ),
    )
    assert back["entries"][0]["entry_id"] == entry["entry_id"]


def test_queued_order_cannot_be_cancelled_until_it_leaves_the_queue(state):
    from apps.api.work_order_cancel import CancellationIntent, cancel_work_order

    release(state)
    queued = update(state, definition("work-A"))

    def cancel():
        with state.engine.begin() as db:
            return cancel_work_order(
                db,
                "work-A",
                CancellationIntent(cancellation_id="cancel-A", expected_row_version=2),
                principal=state.principal,
                clock=lambda: AT,
            )

    with pytest.raises(RegistrationConflict):
        cancel()
    assert scalar(state, "SELECT status FROM work_orders WHERE id='work-A'") == "RELEASED"
    update(state, definition(request_id="queue-2", revision=int(queued["revision"])))
    assert cancel()["status"] == "CANCELLED"


def test_finished_work_order_loads_again_as_a_new_run_and_is_queued_again(completed):
    state, controller, first = completed
    update(state, definition(request_id="queue-empty", revision=1))
    capture(
        state,
        values=project_tags(
            controller.state,
            controller.storage.load_execution(),
            counter_epoch=controller.storage.identity.counter_epoch,
            heartbeat=controller.storage.load_execution().tick,
        ),
    )
    again = bind(
        state,
        definition=BindIntent(
            **{
                **intent().model_dump(),
                "command_id": "load-A-again",
                "expected_work_order_version": int(
                    scalar(state, "SELECT row_version FROM work_orders WHERE id='work-A'")
                ),
                "expected_control_version": controller.state.control_version,
            }
        ),
    )
    assert again["execution_id"] != first["execution_id"]
    assert (
        scalar(
            state, "SELECT count(*) FROM work_order_execution_links WHERE work_order_id='work-A'"
        )
        == 2
    )
    # The first run keeps its own settled result; the new one is still running.
    assert (
        scalar(
            state,
            "SELECT status FROM run_reconciliations WHERE execution_id='"
            + first["execution_id"]
            + "'",
        )
        == "SETTLED"
    )
    with pytest.raises(RegistrationConflict):
        bind(
            state,
            definition=BindIntent(
                **{
                    **intent().model_dump(),
                    "command_id": "load-A-twice",
                    "expected_work_order_version": int(
                        scalar(state, "SELECT row_version FROM work_orders WHERE id='work-A'")
                    ),
                    "expected_control_version": controller.state.control_version,
                }
            ),
        )
    with pytest.raises(RegistrationConflict):
        update(state, definition("work-A", request_id="queue-running", revision=2))


def test_queue_bound_order_is_queued_again_after_its_run_settles(completed):
    state, controller, first = completed
    process = controller.storage.load_execution()
    capture(
        state,
        values=project_tags(
            controller.state,
            process,
            counter_epoch=controller.storage.identity.counter_epoch,
            heartbeat=process.tick,
        ),
    )
    receipt = advance(state)
    execute_load(state, controller, receipt)
    bound = read(state)["entries"][0]
    assert bound["state"] == "BOUND" and bound["work_order_id"] == "work-B"
    final = finish_run(state, controller, receipt["execution_id"])
    with state.engine.begin() as db:
        assert record_final_summary(db, final, **SCOPE, now=AT).status == "SETTLED"
    revision = int(read(state)["revision"])
    version = int(scalar(state, "SELECT row_version FROM work_orders WHERE id='work-B'"))
    again = definition("work-B", request_id="queue-again", revision=revision)
    queued = update(
        state,
        QueueUpdate(
            **{
                **again.model_dump(),
                "entries": [{"work_order_id": "work-B", "expected_row_version": version}],
            }
        ),
    )
    waiting = [entry for entry in queued["entries"] if entry["state"] == "WAITING"]
    assert len(waiting) == 1 and waiting[0]["work_order_id"] == "work-B"
    assert waiting[0]["entry_id"] != bound["entry_id"]
    assert waiting[0]["load_command_id"] != bound["load_command_id"]
