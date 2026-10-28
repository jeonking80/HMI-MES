"""Original-recipe speed limits and independently verifiable parameter history."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.simulator.plc import PLCInputs, PLCState, check_command, execute_command
from contracts.models import (
    CommandRequest,
    LineState,
    Parameters,
    ParametersChangedEvent,
    RunSummary,
)
from contracts.parameter_history import validate_parameter_history

AT = datetime(2026, 10, 25, tzinfo=UTC)
BASE = Parameters(temperature_c=150.0, speed_m_per_min=10.0)


def active(state=LineState.RUNNING, *, baseline=BASE):
    return PLCState(
        state=state,
        recovery_required=False,
        control_mode="REMOTE",
        operation_mode="AUTO",
        execution_id="run-A",
        work_order_id="work-A",
        recipe_version="recipe-1",
        parameters=baseline,
        recipe_baseline=baseline,
        recipe_baseline_version="recipe-1",
        run_state=state.value,
        run_output=state == LineState.RUNNING,
        inputs=PLCInputs(
            temperature_c=150.0,
            run_feedback=state == LineState.RUNNING,
            speed_m_per_min=10.0 if state == LineState.RUNNING else 0.0,
        ),
    )


def request(state, speed, **changes):
    args = dict(
        recipe_version="recipe-1", parameters=dict(temperature_c=150.0, speed_m_per_min=speed)
    )
    args.update(changes.pop("args", {}))
    return CommandRequest.model_validate(
        dict(
            command_id="adjust",
            target_id="Line",
            execution_id="run-A",
            requested_by="manager",
            requested_at=AT,
            expires_at=AT + timedelta(seconds=5),
            expected_control_version=state.control_version,
            command=dict(kind="SET_PARAMETERS", args=args),
        )
        | changes
    )


@pytest.mark.parametrize("state", [LineState.RUNNING, LineState.PAUSED])
@pytest.mark.parametrize("speed", [8.0, 9.0, 10.0, 12.0])
def test_active_speed_accepts_inclusive_original_recipe_boundaries(state, speed):
    before = active(state)
    command = request(before, speed)
    assert check_command(before, command, now=AT).allowed
    transition = execute_command(before, command)
    assert transition.status == "SUCCEEDED"
    assert transition.state.parameters.speed_m_per_min == speed
    assert transition.state.recipe_baseline == BASE
    assert (
        transition.state.execution_id == "run-A" and transition.state.recipe_version == "recipe-1"
    )
    assert transition.state.state == state


@pytest.mark.parametrize("speed", [7.999, 12.001])
def test_adjustment_cannot_exceed_twenty_percent(speed):
    state = active()
    assert not check_command(state, request(state, speed), now=AT).allowed


def test_repeated_adjustment_uses_load_recipe_instead_of_last_speed():
    state = active()
    state = execute_command(state, request(state, 12.0)).state
    assert not check_command(state, request(state, 14.0), now=AT).allowed
    assert check_command(state, request(state, 8.0), now=AT).allowed
    assert state.recipe_baseline == BASE


@pytest.mark.parametrize("baseline,speed", [(1.0, 0.9), (60.0, 61.0)])
def test_recipe_percentage_does_not_override_plc_absolute_speed_limits(baseline, speed):
    state = active(baseline=Parameters(temperature_c=150.0, speed_m_per_min=baseline))
    assert not check_command(state, request(state, speed), now=AT).allowed


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_id": "run-B"},
        {"expected_control_version": 99},
        {"args": {"recipe_version": "recipe-2"}},
        {"args": {"parameters": {"temperature_c": 160.001, "speed_m_per_min": 11.0}}},
        {"args": {"parameters": {"temperature_c": 139.999, "speed_m_per_min": 11.0}}},
        {
            "args": {
                "parameters": {
                    "temperature_c": 150.0,
                    "speed_m_per_min": 11.0,
                    "air_pressure_bar": 5.0,
                }
            }
        },
    ],
)
def test_active_adjustment_refuses_identity_version_recipe_or_far_setpoint(changes):
    state = active()
    assert not check_command(state, request(state, 11.0, **changes), now=AT).allowed


@pytest.mark.parametrize("temperature", [140.0, 145.5, 160.0])
def test_active_temperature_moves_within_ten_degrees_of_load_recipe(temperature):
    before = active()
    command = request(
        before, 10.0, args={"parameters": {"temperature_c": temperature, "speed_m_per_min": 10.0}}
    )
    assert check_command(before, command, now=AT).allowed
    after = execute_command(before, command).state
    assert after.parameters.temperature_c == temperature and after.recipe_baseline == BASE


def test_active_pressure_moves_within_half_a_bar_and_plc_limits():
    loaded = Parameters(temperature_c=150.0, speed_m_per_min=10.0, air_pressure_bar=5.5)
    before = active(baseline=loaded)

    def pressure(value):
        return request(
            before,
            10.0,
            args={
                "parameters": {
                    "temperature_c": 150.0,
                    "speed_m_per_min": 10.0,
                    "air_pressure_bar": value,
                }
            },
        )

    for value in (5.0, 5.5, 6.0):
        assert check_command(before, pressure(value), now=AT).allowed
    assert not check_command(before, pressure(4.99), now=AT).allowed
    assert not check_command(before, pressure(6.01), now=AT).allowed
    # A job loaded with a pressure cannot drop it mid-run.
    assert not check_command(before, request(before, 10.0), now=AT).allowed


def test_parameters_without_pressure_keep_their_original_identity():
    assert BASE.model_dump(mode="json") == {"temperature_c": 150.0, "speed_m_per_min": 10.0}
    assert (
        Parameters(temperature_c=150.0, speed_m_per_min=10.0, air_pressure_bar=5.5).model_dump(
            mode="json"
        )["air_pressure_bar"]
        == 5.5
    )


def event(seq=1, *, previous=BASE, speed=12.0, **changes):
    body = dict(
        event_id=f"event-{seq}",
        source_id="source",
        stream_id="stream",
        seq=seq,
        equipment_id="Line",
        execution_id="run-A",
        occurred_at=AT,
        kind="PARAMETERS_CHANGED",
        payload=dict(
            command_id=f"command-{seq}",
            requested_by="manager",
            recipe_version="recipe-1",
            baseline_parameters=BASE,
            previous_parameters=previous,
            parameters=Parameters(temperature_c=150.0, speed_m_per_min=speed),
            control_version=seq,
        ),
    )
    body.update(changes)
    return ParametersChangedEvent.model_validate(body)


def summary(changes):
    return RunSummary(
        source_id="source",
        stream_id="stream",
        execution_id="run-A",
        state="ENDED",
        as_of=AT,
        ended_at=AT,
        counter_epoch="epoch",
        recipe_version="recipe-1",
        parameters=changes[-1].payload.parameters,
        baseline_parameters=BASE,
        parameter_changes=changes,
        input_quantity=0,
        good_quantity=0,
        reject_quantity=0,
        scrap_quantity=0,
        wip_quantity=0,
        last_event_seq=10,
        final=True,
    )


def test_valid_history_proves_current_parameters_against_original_registration():
    first = event()
    final = summary((first, event(2, previous=first.payload.parameters, speed=8.0)))
    validate_parameter_history(final, BASE, "recipe-1")


@pytest.mark.parametrize(
    "damage", ["missing", "chain", "baseline", "scope", "boundary", "range", "recipe"]
)
def test_history_rejects_missing_or_forged_proof(damage):
    first = event()
    final = summary((first,))
    body = final.model_dump(mode="json")
    if damage == "missing":
        body["parameter_changes"] = []
    elif damage == "baseline":
        body["baseline_parameters"]["speed_m_per_min"] = 11.0
    elif damage == "boundary":
        body["last_event_seq"] = 0
    else:
        changed = body["parameter_changes"][0]
        if damage == "chain":
            changed["payload"]["previous_parameters"]["speed_m_per_min"] = 9.0
        elif damage == "scope":
            changed["execution_id"] = "run-B"
        elif damage == "range":
            changed["payload"]["parameters"]["speed_m_per_min"] = 13.0
            body["parameters"]["speed_m_per_min"] = 13.0
        elif damage == "recipe":
            changed["payload"]["recipe_version"] = "recipe-2"
    with pytest.raises(ValueError):
        validate_parameter_history(RunSummary.model_validate(body), BASE, "recipe-1")


@pytest.mark.parametrize("changes", [{"execution_id": None}, {"equipment_id": "Sealer"}])
def test_parameter_event_requires_line_and_original_execution(changes):
    with pytest.raises(ValidationError):
        event(**changes)


def test_legacy_summary_serialization_omits_new_history_fields():
    body = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/examples/run-summary.json").read_text()
    )
    legacy = RunSummary.model_validate(body)
    assert legacy.baseline_parameters is None and legacy.parameter_changes == ()
    assert legacy.model_dump(mode="json") == body
    validate_parameter_history(legacy, legacy.parameters, legacy.recipe_version)
