"""Controller maintenance guard and real seven-day SQLite prefix retention."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from apps.simulator.controller import SimulatorController
from apps.simulator.plc import PLCInputs, RecoveryChecks
from apps.simulator.process import ProcessConfig
from apps.simulator.storage import (
    RecoveryRequired,
    RetentionBlocked,
    SimulatorStorage,
    StorageCorruption,
)
from contracts.models import CommandRequest, ReadEventsRequest

AT = datetime(2026, 10, 21, tzinfo=UTC)
WEEK = 7 * 24 * 60 * 60
FAST = ProcessConfig(stage_lengths_mm=(1, 1, 1), ambient_temperature_c=150.0)
CHECKS = RecoveryChecks(
    outputs_stopped=True,
    positions_verified=True,
    storage_reconciled=True,
    commands_reconciled=True,
    operator_confirmed=True,
)


class Clock:
    def __init__(self):
        self.utc = AT
        self.mono = 0.0

    def advance(self, seconds):
        self.utc += timedelta(seconds=seconds)
        self.mono += seconds


@pytest.fixture
def runtime(tmp_path):
    clock = Clock()
    path = tmp_path / "controller-retention.sqlite3"
    with SimulatorStorage(
        path, utc_clock=lambda: clock.utc, monotonic_clock=lambda: clock.mono
    ) as store:
        controller = SimulatorController(store, now=clock.utc, process_config=FAST)
        controller.recover(now=clock.utc, operator_id="operator", checks=CHECKS)
        controller.set_inputs(
            PLCInputs(temperature_c=150.0),
            now=clock.utc,
            control_mode="REMOTE",
            operation_mode="AUTO",
        )
        yield SimpleNamespace(controller=controller, store=store, clock=clock, path=path)


def command(runtime, kind, command_id, *, execution="run-A", **args):
    return CommandRequest(
        command_id=command_id,
        target_id="Line",
        execution_id=execution,
        command={"kind": kind, "args": args},
        requested_by="operator",
        requested_at=runtime.clock.utc,
        expires_at=runtime.clock.utc + timedelta(seconds=5),
        expected_control_version=runtime.controller.state.control_version,
    )


def apply(runtime, kind, command_id, *, execution="run-A", **args):
    request = command(runtime, kind, command_id, execution=execution, **args)
    assert (
        runtime.controller.submit(request, now=runtime.clock.utc).submission.disposition
        == "ACCEPTED"
    )
    runtime.controller.tick(now=runtime.clock.utc)


def load(runtime, execution="run-A"):
    apply(
        runtime,
        "LOAD_JOB",
        f"load-{execution}",
        execution=execution,
        work_order_id=f"work-{execution}",
        recipe_version=f"recipe-{execution}",
        target_quantity=1,
        parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
    )


def finish(runtime, execution="run-A"):
    load(runtime, execution)
    apply(runtime, "START", f"start-{execution}", execution=execution)
    runtime.controller.tick(now=runtime.clock.utc)
    runtime.controller.tick(now=runtime.clock.utc)
    apply(runtime, "END_RUN", f"end-{execution}", execution=execution)
    final = runtime.controller.get_summary(execution)
    assert final.final and final.wip_quantity == 0
    return final


def live(runtime):
    return [
        tuple(row)
        for row in runtime.store.connection.execute(
            "SELECT event_id,seq,body FROM source_events ORDER BY seq"
        )
    ]


def test_final_stopped_controller_prunes_bounded_prefix_without_ack_and_preserves_evidence(runtime):
    final = finish(runtime)
    before = live(runtime)
    runtime.clock.advance(WEEK + 1)
    first = runtime.controller.prune_events(limit=2)
    assert first.pruned_count == first.pruned_through_seq == 2
    assert live(runtime) == before[2:]
    rest = runtime.controller.prune_events()
    assert rest.pruned_count == len(before) - 2
    assert runtime.controller.get_summary("run-A") == final
    assert runtime.controller.state.last_event_seq == final.last_event_seq
    assert runtime.controller.prune_events().pruned_count == 0
    page = runtime.controller.read_events(
        ReadEventsRequest(
            source_id=runtime.store.identity.source_id,
            stream_id=runtime.store.identity.stream_id,
            after_seq=0,
        )
    )
    assert page.code == "RETENTION_GAP"
    assert page.missing_through_seq == final.last_event_seq


def test_controller_retention_does_not_require_final_boundary_equal_to_current_head(runtime):
    final = finish(runtime)
    runtime.controller.set_inputs(PLCInputs(temperature_c=150.0, estop=True), now=runtime.clock.utc)
    runtime.controller.set_inputs(PLCInputs(temperature_c=150.0), now=runtime.clock.utc)
    assert runtime.controller.state.state == "STOPPED"
    assert runtime.controller.state.last_event_seq > final.last_event_seq
    runtime.clock.advance(WEEK + 1)
    assert runtime.controller.prune_events().pruned_count > 0
    assert runtime.controller.get_summary("run-A") == final


@pytest.mark.parametrize("condition", ["running", "feedback", "fault", "unfinished", "pending"])
def test_blocked_maintenance_keeps_all_original_events(runtime, condition):
    if condition == "running":
        load(runtime)
        apply(runtime, "START", "running")
    elif condition == "unfinished":
        load(runtime)
        runtime.controller.set_inputs(PLCInputs(temperature_offset_c=-50.0), now=runtime.clock.utc)
        assert runtime.controller.state.state == "STOPPED"
    else:
        finish(runtime)
        if condition == "feedback":
            runtime.controller.set_inputs(
                PLCInputs(temperature_c=150.0, run_feedback=True, speed_m_per_min=1.0),
                now=runtime.clock.utc,
            )
        elif condition == "fault":
            runtime.controller.set_inputs(
                PLCInputs(temperature_c=150.0, fault_code=42), now=runtime.clock.utc
            )
        else:
            next_job = command(
                runtime,
                "LOAD_JOB",
                "pending-next",
                execution="run-B",
                work_order_id="work-B",
                recipe_version="recipe-B",
                target_quantity=1,
                parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
            )
            assert (
                runtime.controller.submit(
                    next_job,
                    now=runtime.clock.utc,
                    authenticated_gateway_principal="pinned-gateway",
                ).submission.disposition
                == "ACCEPTED"
            )
    before = live(runtime)
    runtime.clock.advance(WEEK + 1)
    with pytest.raises(RetentionBlocked):
        runtime.controller.prune_events()
    assert live(runtime) == before
    assert not runtime.store.recovery_required


def test_controller_maintenance_propagates_storage_clock_hold_and_freeze(runtime):
    before = live(runtime)
    with pytest.raises(RetentionBlocked, match="STARTUP_HOLD"):
        runtime.controller.prune_events()
    runtime.clock.utc += timedelta(days=8)
    with pytest.raises(RetentionBlocked, match="CLOCK_FROZEN"):
        runtime.controller.prune_events()
    assert live(runtime) == before


def test_retention_cannot_fake_recovery_acknowledgement(runtime):
    finish(runtime)
    before = live(runtime)
    runtime.store.mark_corrupt()
    runtime.clock.advance(WEEK + 1)
    with pytest.raises(RecoveryRequired):
        runtime.controller.prune_events()
    assert runtime.store.recovery_required
    assert live(runtime) == before


def two_finals(runtime):
    finish(runtime)
    runtime.controller.set_inputs(
        PLCInputs(temperature_c=150.0), now=runtime.clock.utc, control_mode="LOCAL"
    )
    runtime.controller.acknowledge_settlement(
        "run-A", now=runtime.clock.utc, operator_id="operator"
    )
    runtime.controller.set_inputs(
        PLCInputs(temperature_c=150.0), now=runtime.clock.utc, control_mode="REMOTE"
    )
    finish(runtime, "run-B")


def test_unfinished_earlier_execution_blocks_even_when_current_run_is_final(runtime):
    two_finals(runtime)
    previous = runtime.controller.get_summary("run-A").model_dump(mode="json")
    previous.update(state="ENDING", final=False, ended_at=None)
    # Disposable damaged-history fixture: active projection still matches B.
    runtime.store.connection.execute("DROP TRIGGER runtime_final_immutable")
    runtime.store.connection.execute(
        "UPDATE runtime_runs SET summary_json=? WHERE execution_id='run-A'", (json.dumps(previous),)
    )
    before = live(runtime)
    runtime.clock.advance(WEEK + 1)
    with pytest.raises(RetentionBlocked, match="UNFINISHED_EXECUTION"):
        runtime.controller.prune_events()
    assert live(runtime) == before


def test_inconsistent_inactive_final_projection_latches_recovery_and_preserves_events(runtime):
    two_finals(runtime)
    previous = runtime.controller.get_summary("run-A").model_dump(mode="json")
    previous.update(input_quantity="2", good_quantity="2")
    runtime.store.connection.execute("DROP TRIGGER runtime_final_immutable")
    runtime.store.connection.execute(
        "UPDATE runtime_runs SET summary_json=? WHERE execution_id='run-A'", (json.dumps(previous),)
    )
    before = live(runtime)
    runtime.clock.advance(WEEK + 1)
    with pytest.raises(StorageCorruption, match="quantities"):
        runtime.controller.prune_events()
    assert runtime.store.recovery_required
    assert live(runtime) == before


def test_controller_reopens_compacted_storage_and_keeps_final_quantities(runtime):
    final = finish(runtime)
    runtime.clock.advance(WEEK + 1)
    assert runtime.controller.prune_events().pruned_count > 0
    runtime.store.close()
    with SimulatorStorage(
        runtime.path,
        utc_clock=lambda: runtime.clock.utc,
        monotonic_clock=lambda: runtime.clock.mono,
    ) as store:
        controller = SimulatorController(store, now=runtime.clock.utc, process_config=FAST)
        controller.recover(now=runtime.clock.utc, operator_id="operator", checks=CHECKS)
        assert controller.get_summary("run-A") == final
        with pytest.raises(RetentionBlocked, match="STARTUP_HOLD"):
            controller.prune_events()


@pytest.mark.parametrize("evidence", ["witness", "live", "archived"])
def test_corrupt_inactive_final_boundary_blocks_retention_before_further_deletion(
    runtime, evidence
):
    two_finals(runtime)
    runtime.clock.advance(WEEK + 1)
    if evidence == "archived":
        assert runtime.controller.prune_events().pruned_count > 0
    before = live(runtime)
    archived = [
        tuple(row)
        for row in runtime.store.connection.execute(
            "SELECT * FROM archived_event_keys ORDER BY seq"
        )
    ]
    previous = runtime.controller.get_summary("run-A").model_dump(mode="json")
    assert int(previous["last_event_seq"]) > 1
    previous["last_event_seq"] = "1"
    runtime.store.connection.execute("DROP TRIGGER runtime_final_immutable")
    runtime.store.connection.execute(
        "UPDATE runtime_runs SET summary_json=? WHERE execution_id='run-A'", (json.dumps(previous),)
    )
    if evidence != "witness":
        # Independent damaged-evidence fixture: matching forged completion still
        # cannot hide later live or compacted production/scrap identities.
        runtime.store.connection.execute(
            "UPDATE command_journal SET result_json="
            "json_set(result_json,'$.evidence.last_event_seq','1') WHERE command_id='end-run-A'"
        )
    with pytest.raises(StorageCorruption, match="boundary"):
        runtime.controller.prune_events()
    assert runtime.store.recovery_required
    assert live(runtime) == before
    assert [
        tuple(row)
        for row in runtime.store.connection.execute(
            "SELECT * FROM archived_event_keys ORDER BY seq"
        )
    ] == archived
