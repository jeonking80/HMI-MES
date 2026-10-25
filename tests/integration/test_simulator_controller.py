"""Real command/PLC/process/source/final-boundary integration on one SQLite file."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.simulator.controller import SimulatorController
from apps.simulator.plc import PLCInputs, RecoveryChecks
from apps.simulator.process import ProcessConfig
from apps.simulator.storage import (
    FaultEventInput,
    RecoveryRequired,
    SimulatorStorage,
    StateEventInput,
    StorageCorruption,
)
from contracts.models import (
    CommandRequest,
    LineState,
    ProductionEvent,
    ReadEventsRequest,
    ScrapEvent,
    StateEvent,
)

AT = datetime(2026, 10, 20, 12, tzinfo=UTC)
FAST = ProcessConfig(stage_lengths_mm=(1, 1, 1), ambient_temperature_c=150.0)
CHECKS = RecoveryChecks(
    outputs_stopped=True,
    positions_verified=True,
    storage_reconciled=True,
    commands_reconciled=True,
    operator_confirmed=True,
)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runtime(tmp_path):
    with SimulatorStorage(tmp_path / "controller.sqlite3") as store:
        controller = SimulatorController(store, now=AT, process_config=FAST)
        controller.recover(now=AT, operator_id="operator", checks=CHECKS)
        controller.set_inputs(
            PLCInputs(temperature_c=150.0),
            now=AT,
            control_mode="REMOTE",
            operation_mode="AUTO",
        )
        yield controller


def request(controller, kind, command_id=None, *, execution_id="run-A", **args):
    return CommandRequest(
        command_id=command_id or f"{kind}-{controller.state.control_version}",
        target_id="Line",
        execution_id=execution_id,
        requested_by="operator",
        requested_at=AT,
        expires_at=AT + timedelta(seconds=5),
        expected_control_version=controller.state.control_version,
        command={"kind": kind, "args": args},
    )


def load(controller, *, target=100):
    command = request(
        controller,
        "LOAD_JOB",
        "load",
        work_order_id="work-A",
        recipe_version="recipe-1",
        target_quantity=target,
        auto_end_at_target=False,
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
    )
    assert controller.submit(command, now=AT).submission.disposition == "ACCEPTED"
    assert controller.get_result("load").status == "ACCEPTED"
    assert controller.storage.load_execution() is None
    controller.tick(now=AT)
    assert controller.get_result("load").status == "SUCCEEDED"
    assert controller.state.state == LineState.READY
    return command


def action(controller, kind, command_id=None, **args):
    command = request(controller, kind, command_id, **args)
    outcome = controller.submit(command, now=AT)
    assert outcome.submission.disposition == "ACCEPTED", outcome
    controller.tick(now=AT)
    return command


def events(controller):
    identity = controller.storage.identity
    return controller.read_events(
        ReadEventsRequest(
            source_id=identity.source_id,
            stream_id=identity.stream_id,
            after_seq=0,
            limit=1000,
        )
    ).events


def test_full_load_start_pause_resume_end_fixes_real_quantities(runtime):
    controller = runtime
    load(controller, target=8)
    assert controller.get_summary("run-A").state == "LOADED"
    action(controller, "START", "start")
    for _ in range(4):
        controller.tick(now=AT)
    before = controller.storage.load_execution()
    assert before.input_quantity > 0 and before.wip_quantity > 0
    action(controller, "PAUSE", "pause")
    assert controller.state.state == LineState.PAUSED
    assert controller.get_result("pause").evidence.state == LineState.PAUSED
    for _ in range(3):
        controller.tick(now=AT)
    paused = controller.storage.load_execution()
    assert paused.slots == before.slots
    assert paused.input_quantity == before.input_quantity
    action(controller, "START", "resume")
    end = action(controller, "END_RUN", "end")
    for _ in range(10):
        if controller.get_result(end.command_id).status == "SUCCEEDED":
            break
        controller.tick(now=AT)
    summary = controller.get_summary("run-A")
    assert summary.final and summary.state == "ENDED" and summary.wip_quantity == 0
    assert summary.input_quantity == summary.good_quantity + summary.reject_quantity
    assert controller.state.state == LineState.STOPPED
    source = events(controller)
    assert [event.seq for event in source] == list(range(1, len(source) + 1))
    production = [event for event in source if isinstance(event, ProductionEvent)]
    assert len(production) == summary.good_quantity + summary.reject_quantity
    assert all(event.execution_id == "run-A" for event in production)
    assert isinstance(source[-1], StateEvent) and source[-1].payload.state == LineState.STOPPED
    assert summary.last_event_seq == source[-1].seq
    assert controller.get_result("end").evidence.last_event_seq == summary.last_event_seq
    for _ in range(2):
        controller.tick(now=AT + timedelta(seconds=1))
    assert controller.get_summary("run-A") == summary
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with controller.storage.transaction():
            controller.storage.connection.execute(
                "UPDATE runtime_runs SET summary_json=summary_json WHERE execution_id='run-A'"
            )


def test_good_quantity_target_replaces_deterministic_reject_and_explicit_scrap(runtime):
    load(runtime, target=4)
    action(runtime, "START", "start-target")
    runtime.tick(now=AT)
    action(runtime, "PAUSE", "pause-target")
    paused = runtime.storage.load_execution()
    assert paused.input_quantity == paused.wip_quantity == 2
    item_id = min(item.item_id for item in paused.slots if item is not None)
    runtime.scrap_wip((item_id,), reason="Verified damaged item", positions_verified=True, now=AT)
    action(runtime, "START", "resume-target")
    for _ in range(100):
        process = runtime.storage.load_execution()
        assert process.input_quantity == (
            process.good_quantity
            + process.reject_quantity
            + process.scrap_quantity
            + process.wip_quantity
        )
        assert process.good_quantity + process.wip_quantity <= 4
        if process.good_quantity == 4 and process.wip_quantity == 0:
            break
        runtime.tick(now=AT)
    else:
        pytest.fail("Good target was not reached after replacing known losses")
    assert (
        process.input_quantity,
        process.good_quantity,
        process.reject_quantity,
        process.scrap_quantity,
    ) == (6, 4, 1, 1)
    assert runtime.state.run_state == "RUNNING"
    assert runtime.state.state == LineState.RUNNING
    assert not runtime.get_summary("run-A").final
    for _ in range(5):
        runtime.tick(now=AT)
    stable = runtime.storage.load_execution()
    assert stable.input_quantity == 6 and stable.good_quantity == 4 and stable.wip_quantity == 0
    source = events(runtime)
    assert (
        sum(
            event.payload.quantity
            for event in source
            if isinstance(event, ProductionEvent) and event.kind == "PRODUCTION_GOOD"
        )
        == 4
    )
    assert (
        sum(
            event.payload.quantity
            for event in source
            if isinstance(event, ProductionEvent) and event.kind == "PRODUCTION_REJECT"
        )
        == 1
    )
    assert sum(event.payload.quantity for event in source if isinstance(event, ScrapEvent)) == 1
    action(runtime, "END_RUN", "end-target")
    final = runtime.get_summary("run-A")
    assert final.final and final.good_quantity == 4
    assert (
        final.input_quantity == final.good_quantity + final.reject_quantity + final.scrap_quantity
    )


def test_manual_end_stops_replacement_even_when_drain_rejects_below_good_target(runtime):
    load(runtime, target=5)
    action(runtime, "START", "start-early-end")
    runtime.tick(now=AT)
    runtime.tick(now=AT)
    before = runtime.storage.load_execution()
    assert before.input_quantity == before.wip_quantity == 3
    ending = action(runtime, "END_RUN", "early-end")
    for _ in range(20):
        process = runtime.storage.load_execution()
        assert process.input_quantity == before.input_quantity
        if runtime.get_result(ending.command_id).status == "SUCCEEDED":
            break
        runtime.tick(now=AT)
    else:
        pytest.fail("Explicit END_RUN did not finish draining")
    final = runtime.get_summary("run-A")
    assert final.final and final.wip_quantity == 0
    assert (final.input_quantity, final.good_quantity, final.reject_quantity) == (3, 2, 1)
    assert final.good_quantity < runtime.state.target_quantity


def test_duplicate_precedes_expiry_version_and_id_conflict_never_replaces(runtime):
    controller = runtime
    original = load(controller)
    result = controller.get_result("load")
    version = controller.state.control_version
    head = controller.state.last_event_seq
    duplicate = controller.submit(original, now=AT + timedelta(days=1))
    assert not duplicate.created and duplicate.result == result
    changed = CommandRequest.model_validate(
        {
            **original.model_dump(),
            "expected_control_version": version,
        }
    )
    conflict = controller.submit(changed, now=AT + timedelta(days=1))
    assert conflict.submission.reason_code == "COMMAND_ID_CONFLICT"
    assert conflict.result is None
    assert controller.get_result("load") == result
    assert controller.state.control_version == version and controller.state.last_event_seq == head
    first = request(controller, "START", "first")
    assert controller.submit(first, now=AT).created
    assert not controller.submit(first, now=AT).created
    second = controller.submit(request(controller, "START", "second"), now=AT)
    assert second.submission.reason_code == "COMMAND_CONFLICT"
    controller.tick(now=AT)
    assert controller.storage.load_execution().input_quantity == 1


def test_settings_change_preserves_wip_and_keeps_applied_intervals(runtime):
    controller = runtime
    load(controller)
    action(controller, "START", "start")
    controller.tick(now=AT)
    action(controller, "PAUSE", "pause")
    before = controller.storage.load_execution()
    action(
        controller,
        "SET_PARAMETERS",
        "settings",
        recipe_version="recipe-2",
        parameters={"temperature_c": 150.0, "speed_m_per_min": 12.0},
    )
    after = controller.storage.load_execution()
    assert after.slots == before.slots and after.input_quantity == before.input_quantity
    assert after.config.speed_m_per_min == 12.0
    assert controller.get_summary("run-A").recipe_version == "recipe-2"
    assert [
        row[0]
        for row in controller.storage.connection.execute(
            "SELECT recipe_version FROM runtime_settings ORDER BY effective_tick"
        )
    ] == ["recipe-1", "recipe-2"]
    retry_version = request(
        controller,
        "SET_PARAMETERS",
        "reuse",
        recipe_version="recipe-1",
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
    )
    assert (
        controller.submit(retry_version, now=AT).submission.reason_code == "RECIPE_VERSION_REUSED"
    )


def test_fault_during_end_waits_for_explicit_scrap_and_keeps_fault(runtime):
    controller = runtime
    load(controller)
    action(controller, "START", "start")
    controller.tick(now=AT)
    controller.tick(now=AT)
    action(controller, "END_RUN", "end")
    assert controller.get_result("end").status == "ACCEPTED"
    controller.set_inputs(PLCInputs(temperature_c=150.0, fault_code=42), now=AT)
    controller.tick(now=AT)
    ending = controller.get_summary("run-A")
    assert ending.state == "ENDING" and ending.wip_quantity > 0 and not ending.final
    assert ending.scrap_quantity == 0
    assert controller.state.state == LineState.FAULT
    items = tuple(item.item_id for item in controller.storage.load_execution().slots if item)
    controller.scrap_wip(
        items, reason="Stopped faulted positions verified", now=AT, positions_verified=True
    )
    assert controller.get_result("end").status == "ACCEPTED"
    controller.tick(now=AT)
    final = controller.get_summary("run-A")
    assert final.final and final.scrap_quantity == len(items)
    assert controller.get_result("end").status == "SUCCEEDED"
    assert controller.state.state == LineState.FAULT and controller.state.latched_fault_code == 42
    assert len([event for event in events(controller) if isinstance(event, ScrapEvent)]) == len(
        items
    )


def test_restart_preserves_wip_known_results_and_requires_explicit_separate_start(tmp_path):
    path = tmp_path / "restart.sqlite3"
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT, process_config=FAST)
        controller.recover(now=AT, operator_id="operator", checks=CHECKS)
        controller.set_inputs(
            PLCInputs(temperature_c=150.0), now=AT, control_mode="REMOTE", operation_mode="AUTO"
        )
        load(controller)
        action(controller, "START", "start")
        before = store.load_execution()
        original = controller.get_result("start")
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT + timedelta(seconds=2), process_config=FAST)
        assert controller.state.recovery_required and not controller.state.run_output
        assert store.load_execution() == before
        assert controller.get_result("start") == original
        with pytest.raises(RecoveryRequired):
            controller.tick(now=AT + timedelta(seconds=2))
        controller.set_inputs(PLCInputs(temperature_c=150.0), now=AT + timedelta(seconds=2))
        controller.recover(now=AT + timedelta(seconds=2), operator_id="operator", checks=CHECKS)
        assert controller.state.state == LineState.STOPPED
        controller.tick(now=AT + timedelta(seconds=2))
        assert not controller.state.run_output and store.load_execution().slots == before.slots


def test_pending_restart_never_replays_and_recovery_requires_original_id_resolution(tmp_path):
    path = tmp_path / "pending.sqlite3"
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT, process_config=FAST)
        controller.recover(now=AT, operator_id="operator", checks=CHECKS)
        controller.set_inputs(
            PLCInputs(temperature_c=150.0), now=AT, control_mode="REMOTE", operation_mode="AUTO"
        )
        load(controller)
        pending = request(controller, "START", "pending")
        controller.submit(pending, now=AT)
        before = store.load_execution()
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT + timedelta(seconds=8), process_config=FAST)
        assert controller.get_result("pending").status == "OUTCOME_UNKNOWN"
        duplicate = controller.submit(pending, now=AT + timedelta(seconds=8))
        assert not duplicate.created
        with pytest.raises(RecoveryRequired, match="original pending"):
            controller.recover(now=AT + timedelta(seconds=8), operator_id="operator", checks=CHECKS)
        assert store.recovery_required
        controller.reconcile_interrupted("pending", now=AT + timedelta(seconds=8), checks=CHECKS)
        assert controller.get_result("pending").status == "FAILED"
        controller.recover(now=AT + timedelta(seconds=8), operator_id="operator", checks=CHECKS)
        controller.tick(now=AT + timedelta(seconds=8))
        assert store.load_execution().input_quantity == before.input_quantity
        assert not controller.state.run_output


@pytest.mark.parametrize("seam", ["_before_boundary_commit", "_before_tick_commit", "after_commit"])
def test_process_crash_at_final_boundary_commits_all_components_or_none(tmp_path, seam):
    path = tmp_path / "crash.sqlite3"
    child = r"""
