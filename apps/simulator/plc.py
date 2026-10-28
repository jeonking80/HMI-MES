"""Pure immutable PLC decisions; callers own serialization, persistence and I/O.

Numeric limits are configurable simulator assumptions, never real equipment ratings.
Admission is separate from execution and observed completion. No call reads a clock,
creates an ID, authorizes a user, writes storage or operates a transport.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from contracts.models import (
    UINT64_MAX,
    CommandRequest,
    ContractModel,
    Identifier,
    LineState,
    Parameters,
    StrictBool,
    UInt32,
    UInt64,
    UTCTimestamp,
)
from contracts.parameter_history import validate_running_adjustment

Number = Annotated[float, Field(strict=True, ge=0)]
Mode = Literal["LOCAL", "REMOTE"]
OperationMode = Literal["MANUAL", "AUTO"]
RunState = Literal["LOADED", "RUNNING", "PAUSED", "ENDING", "ENDED"]
Kind = Literal["LOAD_JOB", "START", "PAUSE", "END_RUN", "SET_PARAMETERS", "RESET_FAULT"]


class PLCConfig(ContractModel):
    temperature_min_c: Number = 50.0
    temperature_max_c: Number = 200.0
    speed_min_m_per_min: Annotated[float, Field(strict=True, gt=0)] = 1.0
    speed_max_m_per_min: Annotated[float, Field(strict=True, gt=0)] = 60.0
    pressure_low_bar: Number = 4.0
    pressure_return_bar: Number = 4.5
    preheat_tolerance_c: Number = 5.0
    # The regulator seals below supply pressure and above the low-pressure alarm.
    seal_pressure_min_bar: Number = 4.5
    seal_pressure_max_bar: Number = 6.0
    guard_fault_code: Annotated[int, Field(strict=True, ge=1, le=2**32 - 1)] = 1001
    pressure_fault_code: Annotated[int, Field(strict=True, ge=1, le=2**32 - 1)] = 1002
    equipment_fault_code: Annotated[int, Field(strict=True, ge=1, le=2**32 - 1)] = 1003

    @model_validator(mode="after")
    def limits(self) -> Self:
        if (
            self.temperature_min_c > self.temperature_max_c
            or self.speed_min_m_per_min > self.speed_max_m_per_min
            or self.pressure_low_bar > self.pressure_return_bar
            or self.seal_pressure_min_bar > self.seal_pressure_max_bar
        ):
            raise ValueError("PLC limits must be ordered")
        return self


class PLCInputs(ContractModel):
    estop: StrictBool = False
    guard_closed: StrictBool = True
    fault_code: UInt32 = 0
    fault_resettable: StrictBool = True
    equipment_ready: StrictBool = True
    temperature_c: Number = 25.0
    # Test-only sensor bias; process physics and recipe setpoint remain separate.
    temperature_offset_c: Annotated[
        float, Field(strict=True, ge=-50, le=50, allow_inf_nan=False)
    ] = 0.0
    pressure_bar: Number = 6.0
    run_feedback: StrictBool = False
    speed_m_per_min: Number = 0.0


class StartSafety(ContractModel):
    estop: StrictBool
    guard_closed: StrictBool
    fault_code: UInt32
    fault_resettable: StrictBool
    equipment_ready: StrictBool
    latched_fault_code: UInt32
    pressure_ready: StrictBool


class StartReservation(ContractModel):
    command_id: Identifier
    requested_by: Identifier
    authority: Mode
    safety: StartSafety


class PLCState(ContractModel):
    target_id: Identifier = "Line"
    state: LineState = LineState.RECOVERY_REQUIRED
    control_version: UInt64 = 0
    control_mode: Mode = "LOCAL"
    operation_mode: OperationMode = "MANUAL"
    inputs: PLCInputs = Field(default_factory=PLCInputs)
    recovery_required: StrictBool = True
    latched_fault_code: UInt32 = 0
    pressure_ready: StrictBool = True
    permissive: StrictBool = False
    ready: StrictBool = False
    run_output: StrictBool = False
    execution_id: Identifier | None = None
    work_order_id: Identifier | None = None
    recipe_version: Identifier | None = None
    parameters: Parameters | None = None
    # Original LOAD values bound every subsequent active speed adjustment.
    recipe_baseline: Parameters | None = None
    recipe_baseline_version: Identifier | None = None
    target_quantity: UInt32 = 0
    auto_end_at_target: StrictBool = False
    start_when_ready: StrictBool = False
    start_reservation: StartReservation | None = None
    start_reservation_cancel_reason: Identifier | None = None
    end_reason: Literal["OPERATOR", "TARGET_REACHED"] | None = None
    run_state: RunState | None = None
    wip_quantity: UInt32 = 0
    last_event_seq: UInt64 = 0
    previous_execution_settled: StrictBool = True
    unresolved_conflict: StrictBool = False
    # Set only by execution; the caller serializes admission and reserves its journal row.
    pending_command_id: Identifier | None = None
    pending_kind: Kind | None = None
    stop_purpose: Literal["PAUSE", "END_RUN"] | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.pending_command_id is None) != (self.pending_kind is None):
            raise ValueError("pending command identity and kind must be paired")
        if self.start_reservation is not None and (
            not self.start_when_ready or self.execution_id is None or self.run_state != "LOADED"
        ):
            raise ValueError("start reservation requires an opted-in loaded execution")
        autonomous = (
            self.stop_purpose == "END_RUN"
            and self.pending_kind is None
            and self.auto_end_at_target
            and self.end_reason == "TARGET_REACHED"
            and self.run_state == "ENDING"
        )
        if (
            self.stop_purpose is not None
            and self.pending_kind != self.stop_purpose
            and not autonomous
        ):
            raise ValueError("stop purpose must match pending command")
        if self.execution_id is None and any(
            x is not None
            for x in (
                self.parameters,
                self.recipe_baseline,
                self.recipe_baseline_version,
                self.recipe_version,
                self.run_state,
            )
        ):
            raise ValueError("execution settings require an execution")
        if self.run_output and self.state not in {LineState.RUNNING, LineState.STOPPING}:
            raise ValueError("motion output requires a running or draining state")
        return self


class GuardDecision(ContractModel):
    allowed: StrictBool
    reason_code: Identifier | None = None
    reason: str | None = None


class PLCTransition(ContractModel):
    state: PLCState
    status: Literal["ACCEPTED", "SUCCEEDED", "FAILED"]
    reason_code: Identifier | None = None
    reason: str | None = None


class RecoveryChecks(ContractModel):
    outputs_stopped: StrictBool
    positions_verified: StrictBool
    storage_reconciled: StrictBool
    commands_reconciled: StrictBool
    operator_confirmed: StrictBool


DEFAULT_CONFIG = PLCConfig()
_TIMESTAMP = TypeAdapter(UTCTimestamp)
_AUTHORITY = TypeAdapter(Mode)


def _copy(snapshot: PLCState, **changes: object) -> PLCState:
    # model_copy(update=...) skips validation, so validate every constructed snapshot.
    return PLCState.model_validate({**snapshot.model_dump(), **changes})


def _control_tuple(s: PLCState) -> tuple:
    return (
        s.state,
        s.control_mode,
        s.operation_mode,
        s.execution_id,
        s.recipe_version,
        s.parameters,
        s.inputs.estop,
        s.inputs.guard_closed,
        s.inputs.fault_code,
        s.inputs.fault_resettable,
        s.latched_fault_code,
        s.permissive,
        s.ready,
        s.recovery_required,
        s.start_reservation,
        s.start_reservation_cancel_reason,
    )


def _commit(before: PLCState, after: PLCState) -> PLCState:
    if _control_tuple(before) == _control_tuple(after):
        return after
    if before.control_version == UINT64_MAX:
        raise ValueError("ControlVersion exhausted; never wrap")
    return _copy(after, control_version=before.control_version + 1)


def _within_limits(parameters: Parameters, config: PLCConfig) -> bool:
    return (
        config.temperature_min_c <= parameters.temperature_c <= config.temperature_max_c
        and config.speed_min_m_per_min <= parameters.speed_m_per_min <= config.speed_max_m_per_min
        and (
            parameters.air_pressure_bar is None
            or config.seal_pressure_min_bar
            <= parameters.air_pressure_bar
            <= config.seal_pressure_max_bar
        )
    )


def _start_safety(s: PLCState) -> StartSafety:
    return StartSafety(
        **{
            name: getattr(s.inputs, name)
            for name in (
                "estop",
                "guard_closed",
                "fault_code",
                "fault_resettable",
                "equipment_ready",
            )
        },
        latched_fault_code=s.latched_fault_code,
        pressure_ready=s.pressure_ready,
    )


def _cancel_start(s: PLCState, reason: str) -> PLCState:
    if s.start_reservation is None:
        return s
    return _copy(s, start_reservation=None, start_reservation_cancel_reason=reason)


def _check_start_reservation(s: PLCState) -> PLCState:
    reservation = s.start_reservation
    if reservation is None:
        return s
    if s.recovery_required:
        return _cancel_start(s, "RECOVERY_REQUIRED")
    if s.control_mode != reservation.authority:
        return _cancel_start(s, "CONTROL_MODE_CHANGED")
    if s.operation_mode != "AUTO":
        return _cancel_start(s, "OPERATION_MODE_CHANGED")
    if _start_safety(s) != reservation.safety or not s.permissive:
        return _cancel_start(s, "SAFETY_INPUT_CHANGED")
    return s


def _evaluate(s: PLCState, config: PLCConfig, *, promote_ready: bool = True) -> PLCState:
    i = s.inputs
    fault = (
        s.latched_fault_code
        or i.fault_code
        or (config.guard_fault_code if not i.guard_closed else 0)
    )
    pressure = i.pressure_bar >= (
        config.pressure_low_bar if s.pressure_ready else config.pressure_return_bar
    )
    if s.run_output or i.run_feedback or i.speed_m_per_min > 0:
        fault = fault or (
            config.pressure_fault_code
            if not pressure
            else config.equipment_fault_code
            if not i.equipment_ready
            else 0
        )
    permissive = bool(
        not i.estop
        and not fault
        and not s.recovery_required
        and i.guard_closed
        and i.equipment_ready
        and pressure
    )
    ready = bool(
        permissive
        and s.execution_id
        and s.parameters
        and s.recipe_version
        and s.run_state not in {None, "ENDING", "ENDED"}
        and _within_limits(s.parameters, config)
        and abs(i.temperature_c - s.parameters.temperature_c) <= config.preheat_tolerance_c
    )
    state = s.state
    output = s.run_output
    if i.estop:
        state, output = LineState.ESTOP, False
    elif fault:
        state, output = LineState.FAULT, False
    elif s.recovery_required:
        state, output = LineState.RECOVERY_REQUIRED, False
    elif state in {LineState.ESTOP, LineState.FAULT, LineState.RECOVERY_REQUIRED}:
        # Releasing a safety condition never restores previous motion.
        state, output = LineState.STOPPED, False
    elif state == LineState.READY and not ready:
        state = LineState.STOPPED
    elif promote_ready and state == LineState.STOPPED and ready:
        state = LineState.READY
    evaluated = _copy(
        s,
        state=state,
        run_output=output,
        latched_fault_code=fault,
        pressure_ready=pressure,
        permissive=permissive,
        ready=ready,
    )
    return _check_start_reservation(evaluated)


def update_inputs(
    state: PLCState,
    inputs: PLCInputs,
    *,
    config: PLCConfig = DEFAULT_CONFIG,
    control_mode: Mode | None = None,
    operation_mode: OperationMode | None = None,
    wip_quantity: int | None = None,
    last_event_seq: int | None = None,
) -> PLCState:
    """Apply one observed boundary; measurement/counter changes alone do not bump version."""
    changes: dict[str, object] = {"inputs": inputs}
    for name, value in (
        ("control_mode", control_mode),
        ("operation_mode", operation_mode),
        ("wip_quantity", wip_quantity),
        ("last_event_seq", last_event_seq),
    ):
        if value is not None:
            changes[name] = value
    return _commit(state, _evaluate(_copy(state, **changes), config))


def acknowledge_recovery(
    state: PLCState,
    checks: RecoveryChecks,
    *,
    config: PLCConfig = DEFAULT_CONFIG,
) -> PLCState:
    """Local reconciliation gate; caller must persist operator evidence atomically."""
    checked = _evaluate(state, config)
    if (
        not all(checks.model_dump().values())
        or checked.inputs.estop
        or checked.latched_fault_code
        or checked.inputs.run_feedback
        or checked.inputs.speed_m_per_min != 0
        or checked.pending_command_id
        or checked.unresolved_conflict
    ):
        raise ValueError(
            "Recovery requires stopped, safe, reconciled state and explicit confirmation"
        )
    return _commit(
        state,
        _evaluate(
            _copy(checked, recovery_required=False, state=LineState.STOPPED, run_output=False),
            config,
            promote_ready=False,
        ),
    )


def restart(state: PLCState, *, config: PLCConfig = DEFAULT_CONFIG) -> PLCState:
    """Latch restart recovery and inhibit outputs; retain actual observed feedback."""
    return _commit(
        state,
        _evaluate(
            _copy(_cancel_start(state, "RESTART"), recovery_required=True, run_output=False),
            config,
        ),
    )


def cancel_pending_after_recovery(
    state: PLCState,
    *,
    config: PLCConfig = DEFAULT_CONFIG,
) -> PLCState:
    """Clear a reconciled interrupted reservation while the recovery gate is closed.

    The caller must atomically persist its original-ID terminal result/evidence.
    This is a local reconciliation primitive, never a new OPC command.
    """
    if (
        not state.recovery_required
        or state.inputs.run_feedback
        or state.inputs.speed_m_per_min != 0
    ):
        raise ValueError("Interrupted commands require recovery and confirmed stopped drives")
    after = _copy(
        state,
        pending_command_id=None,
        pending_kind=None,
        stop_purpose=None,
        run_output=False,
    )
    return _commit(state, _evaluate(after, config))


def _deny(code: str, reason: str) -> GuardDecision:
    return GuardDecision(allowed=False, reason_code=code, reason=reason)


def _guards(
    s: PLCState,
    request: CommandRequest,
    config: PLCConfig,
    authority: Mode,
) -> GuardDecision:
    authority = _AUTHORITY.validate_python(authority)
    kind = request.command.kind
    if request.target_id != s.target_id:
        return _deny("TARGET_MISMATCH", "Command targets another line")
    if s.pending_command_id is not None or s.unresolved_conflict:
        return _deny("COMMAND_CONFLICT", "Resolve the existing command before a new command")
    if s.control_mode != authority:
        return _deny(
            "LOCAL_MODE" if authority == "REMOTE" else "REMOTE_MODE",
            f"{authority} commands require {authority} control mode",
        )
    if kind != "LOAD_JOB" and request.execution_id != s.execution_id:
        return _deny("EXECUTION_MISMATCH", "Command execution does not match the active execution")
    if kind == "RESET_FAULT":
        if (
            s.state != LineState.FAULT
            or not s.latched_fault_code
            or s.inputs.estop
            or not s.inputs.guard_closed
            or s.inputs.fault_code
            or not s.inputs.fault_resettable
            or not s.pressure_ready
            or not s.inputs.equipment_ready
            or s.inputs.run_feedback
            or s.inputs.speed_m_per_min != 0
        ):
            return _deny("RESET_BLOCKED", "Fault cause must be cleared and all drives stopped")
        return GuardDecision(allowed=True)
    if s.recovery_required:
        return _deny("RECOVERY_REQUIRED", "Explicit recovery confirmation is required")
    if kind == "LOAD_JOB":
        if (
            s.state != LineState.STOPPED
            or s.wip_quantity
            or s.inputs.run_feedback
            or s.inputs.speed_m_per_min != 0
            or (s.execution_id is not None and s.run_state != "ENDED")
            or not s.previous_execution_settled
            or request.execution_id == s.execution_id
        ):
            return _deny(
                "LOAD_BLOCKED", "Load requires stopped drives, zero WIP and settled prior run"
            )
        if request.command.args.start_when_ready:
            if s.operation_mode != "AUTO":
                return _deny("MANUAL_MODE", "Reserved START requires AUTO operation mode")
            if not s.permissive:
                return _deny(
                    "START_RESERVATION_BLOCKED", "Reserved START requires live safety permissives"
                )
    elif kind == "START":
        if s.operation_mode != "AUTO":
            return _deny("MANUAL_MODE", "START requires AUTO operation mode")
        if s.state not in {LineState.READY, LineState.PAUSED} or not s.ready:
            return _deny("NOT_READY", "START requires a ready, safe, preheated active execution")
    elif kind == "PAUSE":
        if s.state != LineState.RUNNING or s.run_state != "RUNNING":
            return _deny("PAUSE_BLOCKED", "PAUSE requires the matching running execution")
    elif kind == "END_RUN":
        if (
            s.execution_id is None
            or s.run_state in {None, "ENDED"}
            or s.state
            not in {
                LineState.STOPPED,
                LineState.READY,
                LineState.RUNNING,
                LineState.PAUSED,
                LineState.FAULT,
                LineState.ESTOP,
            }
        ):
            return _deny("END_BLOCKED", "Execution cannot enter the ending sequence")
        # Safety states may end only with stopped positions; WIP requires explicit local scrap.
        if s.state in {LineState.FAULT, LineState.ESTOP} and (
            s.inputs.run_feedback or s.inputs.speed_m_per_min != 0
        ):
            return _deny("UNSAFE_WIP", "Safety-state ending requires confirmed stopped drives")
        if (
            s.wip_quantity
            and s.state not in {LineState.FAULT, LineState.ESTOP}
            and not s.permissive
        ):
            return _deny("UNSAFE_WIP", "WIP drain requires live pressure and equipment permissives")
    elif kind == "SET_PARAMETERS":
        args = request.command.args
        if s.state in {LineState.RUNNING, LineState.PAUSED}:
            if (
                s.execution_id is None
                or s.run_state != s.state.value
                or s.parameters is None
                or s.recipe_baseline is None
                or s.recipe_baseline_version is None
                or not s.permissive
                or (
                    s.state == LineState.PAUSED
                    and (s.inputs.run_feedback or s.inputs.speed_m_per_min != 0)
                )
            ):
                return _deny(
                    "PARAMETERS_BLOCKED", "Speed adjustment requires a safe active execution"
                )
            if (
                args.recipe_version != s.recipe_version
                or args.recipe_version != s.recipe_baseline_version
            ):
                return _deny("PARAMETERS_BLOCKED", "Active adjustment must retain the recipe")
            try:
                validate_running_adjustment(args.parameters, s.recipe_baseline)
            except ValueError:
                return _deny(
                    "PARAMETERS_OUT_OF_RANGE",
                    "Speed ±20 %, temperature ±10 °C and pressure ±0.5 bar of the LOAD recipe",
                )
        elif (
            s.execution_id is None
            or s.run_state in {None, "ENDING", "ENDED"}
            or s.state not in {LineState.STOPPED, LineState.READY}
            or s.inputs.run_feedback
            or s.inputs.speed_m_per_min != 0
        ):
            return _deny("PARAMETERS_BLOCKED", "Parameters require a stopped active execution")
        elif args.recipe_version == s.recipe_version:
            return _deny("RECIPE_VERSION_REUSED", "A setting change requires a new recipe version")
    if kind in {"LOAD_JOB", "SET_PARAMETERS"} and not _within_limits(
        request.command.args.parameters,
        config,
    ):
        return _deny("PARAMETERS_OUT_OF_RANGE", "Recipe exceeds configured simulator limits")
    return GuardDecision(allowed=True)


def check_command(
    state: PLCState,
    request: CommandRequest,
    *,
    now: datetime,
    config: PLCConfig = DEFAULT_CONFIG,
    authority: Mode = "REMOTE",
) -> GuardDecision:
    """Admission only; the journal must reserve and serialize accepted requests."""
    now = _TIMESTAMP.validate_python(now)
    if now >= request.expires_at:
        return _deny("COMMAND_EXPIRED", "First-seen command has reached its delivery expiry")
    if request.expected_control_version != state.control_version:
        return _deny(
            "CONTROL_VERSION_MISMATCH", "Read the current control version before requesting"
        )
    return _guards(_evaluate(state, config), request, config, authority)


def _failed(state: PLCState, decision: GuardDecision) -> PLCTransition:
    return PLCTransition(
        state=state,
        status="FAILED",
        reason_code=decision.reason_code,
        reason=decision.reason,
    )


def execute_command(
    state: PLCState,
    request: CommandRequest,
    *,
    config: PLCConfig = DEFAULT_CONFIG,
    authority: Mode = "REMOTE",
) -> PLCTransition:
    """Execute an already admitted request at a serialized process boundary.

    TTL and the original expected version are admission predicates, not completion
    predicates. Recheck modes, execution, current state and physical conditions.
    """
    checked = _evaluate(state, config)
    decision = _guards(checked, request, config, authority)
    if not decision.allowed:
        return _failed(_commit(state, checked), decision)
    command = request.command
    kind = command.kind
    changes: dict[str, object] = {}
    if kind == "LOAD_JOB":
        changes = dict(
            execution_id=request.execution_id,
            work_order_id=command.args.work_order_id,
            recipe_version=command.args.recipe_version,
            parameters=command.args.parameters,
            recipe_baseline=command.args.parameters,
            recipe_baseline_version=command.args.recipe_version,
            target_quantity=command.args.target_quantity,
            auto_end_at_target=command.args.auto_end_at_target,
            start_when_ready=command.args.start_when_ready,
            start_reservation=(
                StartReservation(
                    command_id=request.command_id,
                    requested_by=request.requested_by,
                    authority=authority,
                    safety=_start_safety(checked),
                )
                if command.args.start_when_ready
                else None
            ),
            start_reservation_cancel_reason=None,
            end_reason=None,
            run_state="LOADED",
            previous_execution_settled=False,
        )
    elif kind == "SET_PARAMETERS":
        checked = _cancel_start(checked, "PARAMETERS_CHANGED")
        changes = dict(
            recipe_version=command.args.recipe_version, parameters=command.args.parameters
        )
    elif kind == "RESET_FAULT":
        changes = dict(latched_fault_code=0, state=LineState.STOPPED, run_output=False)
    else:
        if kind in {"START", "END_RUN"}:
            checked = _cancel_start(checked, "MANUAL_START" if kind == "START" else "MANUAL_END")
        changes = dict(pending_command_id=request.command_id, pending_kind=kind)
        if kind == "START":
            changes.update(state=LineState.RUNNING, run_output=True)
        elif kind == "PAUSE":
            changes.update(state=LineState.STOPPING, run_output=False, stop_purpose="PAUSE")
        else:
            changes.update(run_state="ENDING", stop_purpose="END_RUN", end_reason="OPERATOR")
            if checked.state not in {LineState.FAULT, LineState.ESTOP}:
                changes.update(state=LineState.STOPPING, run_output=checked.wip_quantity > 0)
    result = _evaluate(_copy(checked, **changes), config, promote_ready=kind != "RESET_FAULT")
    result = _commit(state, result)
    return PLCTransition(
        state=result,
        status="ACCEPTED" if kind in {"START", "PAUSE", "END_RUN"} else "SUCCEEDED",
    )


def finish_command(
    state: PLCState,
    request: CommandRequest,
    *,
    final_boundary_durable: bool = False,
    config: PLCConfig = DEFAULT_CONFIG,
    authority: Mode = "REMOTE",
) -> PLCTransition:
    """Complete a pending operation from observed feedback and durable END evidence.

    END's durable flag attests the caller atomically fixed final quantities, recipe,
    epoch, last_event_seq and ended_at. This function itself provides no durability.
    """
    authority = _AUTHORITY.validate_python(authority)
    if (state.pending_command_id, state.pending_kind) != (
        request.command_id,
        request.command.kind,
    ):
        return _failed(
            state, _deny("PENDING_MISMATCH", "Request does not own the pending operation")
        )
    s = _evaluate(state, config)
    kind = request.command.kind
    decision = None
    if request.target_id != s.target_id or request.execution_id != s.execution_id:
        decision = _deny(
            "EXECUTION_MISMATCH", "Pending request identity no longer matches the line"
        )
    elif s.control_mode != authority:
        decision = _deny(
            "LOCAL_MODE" if authority == "REMOTE" else "REMOTE_MODE",
            f"{authority} control permission was removed during execution",
        )
    elif kind == "START" and (
        s.state != LineState.RUNNING or not s.ready or s.operation_mode != "AUTO"
    ):
        decision = _deny(
            "START_INTERRUPTED", "Safety, readiness or mode changed before START feedback"
        )
    elif kind == "PAUSE" and s.state != LineState.STOPPING:
        decision = _deny("PAUSE_INTERRUPTED", "Safety state interrupted pause completion")
    if decision is not None:
        # A failed START must never leave a latent motion output enabled.
        s = _copy(
            s, pending_command_id=None, pending_kind=None, stop_purpose=None, run_output=False
        )
        if s.state in {LineState.RUNNING, LineState.STOPPING}:
            s = _copy(s, state=LineState.STOPPED)
        return _failed(_commit(state, s), decision)
    complete = False
    changes: dict[str, object] = {}
    if kind == "START":
        complete = s.inputs.run_feedback
        changes = dict(run_state="RUNNING")
    elif kind == "PAUSE":
        complete = not s.inputs.run_feedback and s.inputs.speed_m_per_min == 0
        changes = dict(state=LineState.PAUSED, run_state="PAUSED", run_output=False)
    elif kind == "END_RUN":
        # Never scrap, clear safety causes, or claim a final boundary implicitly.
        complete = (
            s.wip_quantity == 0
            and not s.inputs.run_feedback
            and s.inputs.speed_m_per_min == 0
            and final_boundary_durable
        )
        changes = dict(run_state="ENDED", run_output=False, end_reason="OPERATOR")
        if s.state not in {LineState.FAULT, LineState.ESTOP, LineState.RECOVERY_REQUIRED}:
            changes["state"] = LineState.STOPPED
        if s.wip_quantity == 0:
            s = _copy(s, run_output=False)
    if not complete:
        return PLCTransition(state=_commit(state, s), status="ACCEPTED")
    s = _copy(s, **changes, pending_command_id=None, pending_kind=None, stop_purpose=None)
    return PLCTransition(state=_commit(state, s), status="SUCCEEDED")


def advance_start_reservation(
    state: PLCState,
    *,
    config: PLCConfig = DEFAULT_CONFIG,
) -> PLCState:
    """Consume original LOAD intent once at a safe READY boundary, without a command."""
    s = _evaluate(state, config)
    if (
        s.start_reservation is not None
        and s.state == LineState.READY
        and s.ready
        and s.run_state == "LOADED"
        and s.pending_command_id is None
        and not s.unresolved_conflict
        and not s.inputs.run_feedback
        and s.inputs.speed_m_per_min == 0
    ):
        s = _copy(
            s,
            start_reservation=None,
            start_reservation_cancel_reason=None,
            state=LineState.RUNNING,
            run_state="RUNNING",
            run_output=True,
        )
    return _commit(state, s)


def advance_target_end(
    state: PLCState,
    *,
    good_quantity: int,
    config: PLCConfig = DEFAULT_CONFIG,
) -> PLCState:
    """End an opted-in run from observed target output without inventing a command.

    The owner persists final quantities, the final event boundary and an audit proof
    in the same transaction. Existing commands and safety conditions retain priority.
    """
    s = _evaluate(state, config)
    if (
        not s.auto_end_at_target
        or s.pending_command_id is not None
        or s.unresolved_conflict
        or s.recovery_required
        or s.state in {LineState.FAULT, LineState.ESTOP, LineState.RECOVERY_REQUIRED}
    ):
        return _commit(state, s)
    if (
        s.run_state == "RUNNING"
        and s.state == LineState.RUNNING
        and s.operation_mode == "AUTO"
        and s.permissive
        and good_quantity >= s.target_quantity
    ):
        s = _copy(
            s,
            run_state="ENDING",
            state=LineState.STOPPING,
            stop_purpose="END_RUN",
            end_reason="TARGET_REACHED",
            run_output=s.wip_quantity > 0,
        )
    if (
        s.run_state == "ENDING"
        and s.end_reason == "TARGET_REACHED"
        and s.wip_quantity == 0
        and not s.inputs.run_feedback
        and s.inputs.speed_m_per_min == 0
    ):
        s = _copy(
            s,
            run_state="ENDED",
            state=LineState.STOPPED,
            run_output=False,
            stop_purpose=None,
        )
    return _commit(state, _evaluate(s, config))
