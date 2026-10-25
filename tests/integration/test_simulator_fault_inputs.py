"""Persistent sensor bias, real process separation and exact runtime V1 migration."""

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_simulator_controller import AT, FAST, action, load, request
from test_simulator_controller import runtime as runtime

from apps.simulator.controller import SimulatorController
from apps.simulator.plc import PLCInputs
from apps.simulator.storage import SimulatorStorage, StorageCorruption
from apps.simulator.tags import project_tags
from contracts.models import LineState


def inputs(controller, offset):
    # Full snapshot readback deliberately contains the OLD biased sensor value.
    value = controller.state.inputs.model_copy(update={"temperature_offset_c": offset})
    return controller.set_inputs(value, now=AT)


def test_running_sensor_deviation_is_immediate_sustained_and_applied_once(runtime):
    load(runtime)
    action(runtime, "START", "start-bias")
    base = runtime.storage.load_execution().temperature_c
    state = inputs(runtime, 15.0)
    assert state.inputs.temperature_c == base + 15.0
    assert not state.ready and state.state == LineState.RUNNING
    for tick in range(65):
        runtime.tick(now=AT + timedelta(seconds=tick / 10))
        assert (
            runtime.state.inputs.temperature_c
            == runtime.storage.load_execution().temperature_c + 15.0
        )
        assert runtime.state.state == LineState.RUNNING and runtime.state.inputs.run_feedback
    # Applying an identical snapshot must never add the bias for a second time.
    assert inputs(runtime, 15.0).inputs.temperature_c == base + 15.0
    assert inputs(runtime, 0.0).inputs.temperature_c == base
    assert runtime.storage.load_execution().temperature_c == base
    assert runtime.state.run_output  # Clearing a sensor scenario is not a stop/reset command.


def test_offset_immediately_blocks_start_and_clear_does_not_automatically_start(runtime):
    load(runtime)
    inputs(runtime, 15.0)
    assert not runtime.state.ready
    request_value = request(runtime, "START", "blocked-by-biased-sensor")
    result = runtime.submit(request_value, now=AT)
    assert result.submission.disposition == "REJECTED"
    assert not runtime.state.run_output
    assert inputs(runtime, 0.0).ready
    assert not runtime.state.run_output


def test_physical_thermostat_and_heater_feedback_ignore_sensor_bias(runtime):
    load(runtime)
    action(
        runtime,
        "SET_PARAMETERS",
        "hot-recipe",
        recipe_version="hot",
        parameters={"temperature_c": 180.0, "speed_m_per_min": 6.0},
    )
    process = runtime.storage.load_execution()
    inputs(runtime, 50.0)
    tags = project_tags(
        runtime.state, process, counter_epoch=runtime.storage.identity.counter_epoch, heartbeat=0
    )
    assert runtime.state.inputs.temperature_c > 180.0
    assert tags["Sealer.HeaterOn"] is True
    before = process.temperature_c
    runtime.tick(now=AT)
    assert runtime.storage.load_execution().temperature_c > before
    assert (
        runtime.state.inputs.temperature_c == runtime.storage.load_execution().temperature_c + 50.0
    )


def test_negative_offset_has_explicit_zero_floor_and_repeated_clear_is_exact(tmp_path):
    with SimulatorStorage(tmp_path / "zero-floor.sqlite3") as store:
        controller = SimulatorController(store, now=AT)
        assert inputs(controller, -50.0).inputs.temperature_c == 0.0
        assert inputs(controller, 0.0).inputs.temperature_c == 25.0
        assert inputs(controller, -10.0).inputs.temperature_c == 15.0
        assert inputs(controller, -10.0).inputs.temperature_c == 15.0
        assert inputs(controller, 0.0).inputs.temperature_c == 25.0


def test_offset_survives_restart_but_run_requires_explicit_recovery(tmp_path):
    path = tmp_path / "bias.sqlite3"
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT, process_config=FAST)
        from test_simulator_controller import CHECKS

        controller.recover(now=AT, operator_id="operator", checks=CHECKS)
        controller.set_inputs(
            PLCInputs(temperature_c=150.0), now=AT, control_mode="REMOTE", operation_mode="AUTO"
        )
        load(controller)
        action(controller, "START", "start-persisted-bias")
        inputs(controller, 15.0)
        base = store.load_execution().temperature_c
    with SimulatorStorage(path) as store:
        restored = SimulatorController(store, now=AT + timedelta(seconds=1), process_config=FAST)
        assert restored.state.inputs.temperature_offset_c == 15.0
        assert restored.state.inputs.temperature_c == base + 15.0
        assert store.load_execution().temperature_c == base
        assert restored.state.state == LineState.RECOVERY_REQUIRED
        assert restored.state.recovery_required and not restored.state.run_output
        assert inputs(restored, 0.0).recovery_required and not restored.state.run_output


def legacy_snapshot(snapshot, *, version):
    # These fields were added after runtime V2. Keep the historical shape exact
    # so V1 recognition and V2 conservative defaults exercise real migrations.
    for name in (
        "recipe_baseline",
        "recipe_baseline_version",
        "auto_end_at_target",
        "end_reason",
        "start_when_ready",
        "start_reservation",
        "start_reservation_cancel_reason",
    ):
        snapshot.pop(name)
    if version == 1:
        snapshot["inputs"].pop("temperature_offset_c")
    return snapshot


def remove_new_parameter_history(db):
    assert db.execute("SELECT COUNT(*) FROM runtime_parameter_changes").fetchone()[0] == 0
    # DROP TABLE also removes its two immutable-history triggers.
    db.execute("DROP TABLE runtime_parameter_changes")