import os, sys
from datetime import datetime, UTC, timedelta
from apps.simulator.storage import SimulatorStorage
from apps.simulator.controller import SimulatorController
from apps.simulator.plc import PLCInputs, RecoveryChecks
from apps.simulator.process import ProcessConfig
from contracts.models import CommandRequest
at = datetime(2026,10,20,12,tzinfo=UTC)
s = SimulatorStorage(sys.argv[1])
c = SimulatorController(s,now=at,process_config=ProcessConfig(
    stage_lengths_mm=(1,1,1),ambient_temperature_c=150.0))
c.recover(now=at,operator_id='operator',checks=RecoveryChecks(
    outputs_stopped=True,positions_verified=True,storage_reconciled=True,
    commands_reconciled=True,operator_confirmed=True))
c.set_inputs(PLCInputs(temperature_c=150.0),now=at,control_mode='REMOTE',operation_mode='AUTO')
def submit(kind, ident, args={}):
    c.submit(CommandRequest(command_id=ident,target_id='Line',execution_id='run-A',
        requested_by='operator',requested_at=at,expires_at=at+timedelta(seconds=5),
        expected_control_version=c.state.control_version,
        command={'kind':kind,'args':args}),now=at)
submit('LOAD_JOB','load',dict(work_order_id='work',recipe_version='r1',target_quantity=1,
    auto_end_at_target=False,
    parameters=dict(temperature_c=150.0,speed_m_per_min=6.0)))
