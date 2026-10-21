"""Bounded gateway receipt and screen synchronization DTOs; no runtime or storage."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    model_serializer,
    model_validator,
)

from contracts.models import (
    ContractModel,
    Event,
    Identifier,
    LineState,
    PositiveUInt64,
    Reason,
    StrictBool,
    UInt32,
    UInt64,
    UTCTimestamp,
)
from contracts.tags.validate import load_contract

_DICTIONARY = load_contract()
_TAGS = {tag["logical_name"]: tag for tag in _DICTIONARY["tags"]}
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]


class BooleanValue(ContractModel):
    data_type: Literal["Boolean"]
    value: StrictBool | None


class Int32Value(ContractModel):
    data_type: Literal["Int32"]
    value: Annotated[int, Field(strict=True, ge=-(2**31), le=2**31 - 1)] | None


class UInt32Value(ContractModel):
    data_type: Literal["UInt32"]
    value: UInt32 | None


class UInt64Value(ContractModel):
    data_type: Literal["UInt64"]
    value: UInt64 | None


class DoubleValue(ContractModel):
    data_type: Literal["Double"]
    value: Annotated[float, Field(strict=True)] | None


class StringValue(ContractModel):
    data_type: Literal["String"]
    value: Annotated[str, Field(max_length=128)] | None


TagValue = Annotated[
    BooleanValue | Int32Value | UInt32Value | UInt64Value | DoubleValue | StringValue,
    Field(discriminator="data_type"),
]


class TagSample(ContractModel):
    sample_id: Identifier
    tag_id: Identifier
    source_timestamp: UTCTimestamp | None
    server_timestamp: UTCTimestamp | None
    acquired_at: UTCTimestamp
    status_code: UInt32
    reading: TagValue

    @model_validator(mode="after")
    def validate_dictionary_and_quality(self) -> Self:
        tag = _TAGS.get(self.tag_id)
        if tag is None or tag["data_type"] != self.reading.data_type:
            raise ValueError("tag must exist in v1 dictionary with its declared OPC data type")
        if self.reading.value is None and self.status_code >> 30 == 0:
            raise ValueError("Good status cannot substitute a missing value")
        if tag["enum"] is not None and self.reading.value is not None:
            if str(self.reading.value) not in _DICTIONARY["enums"][tag["enum"]]:
                raise ValueError("unknown PLC enum value")
        return self


class StreamGap(ContractModel):
    from_seq: PositiveUInt64
    through_seq: PositiveUInt64
    irrecoverable: StrictBool
    detected_at: UTCTimestamp
    reason_code: Identifier
    reason: Reason
    affected_execution_ids: Annotated[tuple[Identifier, ...], Field(max_length=1000)] = ()

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if self.from_seq > self.through_seq:
            raise ValueError("gap interval must be ordered and inclusive")
        if len(set(self.affected_execution_ids)) != len(self.affected_execution_ids):
            raise ValueError("affected executions must be unique")
        return self


AgeSeconds = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class HeartbeatAges(ContractModel):
    model_config = {"serialize_by_alias": True}

    line: AgeSeconds | None = Field(alias="Line.Heartbeat")
    infeed: AgeSeconds | None = Field(alias="Infeed.LinkHeartbeat")
    sealer: AgeSeconds | None = Field(alias="Sealer.LinkHeartbeat")
    outfeed: AgeSeconds | None = Field(alias="Outfeed.LinkHeartbeat")


class GatewayCommunication(ContractModel):
    """Gateway monotonic ages at capture, independent of source/wall timestamps."""

    state: Literal["UNKNOWN", "GOOD", "DELAYED", "ALARM"]
    connected: StrictBool
    read_age_seconds: AgeSeconds | None
    heartbeat_ages_seconds: HeartbeatAges

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        ages = (self.read_age_seconds, *self.heartbeat_ages_seconds.model_dump().values())
        oldest = max((age for age in ages if age is not None), default=0.0)
        # The OPC client may still be waiting for its first complete evidence even
        # with finite fallback ages since startup. UNKNOWN never asserts completeness.
        if self.state == "UNKNOWN" and oldest < 3.0:
            return self
        if oldest >= 5.0:
            expected = "ALARM"
        elif oldest >= 3.0:
            expected = "DELAYED"
        elif not self.connected or any(age is None for age in ages):
            expected = "UNKNOWN"
        else:
            expected = "GOOD"
        if self.state != expected:
            raise ValueError("communication state contradicts connection/monotonic age evidence")
        return self


class CaptureTime(ContractModel):
    """Raw capture clock coordinates; these do not assert UTC synchronization."""

    clock_epoch: Identifier
    monotonic_seconds: AgeSeconds
    utc_read_span_seconds: AgeSeconds

    @model_validator(mode="after")
    def ordered_bracket(self) -> Self:
        if self.utc_read_span_seconds > self.monotonic_seconds:
            raise ValueError("UTC read bracket must start at a nonnegative monotonic coordinate")
        return self


def _batch_schema(schema: dict) -> None:
    # The wire schema also rejects adding the new field to old signed/hash-bound bodies.
    schema["allOf"] = [
        {
            "if": {"properties": {"schema_version": {"const": "1.0"}}},
            "then": {"not": {"required": ["communication"]}},
        },
        {
            "if": {
                "properties": {
                    "schema_version": {"enum": ["1.1", "1.2"]},
                    "samples": {"minItems": 1},
                },
                "required": ["schema_version", "samples"],
            },
            "then": {
                "required": ["communication"],
                "properties": {"communication": {"not": {"type": "null"}}},
            },
        },
        {
            "if": {"properties": {"schema_version": {"enum": ["1.0", "1.1"]}}},
            "then": {"not": {"required": ["capture_time"]}},
        },
        {
            "if": {
                "properties": {"schema_version": {"const": "1.2"}, "samples": {"minItems": 1}},
                "required": ["schema_version", "samples"],
            },
            "then": {
                "required": ["capture_time"],
                "properties": {"capture_time": {"not": {"type": "null"}}},
            },
        },
    ]


class BatchBody(ContractModel):
    model_config = {"json_schema_extra": _batch_schema}

    schema_version: Literal["1.0", "1.1", "1.2"] = "1.0"
    batch_id: Identifier
    gateway_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    capture_generation: UInt64
    capture_seq: PositiveUInt64
    captured_at: UTCTimestamp
    samples: Annotated[tuple[TagSample, ...], Field(max_length=48)] = ()
    events: Annotated[tuple[Event, ...], Field(max_length=1000)] = ()
    gaps: Annotated[tuple[StreamGap, ...], Field(max_length=100)] = ()
    communication: GatewayCommunication | None = None
    capture_time: CaptureTime | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_body(self, handler: SerializerFunctionWrapHandler):
        value = handler(self)
        if self.schema_version == "1.0":
            value.pop("communication", None)
        if self.schema_version != "1.2":
            value.pop("capture_time", None)
        return value

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if self.schema_version == "1.0" and "communication" in self.model_fields_set:
            raise ValueError("schema 1.0 forbids communication evidence")
        if self.schema_version != "1.2" and "capture_time" in self.model_fields_set:
            raise ValueError("schemas 1.0/1.1 forbid capture time evidence")
        if self.schema_version == "1.2" and self.samples and self.capture_time is None:
            raise ValueError("schema 1.2 sample batches require capture time evidence")
        if self.schema_version in {"1.1", "1.2"} and self.samples and self.communication is None:
            raise ValueError("schemas 1.1/1.2 sample batches require communication evidence")
        if self.schema_version in {"1.1", "1.2"} and any(
            sample.acquired_at != self.captured_at for sample in self.samples
        ):
            raise ValueError("schemas 1.1/1.2 sample acquired_at must equal batch captured_at")
        if not (self.samples or self.events or self.gaps):
            raise ValueError("empty batches are not ingest operations")
        if len({sample.sample_id for sample in self.samples}) != len(self.samples):
            raise ValueError("duplicate sample ID")
        if len({sample.tag_id for sample in self.samples}) != len(self.samples):
            raise ValueError("at most one observation per tag in one capture")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("duplicate event ID")
        previous = 0
        for event in self.events:
            if (event.source_id, event.stream_id) != (self.source_id, self.stream_id):
                raise ValueError("event belongs to another source/stream")
            if event.seq <= previous:
                raise ValueError("events must be strictly ordered by sequence")
            previous = event.seq
        previous = 0
        for gap in self.gaps:
            if gap.from_seq <= previous:
                raise ValueError("gaps must be ordered without overlap")
            if any(gap.from_seq <= event.seq <= gap.through_seq for event in self.events):
                raise ValueError("a present event cannot also be declared missing")
            previous = gap.through_seq
        return self


def canonical_batch_body(body: BatchBody) -> str:
    """Hash every normalized body field, including all IDs; array order is significant."""
    return json.dumps(
        body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def batch_digest(body: BatchBody) -> str:
    return hashlib.sha256(canonical_batch_body(body).encode("utf-8")).hexdigest()


class GatewayBatch(ContractModel):
    body: BatchBody
    content_sha256: Digest

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        if self.content_sha256 != batch_digest(self.body):
            raise ValueError("batch content digest mismatch")
        return self


class EventReceipt(ContractModel):
    event_id: Identifier
    seq: PositiveUInt64
    disposition: Literal["APPLIED", "QUARANTINED"]
    reason_code: Identifier | None = None
    reason: Reason | None = None

    @model_validator(mode="after")
    def quarantine_reason(self) -> Self:
        if self.disposition == "QUARANTINED" and (self.reason_code is None or self.reason is None):
            raise ValueError("quarantine requires an explicit reason")
        if self.disposition == "APPLIED" and (
            self.reason_code is not None or self.reason is not None
        ):
            raise ValueError("applied receipt cannot carry a quarantine reason")
        return self


class StreamProgress(ContractModel):
    source_id: Identifier
    stream_id: Identifier
    verified_through_seq: UInt64
    latest_ingested_seq: UInt64
    unresolved_gaps: Annotated[tuple[StreamGap, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def continuous_frontier(self) -> Self:
        if self.verified_through_seq > self.latest_ingested_seq:
            raise ValueError("verified frontier cannot exceed ingestion position")
        previous = self.verified_through_seq
        for gap in self.unresolved_gaps:
            if gap.from_seq <= previous or gap.through_seq > self.latest_ingested_seq:
                raise ValueError(
                    "unresolved gaps must follow verified frontier within ingested range"
                )
            previous = gap.through_seq
        return self


class DurableAck(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    batch_id: Identifier
    gateway_id: Identifier
    content_sha256: Digest
    durability: Literal["COMMITTED"]
    committed_at: UTCTimestamp
    events: Annotated[tuple[EventReceipt, ...], Field(max_length=1000)] = ()
    recorded_gaps: Annotated[tuple[StreamGap, ...], Field(max_length=100)] = ()
    progress: StreamProgress

    @model_validator(mode="after")
    def receipts_consistent_with_progress(self) -> Self:
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("duplicate event receipt ID")
        if len({event.seq for event in self.events}) != len(self.events):
            raise ValueError("duplicate event receipt sequence")
        for event in self.events:
            if event.seq > self.progress.latest_ingested_seq:
                raise ValueError("receipt cannot exceed ingestion position")
            if (
                event.disposition == "QUARANTINED"
                and event.seq <= self.progress.verified_through_seq
            ):
                raise ValueError("quarantined event cannot lie within verified frontier")
            if any(
                gap.from_seq <= event.seq <= gap.through_seq
                for gap in self.progress.unresolved_gaps
            ):
                raise ValueError("received event cannot be inside a missing interval")
        return self


def validate_ack(batch: GatewayBatch, ack: DurableAck) -> None:
    """Bind a committed receipt to the exact immutable batch before releasing its buffer."""
    body = batch.body
    if (ack.batch_id, ack.gateway_id, ack.content_sha256) != (
        body.batch_id,
        body.gateway_id,
        batch.content_sha256,
    ):
        raise ValueError("ACK identity/hash does not match batch")
    if (ack.progress.source_id, ack.progress.stream_id) != (body.source_id, body.stream_id):
        raise ValueError("ACK progress belongs to another source/stream")
    if {(event.event_id, event.seq) for event in ack.events} != {
        (event.event_id, event.seq) for event in body.events
    }:
        raise ValueError("ACK must report every batch event exactly once")
    for gap in body.gaps:
        if gap not in ack.recorded_gaps:
            raise ValueError("ACK must retain each submitted missing interval")
    if len(ack.recorded_gaps) != len(body.gaps):
        raise ValueError("ACK gap receipts must match this batch")


class LineView(ContractModel):
    line_id: Identifier
    state: LineState | Literal["UNKNOWN"]
    execution_id: Identifier | None
    control_restricted: StrictBool
    samples: Annotated[tuple[TagSample, ...], Field(max_length=48)] = ()

    @model_validator(mode="after")
    def unique_tags(self) -> Self:
        if len({sample.tag_id for sample in self.samples}) != len(self.samples):
            raise ValueError("screen view must have one current observation per tag")
        if self.state == "UNKNOWN" and not self.control_restricted:
            raise ValueError("unknown physical state requires restricted control")
        return self


class ScreenSnapshot(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["SNAPSHOT"]
    sync_id: Identifier
    revision: UInt64
    emitted_at: UTCTimestamp
    view: LineView


class ScreenUpdate(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["UPDATE"]
    sync_id: Identifier
    base_revision: UInt64
    revision: PositiveUInt64
    emitted_at: UTCTimestamp
    view: LineView

    @model_validator(mode="after")
    def next_revision(self) -> Self:
        if self.revision != self.base_revision + 1:
            raise ValueError("update revision must immediately follow base_revision")
        return self


class ScreenResyncRequired(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["RESYNC_REQUIRED"]
    sync_id: Identifier
    reason: Literal["REVISION_GAP", "RECONNECT", "BACKPRESSURE", "SERVER_RESTART"]
    emitted_at: UTCTimestamp


ScreenMessage = Annotated[
    ScreenSnapshot | ScreenUpdate | ScreenResyncRequired, Field(discriminator="kind")
]
SCREEN_ADAPTER = TypeAdapter(ScreenMessage)


def validate_screen_update(previous: ScreenSnapshot | ScreenUpdate, update: ScreenUpdate) -> None:
    if (previous.sync_id, previous.revision, previous.view.line_id) != (
        update.sync_id,
        update.base_revision,
        update.view.line_id,
    ):
        raise ValueError("screen synchronization gap: obtain a fresh snapshot")


TRANSPORT_MODELS = {
    "transport-batch": GatewayBatch,
    "transport-ack": DurableAck,
    "transport-screen": SCREEN_ADAPTER,
}


def export_transport_schemas() -> dict[str, dict[str, object]]:
    result = {}
    for name, model in TRANSPORT_MODELS.items():
        schema = (
            model.json_schema(mode="serialization")
            if isinstance(model, TypeAdapter)
            else model.model_json_schema(mode="serialization")
        )
        result[name] = {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
    return result