def legacy_database(path, *, mutation=None):
    with SimulatorStorage(path) as store:
        SimulatorController(store, now=AT)
        snapshot = json.loads(
            store.connection.execute("SELECT snapshot_json FROM runtime_state").fetchone()[0]
        )
        legacy_snapshot(snapshot, version=1)
        if mutation:
            mutation(snapshot)
        original = json.dumps(snapshot, separators=(",", ":"))
        with store.transaction():
            remove_new_parameter_history(store.connection)
            store.connection.execute("UPDATE runtime_state SET snapshot_json=?", (original,))
            store.connection.execute("UPDATE runtime_schema_version SET version=1")
            store.connection.execute(
                "INSERT INTO runtime_audit "
                "(occurred_at,cause,execution_id,control_version,details_json) "
                "VALUES (?,'OLD_V1_EVIDENCE',NULL,'0',?)",
                (AT.isoformat(), original),
            )
    return original


def test_exact_v1_snapshot_migration_adds_only_zero_bias_and_preserves_audit(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    original = legacy_database(path)
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT)
        assert (
            store.connection.execute("SELECT version FROM runtime_schema_version").fetchone()[0]
            == 3
        )
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert controller.state.inputs.temperature_offset_c == 0.0
        assert controller.state.recovery_required
        assert (
            store.connection.execute(
                "SELECT details_json FROM runtime_audit WHERE cause='OLD_V1_EVIDENCE'"
            ).fetchone()[0]
            == original
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state["inputs"].pop("guard_closed"),
        lambda state: state["inputs"].update(temperature_offset_c=0.0),
        lambda state: state["inputs"].update(unknown_field=True),
        lambda state: state.update(unknown_state=True),
        lambda state: state["inputs"].update(guard_closed="true"),
    ],
)
def test_v1_migration_refuses_unrecognized_or_invalid_snapshot(tmp_path, mutation):
    path = tmp_path / "malformed.sqlite3"
    original = legacy_database(path, mutation=mutation)
    with SimulatorStorage(path) as store:
        with pytest.raises(StorageCorruption):
            SimulatorController(store, now=AT)
        assert (
            store.connection.execute("SELECT version FROM runtime_schema_version").fetchone()[0]
            == 1
        )
        assert (
            store.connection.execute("SELECT snapshot_json FROM runtime_state").fetchone()[0]
            == original
        )


def test_v1_migration_failure_rolls_back_snapshot_version_and_historical_audit(
    tmp_path, monkeypatch
):
    path = tmp_path / "migration-failure.sqlite3"
    original = legacy_database(path)

    def fail(_self):
        raise RuntimeError("injected runtime migration failure")

    monkeypatch.setattr(SimulatorController, "_before_runtime_migration_commit", fail)
    with SimulatorStorage(path) as store:
        with pytest.raises(RuntimeError, match="injected runtime migration"):
            SimulatorController(store, now=AT)
        assert (
            store.connection.execute("SELECT version FROM runtime_schema_version").fetchone()[0]
            == 1
        )
        assert (
            store.connection.execute("SELECT snapshot_json FROM runtime_state").fetchone()[0]
            == original
        )
        assert store.connection.execute("SELECT count(*) FROM runtime_audit").fetchone()[0] == 1


def test_v1_missing_snapshot_refuses_instead_of_silently_initializing(tmp_path):
    path = tmp_path / "missing.sqlite3"
    legacy_database(path)
    with SimulatorStorage(path) as store:
        store.connection.execute("DELETE FROM runtime_state")
        with pytest.raises(StorageCorruption):
            SimulatorController(store, now=AT)
        assert (
            store.connection.execute("SELECT version FROM runtime_schema_version").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("offset", [-50.1, 50.1, float("inf"), float("nan"), True, "15"])
def test_sensor_offset_requires_finite_bounded_numeric_value(offset):
    with pytest.raises(ValidationError):
        PLCInputs(temperature_offset_c=offset)


@pytest.mark.parametrize("version", [1, 2])
def test_startup_recomputes_stale_pv_before_recovery_or_start(tmp_path, version):
    from test_simulator_controller import CHECKS

    path = tmp_path / f"stale-pv-v{version}.sqlite3"
    with SimulatorStorage(path) as store:
        controller = SimulatorController(store, now=AT)
        controller.recover(now=AT, operator_id="operator", checks=CHECKS)
        controller.set_inputs(PLCInputs(), now=AT, control_mode="REMOTE", operation_mode="AUTO")
        command = request(
            controller,
            "LOAD_JOB",
            "load-cold",
            work_order_id="work",
            recipe_version="v1",
            target_quantity=100,
            parameters={"temperature_c": 150.0, "speed_m_per_min": 6.0},
        )
        controller.submit(command, now=AT)
        controller.tick(now=AT)
        base = store.load_execution().temperature_c
        assert base < 30
        legacy = legacy_snapshot(controller.state.model_dump(mode="json"), version=version)
        legacy["inputs"]["temperature_c"] = 150.0
        legacy["ready"] = True
        legacy["state"] = "READY"
        legacy["permissive"] = True
        with store.transaction():
            remove_new_parameter_history(store.connection)
            store.connection.execute(
                "UPDATE runtime_state SET snapshot_json=?", (json.dumps(legacy),)
            )
            store.connection.execute("UPDATE runtime_schema_version SET version=?", (version,))
    with SimulatorStorage(path) as store:
        restored = SimulatorController(store, now=AT)
        assert restored.state.inputs.temperature_c == base
        assert restored.state.state == LineState.RECOVERY_REQUIRED
        restored.recover(now=AT, operator_id="operator", checks=CHECKS)
        assert not restored.state.ready
        command = request(restored, "START", "cannot-start-on-stale-pv")
        assert restored.submit(command, now=AT).submission.disposition == "REJECTED"
        assert not restored.state.run_output