c.tick(now=at)
submit('START','start')
c.tick(now=at)
c.tick(now=at)
c.tick(now=at)
submit('END_RUN','end')
if sys.argv[2] == 'after_commit':
    c.tick(now=at)
    os._exit(73)
setattr(c, sys.argv[2], lambda: os._exit(73))
c.tick(now=at)
os._exit(99)
"""
    completed = subprocess.run(
        [sys.executable, "-c", child, str(path), seam],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        timeout=20,
        check=False,
    )
    assert completed.returncode == 73
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT + timedelta(seconds=1), process_config=FAST)
        summary = controller.get_summary("run-A")
        bodies = [row[0] for row in store.connection.execute("SELECT body FROM source_events")]
        production = [body for body in bodies if '"kind":"PRODUCTION_' in body]
        if seam == "after_commit":
            assert summary.final and summary.wip_quantity == 0 and summary.good_quantity == 1
            assert controller.get_result("end").status == "SUCCEEDED"
            assert len(production) == 1
            controller.recover(now=AT + timedelta(seconds=1), operator_id="operator", checks=CHECKS)
            controller.tick(now=AT + timedelta(seconds=1))
            assert controller.get_summary("run-A") == summary
        else:
            assert not summary.final and summary.wip_quantity == summary.input_quantity == 1
            assert summary.good_quantity == summary.reject_quantity == 0
            assert controller.commands.lookup("end").result.status == "ACCEPTED"
            assert controller.get_result("end").status == "OUTCOME_UNKNOWN"
            assert not production


def test_boundary_failure_rolls_back_all_components_and_allows_same_pending_tick(runtime):
    controller = runtime
    load(controller, target=1)
    action(controller, "START", "start")
    controller.tick(now=AT)
    controller.tick(now=AT)
    end = request(controller, "END_RUN", "end")
    controller.submit(end, now=AT)
    before_state = controller.state
    before_process = controller.storage.load_execution()
    before_summary = controller.get_summary("run-A")
    before_events = events(controller)

    def failure():
        raise RuntimeError("Power loss before combined commit")

    controller._before_boundary_commit = failure
    with pytest.raises(RuntimeError, match="Power loss"):
        controller.tick(now=AT)
    assert controller.state == before_state
    assert controller.storage.load_execution() == before_process
    assert controller.get_summary("run-A") == before_summary
    assert events(controller) == before_events
    assert controller.get_result("end").status == "ACCEPTED"
    controller._before_boundary_commit = lambda: None
    controller.tick(now=AT)
    assert controller.get_result("end").status == "SUCCEEDED"
    assert controller.get_summary("run-A").final


def test_single_commit_pause_uses_one_control_version_for_all_state_events_and_result(runtime):
    controller = runtime
    load(controller)
    action(controller, "START", "start")
    before = controller.state
    action(controller, "PAUSE", "pause")
    assert controller.state.control_version == before.control_version + 1
    changed = [
        event
        for event in events(controller)
        if isinstance(event, StateEvent) and event.seq > before.last_event_seq
    ]
    assert [event.payload.state for event in changed] == [LineState.STOPPING, LineState.PAUSED]
    assert {event.payload.control_version for event in changed} == {before.control_version + 1}
    assert controller.get_result("pause").applied_control_version == before.control_version + 1


def test_nested_console_mutations_share_one_outer_transaction_control_version(runtime):
    controller = runtime
    before = controller.state
    with controller.storage.transaction():
        controller.set_inputs(PLCInputs(temperature_c=150.0), now=AT, control_mode="LOCAL")
        controller.set_inputs(PLCInputs(temperature_c=150.0), now=AT, operation_mode="MANUAL")
    assert controller.state.control_version == before.control_version + 1


def test_second_job_requires_explicit_local_settlement_and_preserves_first_final(runtime):
    controller = runtime
    load(controller, target=1)
    action(controller, "START", "start")
    controller.tick(now=AT)
    controller.tick(now=AT)
    action(controller, "END_RUN", "end")
    first = controller.get_summary("run-A")
    assert first.final

    def next_job(command_id):
        return request(
            controller,
            "LOAD_JOB",
            command_id,
            execution_id="run-B",
            work_order_id="work-B",
            recipe_version="recipe-B",
            target_quantity=3,
            parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
        )

    assert controller.submit(next_job("blocked"), now=AT).submission.reason_code == "LOAD_BLOCKED"
    controller.set_inputs(PLCInputs(temperature_c=150.0), now=AT, control_mode="LOCAL")
    controller.acknowledge_settlement("run-A", now=AT, operator_id="operator")
    controller.set_inputs(PLCInputs(temperature_c=150.0), now=AT, control_mode="REMOTE")
    assert controller.submit(next_job("next"), now=AT).submission.disposition == "ACCEPTED"
    controller.tick(now=AT)
    assert controller.get_summary("run-A") == first
    assert controller.get_summary("run-B").input_quantity == 0
    assert controller.get_summary("run-B").state == "LOADED"


def _ended_first_remote_job(controller):
    load(controller, target=1)
    action(controller, "START", "start")
    controller.tick(now=AT)
    controller.tick(now=AT)
    action(controller, "END_RUN", "end")
    summary = controller.get_summary("run-A")
    assert summary.final and summary.wip_quantity == 0
    assert controller.state.state == LineState.STOPPED
    assert not controller.state.previous_execution_settled
    return summary


def _remote_next_job(controller, command_id="remote-next"):
    return request(
        controller,
        "LOAD_JOB",
        command_id,
        execution_id="run-B",
        work_order_id="work-B",
        recipe_version="recipe-B",
        target_quantity=1,
        auto_end_at_target=False,
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
    )


def _remote_attestations(controller):
    return [
        json.loads(row[0])
        for row in controller.storage.connection.execute(
            "SELECT details_json FROM runtime_audit WHERE cause='REMOTE_SETTLEMENT_ATTESTATION' "
            "ORDER BY audit_id"
        )
    ]


def _append_remote_state_tail(controller, *transitions):
    tail = controller.storage.append_control_events(
        tuple(
            StateEventInput(
                occurred_at=AT,
                execution_id="run-A",
                payload={
                    "previous_state": previous,
                    "state": state,
                    "control_version": controller.state.control_version,
                },
            )
            for previous, state in transitions
        )
    )
    controller._save(controller.state, AT)
    return tail


def test_remote_next_job_after_actual_final_restart_and_recovery(tmp_path):
    path = tmp_path / "remote-final-restart.sqlite3"
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT, process_config=FAST)
        controller.recover(now=AT, operator_id="operator", checks=CHECKS)
        controller.set_inputs(
            PLCInputs(temperature_c=150.0), now=AT, control_mode="REMOTE", operation_mode="AUTO"
        )
        first = _ended_first_remote_job(controller)
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT, process_config=FAST)
        assert controller.state.recovery_required
        controller.set_inputs(PLCInputs(temperature_c=150.0), now=AT)
        controller.recover(now=AT, operator_id="operator", checks=CHECKS)
        tail = [event for event in events(controller) if event.seq > first.last_event_seq]
        assert len(tail) >= 2
        assert all(isinstance(event, StateEvent) for event in tail)
        assert all(
            event.payload.previous_state in {LineState.STOPPED, LineState.RECOVERY_REQUIRED}
            and event.payload.state in {LineState.STOPPED, LineState.RECOVERY_REQUIRED}
            for event in tail
        )
        assert tail[-1].payload.state == LineState.STOPPED
        candidate = _remote_next_job(controller)
        response = controller.submit(
            candidate, now=AT, authenticated_gateway_principal="pinned-gateway"
        )
        assert response.submission.disposition == "ACCEPTED"
        audit = _remote_attestations(controller)[0]
        assert audit["tail_end_seq"] == str(tail[-1].seq)
        assert (
            audit["tail_sha256"]
            == hashlib.sha256(
                json.dumps(
                    [event.model_dump(mode="json") for event in tail],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
        )
        controller.tick(now=AT)
        assert controller.get_result(candidate.command_id).status == "SUCCEEDED"
        assert controller.state.execution_id == "run-B"
        assert controller.get_summary("run-A") == first


@pytest.mark.parametrize(
    "condition",
    [
        "running_previous",
        "running_state",
        "ends_recovery",
        "fault",
        "production",
        "gap",
        "source",
        "stream",
    ],
)
def test_remote_next_job_rejects_entire_unsafe_post_final_tail(runtime, condition):
    _ended_first_remote_job(runtime)
    _append_remote_state_tail(
        runtime,
        (LineState.STOPPED, LineState.RECOVERY_REQUIRED),
        (LineState.RECOVERY_REQUIRED, LineState.STOPPED),
    )
    db = runtime.storage.connection
    first, last = db.execute(
        "SELECT seq,body FROM source_events ORDER BY seq DESC LIMIT 2"
    ).fetchall()[::-1]
    body = json.loads(first["body"])
    if condition == "running_previous":
        body["payload"]["previous_state"] = "RUNNING"
    elif condition == "running_state":
        body["payload"]["state"] = "RUNNING"
    elif condition == "ends_recovery":
        first = last
        body = json.loads(last["body"])
        body["payload"]["state"] = "RECOVERY_REQUIRED"
    elif condition == "fault":
        runtime.storage.append_control_events(
            (
                FaultEventInput(
                    occurred_at=AT,
                    execution_id="run-A",
                    payload={"fault_code": 42, "active": True},
                ),
            )
        )
        runtime._save(runtime.state, AT)
    elif condition == "production":
        body["kind"] = "PRODUCTION_GOOD"
        body["equipment_id"] = "Outfeed"
        body["payload"] = {"quantity": "1", "counter_epoch": runtime.storage.identity.counter_epoch}
        db.execute(
            "UPDATE source_events SET body=?,item_id=? WHERE seq=?",
            (json.dumps(body), "00000000000000001000", first["seq"]),
        )
    elif condition == "gap":
        db.execute("DELETE FROM source_events WHERE seq=?", (first["seq"],))
    elif condition == "source":
        body["source_id"] = "other-source"
    elif condition == "stream":
        body["stream_id"] = "other-stream"
    if condition not in {"fault", "gap", "production"}:
        db.execute("UPDATE source_events SET body=? WHERE seq=?", (json.dumps(body), first["seq"]))
    response = runtime.submit(
        _remote_next_job(runtime), now=AT, authenticated_gateway_principal="pinned-gateway"
    )
    assert response.submission.disposition == "REJECTED"
    assert response.submission.reason_code == "REMOTE_SETTLEMENT_BLOCKED"
    assert not runtime.state.previous_execution_settled
    assert _remote_attestations(runtime) == []
    assert runtime.storage.load_execution("run-B") is None


@pytest.mark.parametrize("change", ["head", "hash", "old_audit"])
def test_remote_admission_rechecks_pinned_post_final_tail_before_execution(runtime, change):
    first = _ended_first_remote_job(runtime)
    tail = _append_remote_state_tail(runtime, (LineState.STOPPED, LineState.STOPPED))
    candidate = _remote_next_job(runtime)
    admitted = runtime.submit(candidate, now=AT, authenticated_gateway_principal="pinned-gateway")
    assert admitted.submission.disposition == "ACCEPTED"
    db = runtime.storage.connection
    if change == "head":
        _append_remote_state_tail(runtime, (LineState.STOPPED, LineState.STOPPED))
    elif change == "hash":
        body = tail[0].model_dump(mode="json")
        body["payload"]["control_version"] = str(int(body["payload"]["control_version"]) + 1)
        db.execute(
            "UPDATE source_events SET body=? WHERE event_id=?",
            (json.dumps(body), tail[0].event_id),
        )
    else:
        details = _remote_attestations(runtime)[0]
        details.pop("tail_end_seq")
        details.pop("tail_sha256")
        db.execute(
            "UPDATE runtime_audit SET details_json=? WHERE cause='REMOTE_SETTLEMENT_ATTESTATION'",
            (json.dumps(details),),
        )
    runtime.tick(now=AT)
    result = runtime.get_result(candidate.command_id)
    assert result.status == "FAILED"
    assert result.reason_code == "REMOTE_SETTLEMENT_CHANGED"
    assert not runtime.state.previous_execution_settled
    assert runtime.storage.load_execution("run-B") is None
    assert runtime.get_summary("run-A") == first


def test_authenticated_remote_second_job_attests_exact_final_and_completes_two_jobs(runtime):
    first = _ended_first_remote_job(runtime)
    candidate = _remote_next_job(runtime)
    admitted = runtime.submit(candidate, now=AT, authenticated_gateway_principal="pinned-gateway")
    assert admitted.submission.disposition == "ACCEPTED"
    assert admitted.result.status == "ACCEPTED"
    assert runtime.state.previous_execution_settled
    assert runtime.state.execution_id == "run-A"
    assert runtime.storage.load_execution("run-B") is None
    canonical = first.model_dump(mode="json")
    canonical.pop("as_of")
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert _remote_attestations(runtime) == [
        {
            "command_id": candidate.command_id,
            "authenticated_gateway_principal": "pinned-gateway",
            "asserted_requested_by": "operator",
            "previous_source_id": first.source_id,
            "previous_stream_id": first.stream_id,
            "previous_execution_id": "run-A",
            "previous_counter_epoch": first.counter_epoch,
            "canonical_final_sha256": digest,
            "expected_control_version": str(candidate.expected_control_version),
            "tail_end_seq": str(first.last_event_seq),
            "tail_sha256": hashlib.sha256(b"[]").hexdigest(),
        }
    ]
    runtime.tick(now=AT)
    assert runtime.get_result(candidate.command_id).status == "SUCCEEDED"
    assert runtime.state.execution_id == "run-B"
    assert not runtime.state.previous_execution_settled
    action(runtime, "START", "start-B", execution_id="run-B")
    runtime.tick(now=AT)
    runtime.tick(now=AT)
    action(runtime, "END_RUN", "end-B", execution_id="run-B")
    assert runtime.get_summary("run-B").final
    assert runtime.get_summary("run-A") == first
    assert {
        event.execution_id for event in events(runtime) if isinstance(event, ProductionEvent)
    } == {
        "run-A",
        "run-B",
    }


@pytest.mark.parametrize(
    "condition", ["version", "parameters", "feedback", "local", "untrusted", "recovery"]
)
def test_rejected_remote_next_job_does_not_attest_or_mutate_settlement_flag(runtime, condition):
    _ended_first_remote_job(runtime)
    if condition == "feedback":
        runtime.set_inputs(PLCInputs(temperature_c=150.0, run_feedback=True), now=AT)
    elif condition == "local":
        runtime.set_inputs(PLCInputs(temperature_c=150.0), now=AT, control_mode="LOCAL")
    elif condition == "recovery":
        runtime.storage.mark_corrupt()
    candidate = _remote_next_job(runtime)
    if condition == "version":
        candidate = candidate.model_copy(
            update={"expected_control_version": candidate.expected_control_version - 1}
        )
    elif condition == "parameters":
        data = candidate.model_dump()
        data["command"]["args"]["parameters"]["temperature_c"] = 1000.0
        candidate = CommandRequest.model_validate(data)
    before = runtime.state
    admitted = runtime.submit(
        candidate,
        now=AT,
        authenticated_gateway_principal=None if condition == "untrusted" else "pinned-gateway",
    )
    assert admitted.submission.disposition == "REJECTED"
    assert runtime.state == before
    assert not runtime.state.previous_execution_settled
    assert _remote_attestations(runtime) == []
    assert runtime.storage.load_execution("run-B") is None
    if condition == "recovery":
        assert runtime.storage.recovery_required


def test_remote_attestation_only_runs_after_first_seen_dedup_and_expiry(runtime):
    _ended_first_remote_job(runtime)
    expired = _remote_next_job(runtime, "expired-next")
    response = runtime.submit(
        expired, now=AT + timedelta(seconds=5), authenticated_gateway_principal="pinned-gateway"
    )
    assert response.submission.reason_code == "REQUEST_EXPIRED"
    assert not runtime.state.previous_execution_settled
    assert not _remote_attestations(runtime)
    candidate = _remote_next_job(runtime)
    first = runtime.submit(candidate, now=AT, authenticated_gateway_principal="pinned-gateway")
    replay = runtime.submit(
        candidate,
        now=AT + timedelta(days=1),
        authenticated_gateway_principal="different-pinned-principal",
    )
    assert not replay.created and replay.submission == first.submission
    changed = candidate.model_dump()
    changed["command"]["args"]["target_quantity"] = 2
    collision = runtime.submit(
        CommandRequest.model_validate(changed),
        now=AT,
        authenticated_gateway_principal="pinned-gateway",
    )
    assert collision.submission.reason_code == "COMMAND_ID_CONFLICT"
    assert collision.result is None
    assert len(_remote_attestations(runtime)) == 1


def test_remote_attestation_flag_audit_and_accepted_journal_share_outer_transaction(runtime):
    _ended_first_remote_job(runtime)
    candidate = _remote_next_job(runtime)
    before = runtime.state
    with pytest.raises(RuntimeError, match="rollback"):
        with runtime.storage.transaction():
            admitted = runtime.submit(
                candidate, now=AT, authenticated_gateway_principal="pinned-gateway"
            )
            assert admitted.submission.disposition == "ACCEPTED"
            assert runtime.state.previous_execution_settled
            assert len(_remote_attestations(runtime)) == 1
            raise RuntimeError("rollback outer transaction after provisional admission")
    assert runtime.state == before
    assert runtime.commands.lookup(candidate.command_id) is None
    assert _remote_attestations(runtime) == []
    assert runtime.submit(
        candidate, now=AT, authenticated_gateway_principal="pinned-gateway"
    ).created


def test_remote_attestation_preserves_execution_time_physical_recheck(runtime):
    first = _ended_first_remote_job(runtime)
    candidate = _remote_next_job(runtime)
    assert (
        runtime.submit(
            candidate, now=AT, authenticated_gateway_principal="pinned-gateway"
        ).submission.disposition
        == "ACCEPTED"
    )
    runtime.set_inputs(PLCInputs(temperature_c=150.0), now=AT, control_mode="LOCAL")
    runtime.tick(now=AT)
    assert runtime.get_result(candidate.command_id).status == "FAILED"
    assert runtime.get_result(candidate.command_id).reason_code == "LOCAL_MODE"
    assert runtime.storage.load_execution("run-B") is None
    assert runtime.get_summary("run-A") == first


def test_authenticated_remote_load_cannot_attest_unfinished_execution(runtime):
    load(runtime)
    before = runtime.state
    response = runtime.submit(
        _remote_next_job(runtime), now=AT, authenticated_gateway_principal="pinned-gateway"
    )
    assert response.submission.disposition == "REJECTED"
    assert runtime.state == before
    assert _remote_attestations(runtime) == []


def test_pending_remote_load_does_not_repeat_attestation_for_another_id(runtime):
    _ended_first_remote_job(runtime)
    admitted = runtime.submit(
        _remote_next_job(runtime), now=AT, authenticated_gateway_principal="pinned-gateway"
    )
    assert admitted.submission.disposition == "ACCEPTED"
    before = runtime.state
    response = runtime.submit(
        _remote_next_job(runtime, "conflicting-next"),
        now=AT,
        authenticated_gateway_principal="pinned-gateway",
    )
    assert response.submission.reason_code == "COMMAND_CONFLICT"
    assert runtime.state == before
    assert len(_remote_attestations(runtime)) == 1


def test_remote_attestation_rechecks_final_boundary_after_transient_estop(runtime):
    first = _ended_first_remote_job(runtime)
    candidate = _remote_next_job(runtime)
    assert (
        runtime.submit(
            candidate, now=AT, authenticated_gateway_principal="pinned-gateway"
        ).submission.disposition
        == "ACCEPTED"
    )
    runtime.set_inputs(PLCInputs(temperature_c=150.0, estop=True), now=AT)
    runtime.set_inputs(PLCInputs(temperature_c=150.0, estop=False), now=AT)
    assert runtime.state.state == LineState.STOPPED
    assert runtime._head() > first.last_event_seq
    runtime.tick(now=AT)
    result = runtime.get_result(candidate.command_id)
    assert result.status == "FAILED"
    assert result.reason_code == "REMOTE_SETTLEMENT_CHANGED"
    assert runtime.storage.load_execution("run-B") is None
    assert runtime.state.execution_id == "run-A"
    assert runtime.get_summary("run-A") == first
    assert not runtime.state.previous_execution_settled
    invalidated = runtime.storage.connection.execute(
        "SELECT details_json FROM runtime_audit WHERE cause='REMOTE_SETTLEMENT_INVALIDATED'"
    ).fetchone()
    assert json.loads(invalidated[0])["command_id"] == candidate.command_id
    runtime.set_inputs(PLCInputs(temperature_c=150.0), now=AT, control_mode="LOCAL")
    local = runtime.submit(_remote_next_job(runtime, "local-blocked"), now=AT, authority="LOCAL")
    assert local.submission.reason_code == "LOAD_BLOCKED"
    assert runtime.storage.load_execution("run-B") is None
    runtime.acknowledge_settlement("run-A", now=AT, operator_id="local-operator")
    assert (
        runtime.submit(
            _remote_next_job(runtime, "local-after-explicit-ack"), now=AT, authority="LOCAL"
        ).submission.disposition
        == "ACCEPTED"
    )


def test_remote_invalidation_failure_and_audit_roll_back_with_tick(runtime, monkeypatch):
    _ended_first_remote_job(runtime)
    candidate = _remote_next_job(runtime)
    runtime.submit(candidate, now=AT, authenticated_gateway_principal="pinned-gateway")
    runtime.set_inputs(PLCInputs(temperature_c=150.0, estop=True), now=AT)
    runtime.set_inputs(PLCInputs(temperature_c=150.0, estop=False), now=AT)
    before = runtime.state

    def fail():
        raise RuntimeError("tick rollback after attestation invalidation")

    monkeypatch.setattr(runtime, "_before_tick_commit", fail)
    with pytest.raises(RuntimeError, match="rollback"):
        runtime.tick(now=AT)
    assert runtime.state == before
    assert runtime.state.previous_execution_settled
    assert runtime.get_result(candidate.command_id).status == "ACCEPTED"
    assert (
        runtime.storage.connection.execute(
            "SELECT COUNT(*) FROM runtime_audit WHERE cause='REMOTE_SETTLEMENT_INVALIDATED'"
        ).fetchone()[0]
        == 0
    )


def test_remote_physical_rejection_also_discards_stale_settlement_flag(runtime):
    _ended_first_remote_job(runtime)
    candidate = _remote_next_job(runtime)
    runtime.submit(candidate, now=AT, authenticated_gateway_principal="pinned-gateway")
    runtime.set_inputs(PLCInputs(temperature_c=150.0, estop=True), now=AT)
    runtime.tick(now=AT)
    assert runtime.get_result(candidate.command_id).status == "FAILED"
    assert not runtime.state.previous_execution_settled
    runtime.set_inputs(PLCInputs(temperature_c=150.0, estop=False), now=AT, control_mode="LOCAL")
    local = runtime.submit(
        _remote_next_job(runtime, "local-after-physical-failure"), now=AT, authority="LOCAL"
    )
    assert local.submission.reason_code == "LOAD_BLOCKED"
    assert runtime.storage.load_execution("run-B") is None


def test_local_command_authority_is_persisted_and_modes_rechecked_before_execution(runtime):
    controller = runtime
    controller.set_inputs(PLCInputs(temperature_c=150.0), now=AT, control_mode="LOCAL")
    local = request(
        controller,
        "LOAD_JOB",
        "local-load",
        work_order_id="work-A",
        recipe_version="r1",
        target_quantity=1,
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
    )
    assert controller.submit(local, now=AT, authority="LOCAL").submission.disposition == "ACCEPTED"
    controller.tick(now=AT)
    assert controller.get_result("local-load").status == "SUCCEEDED"
    start = request(controller, "START", "local-start")
    controller.submit(start, now=AT, authority="LOCAL")
    controller.set_inputs(PLCInputs(temperature_c=150.0), now=AT, control_mode="REMOTE")
    controller.tick(now=AT)
    assert controller.get_result("local-start").status == "FAILED"
    assert (
        not controller.state.run_output and controller.storage.load_execution().input_quantity == 0
    )


def test_fault_disables_actual_heating_and_reported_heater_tag(runtime):
    from apps.simulator.tags import project_tags

    controller = runtime
    load(controller)
    action(
        controller,
        "SET_PARAMETERS",
        "heat",
        recipe_version="hot",
        parameters={"temperature_c": 180.0, "speed_m_per_min": 6.0},
    )
    controller.tick(now=AT)
    controller.set_inputs(PLCInputs(temperature_c=150.0, fault_code=42), now=AT)
    before = controller.storage.load_execution().temperature_c
    controller.tick(now=AT)
    assert controller.storage.load_execution().temperature_c < before
    tags = project_tags(
        controller.state,
        controller.storage.load_execution(),
        counter_epoch=controller.storage.identity.counter_epoch,
        heartbeat=0,
    )
    assert tags["Sealer.HeaterOn"] is False


@pytest.mark.parametrize("component", ["state", "summary", "summary_identity"])
def test_corrupt_persisted_component_read_latches_recovery_even_if_caught(runtime, component):
    controller = runtime
    load(controller)
    if component == "state":
        controller.storage.connection.execute("UPDATE runtime_state SET snapshot_json='{}'")
    elif component == "summary":
        controller.storage.connection.execute("UPDATE runtime_runs SET summary_json='{}'")
    else:
        controller.storage.connection.execute(
            "UPDATE runtime_runs SET summary_json=json_set(summary_json,'$.execution_id','other')"
        )
    with pytest.raises(StorageCorruption):
        with controller.storage.transaction():
            try:
                if component == "state":
                    _ = controller.state
                else:
                    controller.get_summary("run-A")
            except StorageCorruption:
                pass
            controller.storage.connection.execute(
                "INSERT INTO runtime_audit "
                "(occurred_at,cause,control_version,details_json) "
                "VALUES ('x','must-rollback','0','{}')"
            )
    assert controller.storage.recovery_required
    assert (
        controller.storage.connection.execute(
            "SELECT COUNT(*) FROM runtime_audit WHERE cause='must-rollback'"
        ).fetchone()[0]
        == 0
    )
