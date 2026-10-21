"""PLC guards, safety precedence, asynchronous completion and version boundaries."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from apps.simulator.plc import (
    PLCConfig,
    PLCInputs,
    PLCState,
    RecoveryChecks,
    acknowledge_recovery,
    cancel_pending_after_recovery,
    check_command,
    execute_command,
    finish_command,
    restart,
    update_inputs,
)
from contracts.models import UINT64_MAX, CommandRequest, LineState

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def changed(model, **changes):
    return type(model).model_validate({**model.model_dump(), **changes})


def inputs(state, **changes):
    return changed(state.inputs, **changes)


def request(state, kind, *, command_id="cmd-A", execution_id="execution-A", **overrides):
    args = {}
    if kind == "LOAD_JOB":
        args = dict(
            work_order_id="order-A",
            recipe_version="recipe-A",
            target_quantity=100,
            parameters=dict(temperature_c=150.0, speed_m_per_min=6.0),
        )
    if kind == "SET_PARAMETERS":
        args = dict(
            recipe_version="recipe-B",
            parameters=dict(temperature_c=160.0, speed_m_per_min=8.0),
        )
    return CommandRequest.model_validate(
        dict(
            command_id=command_id,
            target_id="Line",
            execution_id=execution_id,
            command=dict(kind=kind, args=args),
            requested_by="operator-A",
            requested_at=NOW,
            expires_at=NOW + timedelta(seconds=5),
            expected_control_version=state.control_version,
            **overrides,
        )
    )


def stopped():
    return PLCState(
        state=LineState.STOPPED,
        recovery_required=False,
        control_mode="REMOTE",
        operation_mode="AUTO",
        inputs=PLCInputs(temperature_c=150.0),
    )


def loaded():
    s = stopped()
    return execute_command(s, request(s, "LOAD_JOB")).state


def running():
    s = loaded()
    req = request(s, "START")
    start = execute_command(s, req)
    s = update_inputs(start.state, inputs(start.state, run_feedback=True, speed_m_per_min=6.0))
    return finish_command(s, req).state


def test_load_start_observed_feedback_pause_and_resume_same_execution():
    s = stopped()
    load = request(s, "LOAD_JOB")
    assert check_command(s, load, now=NOW).allowed
    loaded_result = execute_command(s, load)
    s = loaded_result.state
    assert loaded_result.status == "SUCCEEDED"
    assert (s.state, s.execution_id, s.run_state) == (LineState.READY, "execution-A", "LOADED")
    req = request(s, "START")
    start = execute_command(s, req)
    assert start.status == "ACCEPTED" and start.state.run_output
    assert finish_command(start.state, req).status == "ACCEPTED"
    s = update_inputs(
        start.state, inputs(s, run_feedback=True, speed_m_per_min=6.0), wip_quantity=2
    )
    started = finish_command(s, req)
    assert started.status == "SUCCEEDED"
    s = started.state
    pause = request(s, "PAUSE")
    stopping = execute_command(s, pause)
    assert stopping.state.state == LineState.STOPPING
    assert stopping.state.stop_purpose == "PAUSE" and not stopping.state.run_output
    assert finish_command(stopping.state, pause).status == "ACCEPTED"
    s = update_inputs(stopping.state, inputs(s, run_feedback=False, speed_m_per_min=0.0))
    paused = finish_command(s, pause)
    assert paused.status == "SUCCEEDED"
    assert paused.state.state == LineState.PAUSED and paused.state.wip_quantity == 2
    assert paused.state.execution_id == "execution-A"
    assert check_command(paused.state, request(paused.state, "START"), now=NOW).allowed


def test_safety_priority_latches_guard_fault_and_reset_never_restarts():
    s = running()
    s = update_inputs(s, inputs(s, estop=True, guard_closed=False), wip_quantity=2)
    assert s.state == LineState.ESTOP and not s.run_output
    assert s.latched_fault_code == 1001 and s.execution_id == "execution-A"
    s = update_inputs(
        s, inputs(s, estop=False, guard_closed=True, run_feedback=False, speed_m_per_min=0.0)
    )
    assert s.state == LineState.FAULT
    reset = request(s, "RESET_FAULT")
    result = execute_command(s, reset)
    assert result.status == "SUCCEEDED"
    assert result.state.state == LineState.STOPPED and not result.state.run_output
    assert result.state.latched_fault_code == 0 and result.state.wip_quantity == 2


def test_estop_release_alone_stays_stopped_and_preserves_recovery_gate():
    s = PLCState(inputs=PLCInputs(estop=True))
    s = update_inputs(s, s.inputs)
    assert s.state == LineState.ESTOP
    s = update_inputs(s, inputs(s, estop=False))
    assert s.state == LineState.RECOVERY_REQUIRED and s.recovery_required
    assert not s.run_output


def test_recovery_requires_all_reconciliation_checks_and_outputs_stopped():
    checks = RecoveryChecks(
        outputs_stopped=True,
        positions_verified=True,
        storage_reconciled=True,
        commands_reconciled=True,
        operator_confirmed=True,
    )
    s = PLCState()
    with pytest.raises(ValueError, match="Recovery requires"):
        acknowledge_recovery(s, changed(checks, commands_reconciled=False))
    with pytest.raises(ValueError, match="Recovery requires"):
        acknowledge_recovery(changed(s, inputs=PLCInputs(run_feedback=True)), checks)
    recovered = acknowledge_recovery(s, checks)
    assert recovered.state == LineState.STOPPED and not recovered.recovery_required
    assert not recovered.run_output and recovered.control_version == 1
    assert execute_command(s, request(s, "LOAD_JOB")).status == "FAILED"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"control_mode": "LOCAL"}, "LOCAL_MODE"),
        ({"operation_mode": "MANUAL"}, "MANUAL_MODE"),
        ({"inputs": PLCInputs(temperature_c=25.0)}, "NOT_READY"),
        ({"inputs": PLCInputs(temperature_c=150.0, guard_closed=False)}, "NOT_READY"),
        ({"inputs": PLCInputs(temperature_c=150.0, estop=True)}, "NOT_READY"),
    ],
)
def test_start_guards(change, code):
    s = loaded()
    s = changed(s, **change)
    result = check_command(s, request(s, "START"), now=NOW)
    assert not result.allowed and result.reason_code == code


def test_admission_expiry_version_execution_target_and_conflict_guards():
    s = loaded()
    req = request(s, "START")
    assert check_command(s, req, now=req.expires_at).reason_code == "COMMAND_EXPIRED"
    stale = changed(req, expected_control_version=s.control_version + 1)
    assert check_command(s, stale, now=NOW).reason_code == "CONTROL_VERSION_MISMATCH"
    wrong = changed(req, execution_id="other")
    assert check_command(s, wrong, now=NOW).reason_code == "EXECUTION_MISMATCH"
    assert (
        check_command(s, changed(req, target_id="other"), now=NOW).reason_code == "TARGET_MISMATCH"
    )
    assert check_command(changed(s, unresolved_conflict=True), req, now=NOW).reason_code == (
        "COMMAND_CONFLICT"
    )


def test_fault_after_admitted_start_fails_execution_and_pending_completion():
    s = loaded()
    req = request(s, "START")
    assert check_command(s, req, now=NOW).allowed
    fault = update_inputs(s, inputs(s, fault_code=77))
    result = execute_command(fault, req)
    assert result.status == "FAILED" and result.state.state == LineState.FAULT
    assert not result.state.run_output
    pending = execute_command(s, req).state
    interrupted = update_inputs(pending, inputs(pending, guard_closed=False))
    result = finish_command(interrupted, req)
    assert result.status == "FAILED" and result.reason_code == "START_INTERRUPTED"
    assert result.state.state == LineState.FAULT and not result.state.run_output
    assert result.state.pending_command_id is None


def test_completion_uses_semantic_recheck_not_original_expected_version_or_ttl():
    s = loaded()
    req = request(s, "START")
    pending = execute_command(s, req).state
    assert pending.control_version > req.expected_control_version
    s = update_inputs(pending, inputs(pending, run_feedback=True))
    assert finish_command(s, req).status == "SUCCEEDED"
    # execute is only for already admitted requests; TTL is never a cancellation clock.
    assert execute_command(loaded(), req).status == "ACCEPTED"


@pytest.mark.parametrize("kind", ["START", "PAUSE", "END_RUN", "SET_PARAMETERS", "LOAD_JOB"])
def test_pause_reservation_rejects_conflicting_commands(kind):
    s = running()
    pause = request(s, "PAUSE")
    pending = execute_command(s, pause).state
    assert check_command(
        pending, request(pending, kind, command_id="other"), now=NOW
    ).reason_code == ("COMMAND_CONFLICT")
    assert finish_command(pending, request(pending, "END_RUN", command_id="other")).reason_code == (
        "PENDING_MISMATCH"
    )


def test_end_drains_wip_requires_stopped_feedback_and_durable_final_boundary():
    s = changed(running(), wip_quantity=2)
    req = request(s, "END_RUN")
    end = execute_command(s, req)
    assert end.state.run_output and end.state.run_state == "ENDING"
    assert finish_command(end.state, req, final_boundary_durable=True).status == "ACCEPTED"
    s = update_inputs(end.state, inputs(end.state), wip_quantity=0, last_event_seq=9)
    assert not finish_command(s, req).state.run_output
    s = update_inputs(s, inputs(s, run_feedback=False, speed_m_per_min=0.0))
    assert finish_command(s, req).status == "ACCEPTED"
    result = finish_command(s, req, final_boundary_durable=True)
    assert result.status == "SUCCEEDED"
    assert result.state.run_state == "ENDED" and result.state.state == LineState.STOPPED
    assert result.state.last_event_seq == 9 and result.state.execution_id == "execution-A"
    assert not check_command(
        result.state, request(result.state, "LOAD_JOB", execution_id="new"), now=NOW
    ).allowed
    settled = changed(result.state, previous_execution_settled=True)
    assert check_command(settled, request(settled, "LOAD_JOB", execution_id="new"), now=NOW).allowed


@pytest.mark.parametrize("estop", [False, True])
def test_end_during_safety_state_never_clears_cause_or_automatically_scraps(estop):
    s = loaded()
    s = update_inputs(s, inputs(s, fault_code=22, estop=estop), wip_quantity=1)
    req = request(s, "END_RUN")
    assert check_command(s, req, now=NOW).allowed
    end = execute_command(s, req)
    assert not end.state.run_output
    waiting = finish_command(end.state, req, final_boundary_durable=True)
    assert waiting.status == "ACCEPTED" and waiting.state.wip_quantity == 1
    # Only the external explicit scrap+storage transition can supply zero WIP.
    s = update_inputs(waiting.state, waiting.state.inputs, wip_quantity=0, last_event_seq=1)
    result = finish_command(s, req, final_boundary_durable=True)
    assert result.status == "SUCCEEDED" and result.state.run_state == "ENDED"
    assert result.state.state == (LineState.ESTOP if estop else LineState.FAULT)
    assert result.state.latched_fault_code == 22 and result.state.inputs.estop == estop


def test_reset_is_blocked_by_cause_estop_unresettable_fault_or_motion():
    s = update_inputs(loaded(), PLCInputs(fault_code=8, temperature_c=150.0))
    for observation in (
        s.inputs,
        inputs(s, fault_code=0, estop=True),
        inputs(s, fault_code=0, fault_resettable=False),
        inputs(s, fault_code=0, run_feedback=True),
        inputs(s, fault_code=0, speed_m_per_min=0.1),
    ):
        state = update_inputs(s, observation)
        assert not check_command(state, request(state, "RESET_FAULT"), now=NOW).allowed


def test_reset_preserves_recovery_and_does_not_promote_ready():
    s = changed(loaded(), recovery_required=True)
    s = update_inputs(s, inputs(s, fault_code=8))
    s = update_inputs(s, inputs(s, fault_code=0))
    result = execute_command(s, request(s, "RESET_FAULT"))
    assert result.status == "SUCCEEDED"
    assert result.state.state == LineState.RECOVERY_REQUIRED and result.state.recovery_required


def test_recipe_range_limits_setting_state_new_version_and_preheat_reevaluation():
    s = loaded()
    req = request(s, "SET_PARAMETERS")
    result = execute_command(s, req)
    assert result.status == "SUCCEEDED" and result.state.recipe_version == "recipe-B"
    assert result.state.state == LineState.STOPPED and not result.state.ready
    assert result.state.parameters.temperature_c == 160
    assert check_command(
        result.state, request(result.state, "SET_PARAMETERS"), now=NOW
    ).reason_code == ("RECIPE_VERSION_REUSED")
    assert not check_command(running(), request(running(), "SET_PARAMETERS"), now=NOW).allowed
    narrow = PLCConfig(temperature_max_c=155.0)
    assert check_command(s, req, now=NOW, config=narrow).reason_code == "PARAMETERS_OUT_OF_RANGE"
    config = PLCConfig(temperature_min_c=160.0, speed_min_m_per_min=7.0)
    base = stopped()
    assert check_command(base, request(base, "LOAD_JOB"), now=NOW, config=config).reason_code == (
        "PARAMETERS_OUT_OF_RANGE"
    )


def test_pressure_hysteresis_and_preheat_are_configurable_simulator_assumptions():
    config = PLCConfig(pressure_low_bar=3.0, pressure_return_bar=4.0, preheat_tolerance_c=2.0)
    s = loaded()
    s = update_inputs(s, inputs(s, pressure_bar=2.9), config=config)
    assert not s.ready and not s.pressure_ready
    s = update_inputs(s, inputs(s, pressure_bar=3.5), config=config)
    assert not s.ready
    s = update_inputs(s, inputs(s, pressure_bar=4.0, temperature_c=148.0), config=config)
    assert s.ready and s.pressure_ready
    s = update_inputs(s, inputs(s, pressure_bar=3.0, temperature_c=147.9), config=config)
    assert s.pressure_ready and not s.ready
    s = update_inputs(s, inputs(s, temperature_c=152.0), config=config)
    assert s.ready
    assert s.latched_fault_code == 0  # Alarm duration policy belongs to DEV-14.


def test_control_version_one_per_tuple_transaction_not_measurements_or_counters():
    s = loaded()
    version = s.control_version
    observed = update_inputs(
        s, inputs(s, temperature_c=151.0, pressure_bar=5.0), wip_quantity=2, last_event_seq=100
    )
    assert observed.control_version == version
    multiple = update_inputs(
        observed,
        inputs(observed, estop=True, guard_closed=False),
        control_mode="LOCAL",
        operation_mode="MANUAL",
    )
    assert multiple.control_version == version + 1
    assert multiple.state == LineState.ESTOP and multiple.latched_fault_code == 1001
    assert not multiple.ready and not multiple.permissive
    assert update_inputs(multiple, multiple.inputs) == multiple
    assert check_command(s, request(s, "PAUSE"), now=NOW).allowed is False
    assert s.control_version == version


def test_uint64_json_roundtrip_immutability_validation_and_exhaustion():
    s = changed(loaded(), control_version=UINT64_MAX, last_event_seq=UINT64_MAX)
    wire = s.model_dump_json()
    assert f'"control_version":"{UINT64_MAX}"' in wire
    assert PLCState.model_validate_json(wire) == s
    with pytest.raises(ValidationError):
        s.control_version = 0
    for invalid in (-1, UINT64_MAX + 1, True):
        with pytest.raises(ValidationError):
            changed(s, control_version=invalid)
    unchanged = update_inputs(s, inputs(s, temperature_c=151.0))
    assert unchanged.control_version == UINT64_MAX
    with pytest.raises(ValueError, match="exhausted"):
        update_inputs(s, inputs(s, estop=True))
    assert s.state == LineState.READY and not s.inputs.estop
    with pytest.raises(ValidationError):
        PLCConfig(pressure_low_bar=5.0, pressure_return_bar=4.0)
    with pytest.raises(ValidationError):
        PLCInputs(temperature_c=float("nan"))


@pytest.mark.parametrize(
    "observation,code",
    [
        ({"pressure_bar": 0.0}, 1002),
        ({"equipment_ready": False}, 1003),
    ],
)
def test_live_motion_interlocks_latch_immediately_and_require_cause_cleared(observation, code):
    s = running()
    fault = update_inputs(s, inputs(s, **observation))
    assert fault.state == LineState.FAULT and not fault.run_output
    assert fault.latched_fault_code == code
    stopped_fault = update_inputs(fault, inputs(fault, run_feedback=False, speed_m_per_min=0.0))
    assert not check_command(stopped_fault, request(stopped_fault, "RESET_FAULT"), now=NOW).allowed
    cleared = update_inputs(
        stopped_fault,
        inputs(
            stopped_fault,
            pressure_bar=6.0,
            equipment_ready=True,
        ),
    )
    assert cleared.state == LineState.FAULT  # Returning measurements do not reset a latch.
    assert (
        execute_command(cleared, request(cleared, "RESET_FAULT")).state.state == LineState.STOPPED
    )


def test_end_from_pause_cannot_enable_motion_without_live_permissive_but_needs_no_preheat():
    s = changed(loaded(), state=LineState.PAUSED, run_state="PAUSED", wip_quantity=2)
    low = update_inputs(s, inputs(s, pressure_bar=0.0))
    req = request(low, "END_RUN")
    assert check_command(low, req, now=NOW).reason_code == "UNSAFE_WIP"
    assert execute_command(low, req).status == "FAILED"
    assert not execute_command(low, req).state.run_output
    # Temperature is a start/preheat condition; draining still uses live safety interlocks.
    cold = update_inputs(s, inputs(s, temperature_c=25.0))
    assert not cold.ready and cold.permissive
    end = execute_command(cold, request(cold, "END_RUN"))
    assert end.status == "ACCEPTED" and end.state.run_output
    fault = update_inputs(end.state, inputs(end.state, equipment_ready=False))
    assert fault.state == LineState.FAULT and not fault.run_output


def test_restart_and_explicit_interrupted_command_reconciliation_preserve_identity_and_safety():
    s = loaded()
    req = request(s, "START")
    pending = execute_command(s, req).state
    recovered = restart(pending)
    assert recovered.recovery_required and recovered.state == LineState.RECOVERY_REQUIRED
    assert not recovered.run_output and recovered.pending_command_id == req.command_id
    assert recovered.control_version == pending.control_version + 1
    assert recovered.execution_id == pending.execution_id
    cleared = cancel_pending_after_recovery(recovered)
    assert cleared.pending_command_id is None and cleared.stop_purpose is None
    assert cleared.recovery_required and cleared.control_version == recovered.control_version
    with pytest.raises(ValueError, match="recovery"):
        cancel_pending_after_recovery(pending)
    moving = changed(recovered, inputs=inputs(recovered, run_feedback=True))
    with pytest.raises(ValueError, match="stopped"):
        cancel_pending_after_recovery(moving)
    fault = update_inputs(pending, inputs(pending, estop=True, fault_code=44))
    restarted_fault = restart(fault)
    assert restarted_fault.state == LineState.ESTOP and restarted_fault.latched_fault_code == 44
    assert (
        restarted_fault.recovery_required and restarted_fault.pending_command_id == req.command_id
    )


@pytest.mark.parametrize("authority,other", [("LOCAL", "REMOTE"), ("REMOTE", "LOCAL")])
def test_authority_must_match_stored_mode_at_admission_execution_and_completion(authority, other):
    s = changed(loaded(), control_mode=authority)
    req = request(s, "START")
    assert check_command(s, req, now=NOW, authority=authority).allowed
    assert not check_command(s, req, now=NOW, authority=other).allowed
    assert execute_command(s, req, authority=other).status == "FAILED"
    pending = execute_command(s, req, authority=authority)
    assert pending.status == "ACCEPTED" and pending.state.control_mode == authority
    observed = update_inputs(pending.state, inputs(pending.state, run_feedback=True))
    assert finish_command(observed, req, authority=authority).status == "SUCCEEDED"
    assert finish_command(observed, req, authority=other).status == "FAILED"
    switched = update_inputs(observed, observed.inputs, control_mode=other)
    result = finish_command(switched, req, authority=authority)
    assert result.status == "FAILED" and not result.state.run_output
    assert result.state.control_mode == other


@pytest.mark.parametrize("authority", ["LOCAL", "REMOTE"])
@pytest.mark.parametrize(
    "change",
    [
        {"operation_mode": "MANUAL"},
        {"inputs": PLCInputs(temperature_c=25.0)},
        {"inputs": PLCInputs(temperature_c=150.0, estop=True)},
        {"inputs": PLCInputs(temperature_c=150.0, guard_closed=False)},
    ],
)
def test_local_authority_has_no_safety_or_auto_bypass(authority, change):
    s = changed(loaded(), control_mode=authority, **change)
    req = request(s, "START")
    assert not check_command(s, req, now=NOW, authority=authority).allowed
    result = execute_command(s, req, authority=authority)
    assert result.status == "FAILED" and not result.state.run_output


def test_local_load_uses_same_conditions_without_changing_mode_and_invalid_authority_rejected():
    s = changed(stopped(), control_mode="LOCAL")
    req = request(s, "LOAD_JOB")
    assert not check_command(s, req, now=NOW).allowed  # Default remains REMOTE.
    assert check_command(s, req, now=NOW, authority="LOCAL").allowed
    result = execute_command(s, req, authority="LOCAL")
    assert result.status == "SUCCEEDED" and result.state.control_mode == "LOCAL"
    assert result.state.control_version == s.control_version + 1
    for call in (
        lambda: check_command(s, req, now=NOW, authority="INVALID"),
        lambda: execute_command(s, req, authority="INVALID"),
        lambda: finish_command(s, req, authority="INVALID"),
    ):
        with pytest.raises(ValidationError):
            call()
