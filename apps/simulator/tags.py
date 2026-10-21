"""48 simulator field channels derived from a consistent controller/process snapshot."""

from apps.simulator.plc import PLCState
from apps.simulator.process import ProcessState
from contracts.models import UINT64_MAX, LineState

_STATES = {state: index for index, state in enumerate(LineState)}


def heater_enabled(plc: PLCState, process: ProcessState | None = None) -> bool:
    """Physical thermostat permission; a test sensor bias does not drive the heater."""
    temperature = (
        process.temperature_c
        if process is not None
        else (plc.inputs.temperature_c - plc.inputs.temperature_offset_c)
    )
    return bool(
        plc.parameters
        and not plc.inputs.estop
        and not (plc.latched_fault_code or plc.inputs.fault_code)
        and not plc.recovery_required
        and temperature < plc.parameters.temperature_c
    )


def project_tags(
    plc: PLCState,
    process: ProcessState | None,
    *,
    counter_epoch: str,
    heartbeat: int,
    last_outcome: str | None = None,
) -> dict[str, bool | str | int | float]:
    """Values are simulated channels, not real hardware measurements.

    Caller must publish one snapshot with its acquisition time and status quality.
    Heartbeats reflect successful simulator cycles, never wall-clock timestamps.
    """
    if type(heartbeat) is not int or not 0 <= heartbeat <= UINT64_MAX:
        raise ValueError("Heartbeat must be exact UInt64")
    if not counter_epoch or not isinstance(counter_epoch, str):
        raise ValueError("Counter epoch is required")
    if last_outcome not in {None, "GOOD", "REJECT"}:
        raise ValueError("Invalid last inspection outcome")
    if plc.execution_id is not None and process is None:
        raise ValueError("Active execution requires its process snapshot")
    if process is not None and process.execution_id != plc.execution_id:
        raise ValueError("PLC and process execution snapshots differ")
    if process is not None and process.wip_quantity != plc.wip_quantity:
        raise ValueError("PLC and process WIP snapshots differ")
    fault = plc.latched_fault_code or plc.inputs.fault_code
    running = plc.inputs.run_feedback
    speed = float(plc.inputs.speed_m_per_min)
    values = {
        "Line.State": _STATES[plc.state],
        "Line.ControlMode": int(plc.control_mode == "REMOTE"),
        "Line.OperationMode": int(plc.operation_mode == "AUTO"),
        "Line.EStop": plc.inputs.estop,
        "Line.GuardClosed": plc.inputs.guard_closed,
        "Line.Permissive": plc.permissive,
        "Line.FaultCode": fault,
        "Line.ActiveExecutionId": plc.execution_id or "",
        "Line.RecipeVersion": plc.recipe_version or "",
        "Line.CounterEpoch": counter_epoch,
        "Line.TargetQuantity": plc.target_quantity,
        "Line.WipCount": plc.wip_quantity,
        "Line.GoodTotal": process.good_quantity if process else 0,
        "Line.RejectTotal": process.reject_quantity if process else 0,
        "Line.ScrapTotal": process.scrap_quantity if process else 0,
        "Line.Heartbeat": heartbeat,
        "Line.ControlVersion": plc.control_version,
        "Line.EventSeq": plc.last_event_seq,
    }
    for group in ("Infeed", "Sealer", "Outfeed"):
        values.update(
            {
                f"{group}.State": _STATES[plc.state],
                f"{group}.RunFeedback": running,
                f"{group}.FaultCode": fault,
                # Simple deterministic motor-load model; explicitly simulated amperes.
                f"{group}.MotorCurrent": 1.2 + 0.1 * speed if running else 0.0,
                f"{group}.ActualSpeed": speed,
                f"{group}.LinkHeartbeat": heartbeat,
            }
        )
    slots = process.slots if process else (None, None, None)
    values.update(
        {
            "Infeed.EntrySensor": slots[0] is not None,
            "Infeed.BufferCount": int(slots[0] is not None),
            "Sealer.TemperaturePV": float(plc.inputs.temperature_c),
            "Sealer.TemperatureSP": float(plc.parameters.temperature_c) if plc.parameters else 0.0,
            "Sealer.AirPressure": float(plc.inputs.pressure_bar),
            "Sealer.HeaterOn": heater_enabled(plc, process),
            "Sealer.GuardClosed": plc.inputs.guard_closed,
            "Sealer.Ready": plc.ready,
            "Outfeed.ExitSensor": slots[2] is not None,
            "Outfeed.CheckResult": {None: 0, "GOOD": 1, "REJECT": 2}[last_outcome],
            "Outfeed.GoodCounter": process.good_quantity if process else 0,
            "Outfeed.RejectCounter": process.reject_quantity if process else 0,
        }
    )
    return values
