"""Pure validation of PLC running-adjustment evidence against immutable LOAD identity."""

from decimal import Decimal

from contracts.models import Parameters, RunSummary

# How far a running job may move from the values it was loaded with.
SPEED_RATIO = Decimal("0.2")
TEMPERATURE_STEP_C = Decimal("10")
PRESSURE_STEP_BAR = Decimal("0.5")


def validate_running_adjustment(parameters: Parameters, baseline_parameters: Parameters) -> None:
    """Raise ValueError unless every value stays near the LOAD recipe.

    Speed within ±20 % (and 1–60 m/min), sealing temperature within ±10 °C and sealing
    pressure within ±0.5 bar. A job loaded without a pressure cannot gain one mid-run.
    """
    speed = Decimal(str(parameters.speed_m_per_min))
    baseline_speed = Decimal(str(baseline_parameters.speed_m_per_min))
    temperature = Decimal(str(parameters.temperature_c))
    baseline_temperature = Decimal(str(baseline_parameters.temperature_c))
    pressure, baseline_pressure = parameters.air_pressure_bar, baseline_parameters.air_pressure_bar
    if (
        not Decimal("1") <= speed <= Decimal("60")
        or not baseline_speed * (1 - SPEED_RATIO) <= speed <= baseline_speed * (1 + SPEED_RATIO)
        or abs(temperature - baseline_temperature) > TEMPERATURE_STEP_C
        or (pressure is None) != (baseline_pressure is None)
        or (
            pressure is not None
            and abs(Decimal(str(pressure)) - Decimal(str(baseline_pressure))) > PRESSURE_STEP_BAR
        )
    ):
        raise ValueError("running adjustment must stay near the original recipe")


def validate_parameter_history(
    summary: RunSummary,
    original_parameters: Parameters,
    original_recipe_version: str,
) -> None:
    """Raise ValueError for missing, unscoped, inconsistent or out-of-range evidence.

    A legacy summary with no history retains strict immutable-recipe comparison.
    Sequence gaps are allowed because production and state events share the stream.
    """
    if summary.recipe_version != original_recipe_version:
        raise ValueError("summary recipe does not match original LOAD")
    if (
        summary.baseline_parameters is not None
        and summary.baseline_parameters != original_parameters
    ):
        raise ValueError("summary baseline does not match original LOAD")
    if not summary.parameter_changes:
        if summary.parameters != original_parameters:
            raise ValueError("changed parameters require complete PLC event history")
        return
    if summary.baseline_parameters is None:
        raise ValueError("parameter history requires original LOAD baseline")
    previous = original_parameters
    previous_seq = 0
    previous_version = -1
    previous_time = None
    event_ids: set[str] = set()
    command_ids: set[str] = set()
    for event in summary.parameter_changes:
        payload = event.payload
        if (
            (event.source_id, event.stream_id, event.execution_id, event.equipment_id)
            != (summary.source_id, summary.stream_id, summary.execution_id, "Line")
            or not previous_seq < event.seq <= summary.last_event_seq
            or payload.control_version <= previous_version
            or event.occurred_at > (summary.ended_at or summary.as_of)
            or (previous_time is not None and event.occurred_at < previous_time)
            or event.event_id in event_ids
            or payload.command_id in command_ids
        ):
            raise ValueError("parameter events must have unique ordered scoped identities")
        if (
            payload.recipe_version != original_recipe_version
            or payload.baseline_parameters != original_parameters
            or payload.previous_parameters != previous
        ):
            raise ValueError("parameter event does not continue the original recipe chain")
        validate_running_adjustment(payload.parameters, original_parameters)
        event_ids.add(event.event_id)
        command_ids.add(payload.command_id)
        previous = payload.parameters
        previous_seq = event.seq
        previous_version = payload.control_version
        previous_time = event.occurred_at
    if previous != summary.parameters:
        raise ValueError("parameter history does not end at summary parameters")
