"""Version 1 application contracts; no transport, state machine, or persistence."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    ValidationInfo,
    WithJsonSchema,
    model_validator,
)

UINT64_MAX = 2**64 - 1


def _decimal_pattern(minimum: int = 0) -> str:
    """Match canonical decimal strings within UInt64, including its upper bound."""
    bound = str(UINT64_MAX)
    choices = ["[1-9][0-9]{0,18}"]
    if minimum == 0:
        choices.insert(0, "0")
    for index, digit in enumerate(bound):
        lower = 1 if index == 0 else 0
        upper = int(digit) - 1
        if upper >= lower:
            choices.append(bound[:index] + f"[{lower}-{upper}]" + f"[0-9]{{{19 - index}}}")
    choices.append(bound)
    return "^(?:" + "|".join(choices) + ")$(?![\\s\\S])"


def _uint64(value: object, info: ValidationInfo) -> object:
    # Python callers use ints; the JSON wire contract always uses decimal strings.
    if info.mode == "json" and not isinstance(value, str):
        raise ValueError("UInt64 JSON values must be canonical decimal strings")
    if isinstance(value, str):
        if not value.isascii() or not value.isdecimal() or (len(value) > 1 and value[0] == "0"):
            raise ValueError("UInt64 requires a canonical decimal string")
        if len(value) > 20:
            raise ValueError("UInt64 out of range")
        return int(value)
    return value


def _uint_schema(minimum: int) -> dict[str, object]:
    return {
        "type": "string",
        "pattern": _decimal_pattern(minimum),
        "description": "UInt64 canonical decimal string; use BigInt in JavaScript.",
    }


UInt64 = Annotated[
    int,
    Field(strict=True, ge=0, le=UINT64_MAX),
    BeforeValidator(_uint64),
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema(_uint_schema(0)),
]
PositiveUInt64 = Annotated[
    int,
    Field(strict=True, ge=1, le=UINT64_MAX),
    BeforeValidator(_uint64),
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema(_uint_schema(1)),
]
Identifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^\S+$"),
    WithJsonSchema({"type": "string", "minLength": 1, "maxLength": 128, "not": {"pattern": r"\s"}}),
]
Reason = Annotated[str, Field(min_length=1, max_length=1000, pattern=r"\S")]


def _timestamp(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("timestamps require an RFC3339 string with explicit UTC offset")
    return value


UTCTimestamp = Annotated[
    AwareDatetime,
    BeforeValidator(_timestamp),
    AfterValidator(lambda value: value.astimezone(UTC)),
]
UInt32 = Annotated[int, Field(strict=True, ge=0, le=2**32 - 1)]
StrictBool = Annotated[bool, Field(strict=True)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class LineState(StrEnum):
    STOPPED = "STOPPED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    FAULT = "FAULT"
    ESTOP = "ESTOP"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class Parameters(ContractModel):
    # Physical operating limits are configured and checked by the PLC in DEV-06.
    temperature_c: Annotated[
        float, Field(strict=True, ge=0), AfterValidator(lambda value: 0.0 if value == 0 else value)
    ]
    speed_m_per_min: Annotated[float, Field(strict=True, gt=0)]


class LoadJobArguments(ContractModel):
    work_order_id: Identifier
    recipe_version: Identifier
    target_quantity: Annotated[int, Field(strict=True, gt=0, le=2**32 - 1)]
    parameters: Parameters


class SetParametersArguments(ContractModel):
    recipe_version: Identifier
    parameters: Parameters


class EmptyArguments(ContractModel):
    pass


class LoadJob(ContractModel):
    kind: Literal["LOAD_JOB"]
    args: LoadJobArguments


class Start(ContractModel):
    kind: Literal["START"]
    args: EmptyArguments = Field(default_factory=EmptyArguments)


class Pause(ContractModel):
    kind: Literal["PAUSE"]
    args: EmptyArguments = Field(default_factory=EmptyArguments)


class EndRun(ContractModel):
    kind: Literal["END_RUN"]
    args: EmptyArguments = Field(default_factory=EmptyArguments)


class SetParameters(ContractModel):
    kind: Literal["SET_PARAMETERS"]
    args: SetParametersArguments


class ResetFault(ContractModel):
    kind: Literal["RESET_FAULT"]
    args: EmptyArguments = Field(default_factory=EmptyArguments)


Command = Annotated[
    LoadJob | Start | Pause | EndRun | SetParameters | ResetFault, Field(discriminator="kind")
]


class CommandRequest(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    command_id: Identifier
    target_id: Identifier
    execution_id: Identifier | None
    command: Command
    requested_by: Identifier
    requested_at: UTCTimestamp
    expires_at: UTCTimestamp
    expected_control_version: UInt64

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.expires_at <= self.requested_at:
            raise ValueError("expires_at must follow requested_at")
        if (
            self.command.kind in {"LOAD_JOB", "START", "PAUSE", "END_RUN"}
            and self.execution_id is None
        ):
            raise ValueError("run commands require execution_id")
        return self


def canonical_command_payload(request: CommandRequest) -> str:
    """Stable semantic comparison for a previously stored command_id."""
    return json.dumps(
        request.model_dump(mode="json", exclude={"command_id"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def same_command_payload(left: CommandRequest, right: CommandRequest) -> bool:
    return canonical_command_payload(left) == canonical_command_payload(right)


class CommandSubmission(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    command_id: Identifier
    disposition: Literal["ACCEPTED", "REJECTED"]
    received_at: UTCTimestamp
    reason_code: Identifier | None = None
    reason: Reason | None = None

    @model_validator(mode="after")
    def rejection_reason(self) -> Self:
        if self.disposition == "REJECTED" and (self.reason_code is None or self.reason is None):
            raise ValueError("rejected submission requires reason_code and reason")
        if self.disposition == "ACCEPTED" and (
            self.reason_code is not None or self.reason is not None
        ):
            raise ValueError("accepted submission cannot carry a rejection reason")
        return self


class CommandResultRequest(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    command_id: Identifier


class RunSummaryRequest(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    execution_id: Identifier


class CommandNotFound(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    code: Literal["COMMAND_NOT_FOUND"]
    command_id: Identifier
    reason: Reason


class RunNotFound(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    code: Literal["RUN_NOT_FOUND"]
    execution_id: Identifier
    reason: Reason


class CompletionEvidence(ContractModel):
    state: LineState
    execution_id: Identifier | None
    recipe_version: Identifier | None = None
    last_event_seq: UInt64 | None = None


class CommandResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    command_id: Identifier
    status: Literal[
        "REQUESTED",
        "DELIVERED",
        "ACCEPTED",
        "SUCCEEDED",
        "FAILED",
        "REJECTED",
        "EXPIRED",
        "OUTCOME_UNKNOWN",
    ]
    updated_at: UTCTimestamp
    delivered_at: UTCTimestamp | None = None
    completed_at: UTCTimestamp | None = None
    reason_code: Identifier | None = None
    reason: Reason | None = None
    applied_control_version: UInt64 | None = None
    evidence: CompletionEvidence | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        terminal = self.status in {"SUCCEEDED", "FAILED", "REJECTED", "EXPIRED"}
        if terminal != (self.completed_at is not None):
            raise ValueError("only terminal known outcomes require completed_at")
        if self.status == "SUCCEEDED" and (
            self.evidence is None or self.applied_control_version is None
        ):
            raise ValueError("success requires completion evidence and applied_control_version")
        if self.status != "SUCCEEDED" and (
            self.evidence is not None or self.applied_control_version is not None
        ):
            raise ValueError("only success carries completion evidence and applied_control_version")
        if self.status == "REQUESTED" and self.delivered_at is not None:
            raise ValueError("requested status cannot already be delivered")
        if self.status in {"FAILED", "REJECTED", "EXPIRED", "OUTCOME_UNKNOWN"} and (
            self.reason_code is None or self.reason is None
        ):
            raise ValueError("unsuccessful or unknown outcome requires a reason")
        if self.status == "EXPIRED" and self.delivered_at is not None:
            raise ValueError("only commands that were never delivered may expire")
        if (
            self.status in {"DELIVERED", "ACCEPTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"}
            and self.delivered_at is None
        ):
            raise ValueError("delivered command status requires delivered_at")
        if self.delivered_at is not None and self.delivered_at > self.updated_at:
            raise ValueError("delivered_at cannot follow updated_at")
        if self.completed_at is not None and (
            self.completed_at > self.updated_at
            or (self.delivered_at is not None and self.completed_at < self.delivered_at)
        ):
            raise ValueError("completion timestamps are out of order")
        return self


class EventBase(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    seq: PositiveUInt64
    equipment_id: Identifier
    execution_id: Identifier | None
    occurred_at: UTCTimestamp


class ProductionPayload(ContractModel):
    quantity: PositiveUInt64
    counter_epoch: Identifier


class ScrapPayload(ProductionPayload):
    reason_code: Identifier
    reason: Reason


class ProductionEvent(EventBase):
    kind: Literal["PRODUCTION_GOOD", "PRODUCTION_REJECT"]
    execution_id: Identifier
    payload: ProductionPayload


class ScrapEvent(EventBase):
    kind: Literal["WIP_SCRAP"]
    execution_id: Identifier
    payload: ScrapPayload


class StatePayload(ContractModel):
    previous_state: LineState
    state: LineState
    control_version: UInt64


class StateEvent(EventBase):
    kind: Literal["STATE_CHANGED"]
    payload: StatePayload


class FaultPayload(ContractModel):
    fault_code: Annotated[int, Field(strict=True, gt=0, le=2**32 - 1)]
    active: StrictBool


class FaultEvent(EventBase):
    kind: Literal["FAULT_CHANGED"]
    payload: FaultPayload


Event = Annotated[
    ProductionEvent | ScrapEvent | StateEvent | FaultEvent, Field(discriminator="kind")
]
EVENT_ADAPTER = TypeAdapter(Event)


class ReadEventsRequest(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    stream_id: Identifier
    after_seq: UInt64
    limit: Annotated[int, Field(strict=True, ge=1, le=1000)] = 100


class EventPage(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    stream_id: Identifier
    after_seq: UInt64
    retained_from_seq: PositiveUInt64
    retained_through_seq: UInt64
    events: Annotated[tuple[Event, ...], Field(max_length=1000)]
    page_last_seq: UInt64
    has_more: StrictBool

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        # Empty log: retained_from_seq == retained_through_seq + 1.
        if self.retained_from_seq > self.retained_through_seq + 1:
            raise ValueError("invalid retained bounds")
        if not self.retained_from_seq - 1 <= self.after_seq <= self.retained_through_seq:
            raise ValueError("cursor outside retained bounds requires an explicit error")
        expected = self.after_seq
        ids: set[str] = set()
        for event in self.events:
            expected += 1
            if (event.source_id, event.stream_id) != (
                self.source_id,
                self.stream_id,
            ) or event.seq != expected:
                raise ValueError("page events must be contiguous and belong to this stream")
            if event.event_id in ids:
                raise ValueError("duplicate event_id inside page")
            ids.add(event.event_id)
        if self.page_last_seq != expected or self.page_last_seq > self.retained_through_seq:
            raise ValueError("page_last_seq must equal last event or after_seq for empty page")
        if self.has_more != (self.page_last_seq < self.retained_through_seq):
            raise ValueError("has_more must agree with retained upper bound")
        if self.has_more and not self.events:
            raise ValueError("non-final page must make progress")
        return self


class EventReadError(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    stream_id: Identifier
    after_seq: UInt64
    code: Literal["RETENTION_GAP", "CURSOR_AHEAD", "STREAM_MISMATCH"]
    retained_from_seq: PositiveUInt64
    retained_through_seq: UInt64
    missing_from_seq: PositiveUInt64 | None = None
    missing_through_seq: PositiveUInt64 | None = None
    reason: Reason

    @model_validator(mode="after")
    def validate_gap(self) -> Self:
        if self.retained_from_seq > self.retained_through_seq + 1:
            raise ValueError("invalid retained bounds")
        if self.code == "RETENTION_GAP":
            if self.after_seq + 1 >= self.retained_from_seq:
                raise ValueError("retention gap requires a cursor before retained history")
            if (self.missing_from_seq, self.missing_through_seq) != (
                self.after_seq + 1,
                self.retained_from_seq - 1,
            ):
                raise ValueError("missing range must describe the entire lost interval")
        elif self.missing_from_seq is not None or self.missing_through_seq is not None:
            raise ValueError("only retention gaps carry a missing interval")
        if self.code == "CURSOR_AHEAD" and self.after_seq <= self.retained_through_seq:
            raise ValueError("cursor-ahead error requires cursor beyond retained history")
        return self


class RunSummary(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    source_id: Identifier
    stream_id: Identifier
    execution_id: Identifier
    state: Literal["LOADED", "RUNNING", "PAUSED", "ENDING", "ENDED"]
    as_of: UTCTimestamp
    counter_epoch: Identifier
    recipe_version: Identifier
    parameters: Parameters
    input_quantity: UInt64
    good_quantity: UInt64
    reject_quantity: UInt64
    scrap_quantity: UInt64
    wip_quantity: UInt32
    last_event_seq: UInt64
    final: StrictBool
    ended_at: UTCTimestamp | None = None

    @model_validator(mode="after")
    def validate_final_boundary(self) -> Self:
        if (
            self.input_quantity
            != self.good_quantity + self.reject_quantity + self.scrap_quantity + self.wip_quantity
        ):
            raise ValueError("input must equal good + reject + scrap + WIP")
        if (
            self.good_quantity + self.reject_quantity + self.scrap_quantity > 0
            and self.last_event_seq == 0
        ):
            raise ValueError("completed quantities require an event boundary")
        if self.final != (self.state == "ENDED"):
            raise ValueError("only ENDED summaries are final")
        if self.final and (self.wip_quantity != 0 or self.ended_at is None):
            raise ValueError("final summary requires zero WIP and ended_at")
        if not self.final and self.ended_at is not None:
            raise ValueError("non-final summary cannot carry ended_at")
        if self.ended_at is not None and self.ended_at > self.as_of:
            raise ValueError("ended_at cannot follow summary timestamp")
        return self


SCHEMA_MODELS = {
    "command-request": CommandRequest,
    "command-submission": CommandSubmission,
    "command-result-request": CommandResultRequest,
    "command-result": CommandResult,
    "command-not-found": CommandNotFound,
    "event": EVENT_ADAPTER,
    "read-events-request": ReadEventsRequest,
    "event-page": EventPage,
    "event-read-error": EventReadError,
    "run-summary-request": RunSummaryRequest,
    "run-summary": RunSummary,
    "run-not-found": RunNotFound,
}


def export_schemas() -> dict[str, dict[str, object]]:
    """Return wire schemas for checked-in export and drift checks."""
    result = {}
    for name, model in SCHEMA_MODELS.items():
        schema = (
            model.json_schema(mode="serialization")
            if isinstance(model, TypeAdapter)
            else model.model_json_schema(mode="serialization")
        )
        result[name] = {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
    return result
