"""Pure logical-Line fault qualification for the pinned simulator event invariant.

The four projected asset FaultCode tags alias one global latch. This module does
not assert independent equipment faults or compatibility with arbitrary PLCs.
"""

from typing import Annotated, Literal

from pydantic import Field

from apps.api.alarm_capture import (
    AlarmProcessingContext,
    CaptureIdentity,
    CaptureNormalizationConflict,
    UnsupportedAlarmCapture,
    _digest,
)
from apps.api.alarm_lifecycle import AlarmEvidence, AlarmReading, AlarmScope, QualifiedCondition
from apps.api.alarm_qualification import Nonnegative
from apps.api.alarm_rules import PLCFaultRuleConfig, require_rule_target
from contracts.models import (
    ContractModel,
    FaultEvent,
    Identifier,
    StateEvent,
    StrictBool,
    UInt32,
    UInt64,
    UTCTimestamp,
)
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, Digest, TagSample, batch_digest

_TAGS = frozenset(tag["logical_name"] for tag in load_contract()["tags"])
FaultUnknownReason = Literal[
    "CAPTURE_SEQUENCE_GAP", "CAPTURE_FRESHNESS_UNKNOWN", "CONFIG_INTERVAL_UNKNOWN"
]

_INPUTS = ("Line.FaultCode", "Line.State", "Line.EventSeq", "Line.ControlVersion")


class FaultObservation(ContractModel):
    observation_id: Identifier
    scope: AlarmScope
    rule_version: Identifier
    position: UInt64
    observed_at: UTCTimestamp
    clock_epoch: Identifier
    monotonic_seconds: Nonnegative
    monotonic_span_seconds: Nonnegative
    time_quality: Literal["KNOWN", "UNKNOWN"]
    quality: Literal["KNOWN", "UNKNOWN"]
    communication_good: StrictBool
    samples: Annotated[tuple[TagSample, ...], Field(max_length=4)]
    batch_id: Identifier
    batch_sha256: Digest
    clock_assessment_sha256: Digest | None
    fault_code: UInt32 | None
    line_state: Annotated[int, Field(strict=True, ge=0, le=7)] | None
    control_version: UInt64 | None
    event_ceiling: UInt64 | None
    verified_through_event_seq: UInt64
    # Latest fault event, its preceding state event, and previous fault event
    # for matching latch provenance. Originals come from applied immutable DB evidence.
    events: Annotated[tuple[StateEvent | FaultEvent, ...], Field(max_length=3)]
    historical: StrictBool
    forced_unknown_reason: FaultUnknownReason | None = None


class FaultCheckpoint(ContractModel):
    scope: AlarmScope
    config: PLCFaultRuleConfig
    last_observation: FaultObservation | None = None
    latched: Literal["ACTIVE", "NORMAL"] | None = None
    occurrence_event_fence: UInt64 | None = None
    highest_event_ceiling: UInt64 | None = None
    last_fault_code: UInt32 | None = None
    last_known_at: UTCTimestamp | None = None
    last_condition: Literal["ACTIVE", "NORMAL", "UNKNOWN"] = "UNKNOWN"
    last_reason: Identifier = "NO_OBSERVATION"
    last_quality: Literal["KNOWN", "UNKNOWN"] = "UNKNOWN"
    last_time_quality: Literal["KNOWN", "UNKNOWN"] = "UNKNOWN"


class FaultQualificationResult(ContractModel):
    state: FaultCheckpoint
    observation: FaultObservation
    condition: Literal["ACTIVE", "NORMAL", "UNKNOWN"]
    reason: Identifier
    quality: Literal["KNOWN", "UNKNOWN"]
    time_quality: Literal["KNOWN", "UNKNOWN"]
    disposition: Literal["LIVE", "DUPLICATE", "HISTORY_ONLY"] = "LIVE"


def normalize_fault_capture(
    batch: BatchBody,
    config: PLCFaultRuleConfig,
    context: AlarmProcessingContext,
    *,
    events: tuple[StateEvent | FaultEvent, ...] = (),
    verified_through_event_seq: int,
    forced_unknown_reason: FaultUnknownReason | None = None,
) -> FaultObservation:
    batch = BatchBody.model_validate_json(batch.model_dump_json())
    config = PLCFaultRuleConfig.model_validate_json(config.model_dump_json())
    context = AlarmProcessingContext.model_validate_json(context.model_dump_json())
    require_rule_target(context.scope, config)
    if batch.schema_version != "1.2" or batch.capture_time is None or not batch.samples:
        raise UnsupportedAlarmCapture("Line fault normalization requires a schema 1.2 capture")
    digest = batch_digest(batch)
    if (
        context.capture != CaptureIdentity.from_batch(batch)
        or context.batch_sha256 != digest
        or context.scope.source_id != batch.source_id
        or context.scope.stream_id != batch.stream_id
        or context.rule != config.rule
    ):
        raise CaptureNormalizationConflict("Fault capture scope, rule or immutable body differs")
    assessment = context.clock_assessment
    if assessment is not None and (
        assessment.batch_sha256 != digest
        or assessment.gateway_id != batch.gateway_id
        or assessment.clock_epoch != batch.capture_time.clock_epoch
    ):
        raise CaptureNormalizationConflict("Fault clock assessment does not bind original capture")
    known_time = assessment is not None and (
        -2.0 <= assessment.offset_lower_seconds <= assessment.offset_upper_seconds <= 2.0
    )
    samples = {sample.tag_id: sample for sample in batch.samples}
    relevant = tuple(samples[name] for name in _INPUTS if name in samples)
    good = {
        sample.tag_id: sample.reading.value
        for sample in relevant
        if sample.status_code >> 30 == 0
        and sample.reading.value is not None
        and sample.acquired_at == batch.captured_at
    }
    ceiling = good.get("Line.EventSeq")
    ordered = []
    previous = -1
    for event in events:
        event = type(event).model_validate_json(event.model_dump_json())
        if (
            event.source_id != batch.source_id
            or event.stream_id != batch.stream_id
            or event.equipment_id != "Line"
            or event.seq <= previous
        ):
            raise CaptureNormalizationConflict("Fault evidence requires exact ordered Line events")
        previous = event.seq
        # ReadEvents runs after tags: later events in the same gateway batch must
        # never close a condition at an earlier sampled source-event ceiling.
        if ceiling is not None and event.seq <= ceiling:
            ordered.append(event)
    identity = {
        "scope": context.scope.model_dump(mode="json"),
        "rule_id": config.rule.rule_id,
        "rule_version": config.rule.version,
        "capture": context.capture.model_dump(mode="json"),
    }
    return FaultObservation(
        observation_id="fault-observation:" + _digest(identity),
        scope=context.scope,
        rule_version=config.rule.version,
        position=context.position,
        observed_at=batch.captured_at,
        clock_epoch=batch.capture_time.clock_epoch,
        monotonic_seconds=batch.capture_time.monotonic_seconds,
        monotonic_span_seconds=batch.capture_time.utc_read_span_seconds,
        time_quality="KNOWN" if known_time else "UNKNOWN",
        quality="KNOWN" if set(samples) == _TAGS and len(good) == 4 else "UNKNOWN",
        communication_good=batch.communication is not None and batch.communication.state == "GOOD",
        samples=relevant,
        batch_id=batch.batch_id,
        batch_sha256=digest,
        clock_assessment_sha256=_digest(assessment.model_dump(mode="json")) if assessment else None,
        fault_code=good.get("Line.FaultCode"),
        line_state=good.get("Line.State"),
        control_version=good.get("Line.ControlVersion"),
        event_ceiling=ceiling,
        verified_through_event_seq=verified_through_event_seq,
        events=tuple(ordered),
        historical=context.classification == "HISTORICAL",
        forced_unknown_reason=forced_unknown_reason,
    )


def qualify_fault(
    state: FaultCheckpoint, observation: FaultObservation
) -> FaultQualificationResult:
    state = FaultCheckpoint.model_validate_json(state.model_dump_json())
    observation = FaultObservation.model_validate_json(observation.model_dump_json())
    if state.scope != observation.scope:
        raise CaptureNormalizationConflict("Fault checkpoint scope differs")
    previous = state.last_observation
    if previous is not None and observation.observation_id == previous.observation_id:
        if observation != previous:
            raise CaptureNormalizationConflict("Fault identity already has different content")
        return FaultQualificationResult(
            state=state,
            observation=observation,
            condition=state.last_condition,
            reason=state.last_reason,
            quality=state.last_quality,
            time_quality=state.last_time_quality,
            disposition="DUPLICATE",
        )
    if observation.historical or (
        previous is not None and observation.position < previous.position
    ):
        return FaultQualificationResult(
            state=state,
            observation=observation,
            condition="UNKNOWN",
            reason="HISTORY_ONLY",
            quality="UNKNOWN",
            time_quality="UNKNOWN",
            disposition="HISTORY_ONLY",
        )
    if observation.rule_version != state.config.rule.version:
        raise CaptureNormalizationConflict(
            "Live fault evidence requires the installed rule version"
        )
    if previous is not None and observation.position == previous.position:
        raise CaptureNormalizationConflict("Fault normalized position already used")

    def finish(condition, reason, *, quality="KNOWN", time_quality="KNOWN", **changes):
        highest = state.highest_event_ceiling
        if (
            observation.event_ceiling is not None
            and observation.verified_through_event_seq >= observation.event_ceiling
        ):
            highest = max(highest or 0, observation.event_ceiling)
        updated = FaultCheckpoint.model_validate(
            state.model_dump()
            | {
                "last_observation": observation,
                "highest_event_ceiling": highest,
                "last_condition": condition,
                "last_reason": reason,
                "last_quality": quality,
                "last_time_quality": time_quality,
            }
            | changes
        )
        return FaultQualificationResult(
            state=updated,
            observation=observation,
            condition=condition,
            reason=reason,
            quality=quality,
            time_quality=time_quality,
        )

    invalid_time = observation.time_quality != "KNOWN" or observation.monotonic_span_seconds >= 3.0
    invalid_time |= (
        state.last_known_at is not None and observation.observed_at < state.last_known_at
    )
    if previous is not None and previous.clock_epoch == observation.clock_epoch:
        invalid_time |= observation.monotonic_seconds <= previous.monotonic_seconds
    # A coordinator restriction never fabricates raw quality or clock evidence.
    # finish retains only independently verified source ordering, not a latch decision.
    if observation.forced_unknown_reason is not None:
        return finish(
            "UNKNOWN",
            observation.forced_unknown_reason,
            quality="UNKNOWN",
            time_quality="UNKNOWN" if invalid_time else "KNOWN",
        )
    if invalid_time:
        return finish("UNKNOWN", "TIME_UNKNOWN", time_quality="UNKNOWN")
    if not observation.communication_good:
        return finish("UNKNOWN", "COMMUNICATION_NOT_GOOD", quality="UNKNOWN")
    if observation.quality != "KNOWN" or observation.event_ceiling is None:
        return finish("UNKNOWN", "FAULT_CAPTURE_UNKNOWN", quality="UNKNOWN")
    if observation.verified_through_event_seq < observation.event_ceiling:
        return finish("UNKNOWN", "EVENT_CEILING_UNVERIFIED", quality="UNKNOWN")
    if (
        state.highest_event_ceiling is not None
        and observation.event_ceiling < state.highest_event_ceiling
    ):
        return finish("UNKNOWN", "SOURCE_EVENT_CEILING_REGRESSED", quality="UNKNOWN")
    latest = next(
        (event for event in reversed(observation.events) if isinstance(event, FaultEvent)), None
    )
    if observation.fault_code:
        if (
            latest is None
            or not latest.payload.active
            or latest.payload.fault_code != observation.fault_code
        ):
            return finish("UNKNOWN", "FAULT_EVENT_MISMATCH", quality="UNKNOWN")
        fence = (
            state.occurrence_event_fence if state.latched == "ACTIVE" else observation.event_ceiling
        )
        return finish(
            "ACTIVE",
            "FAULT_LATCHED",
            latched="ACTIVE",
            occurrence_event_fence=fence,
            last_fault_code=observation.fault_code,
            last_known_at=observation.observed_at,
        )
    if observation.line_state == 5:
        return finish("UNKNOWN", "ZERO_WHILE_FAULT", quality="UNKNOWN")
    if state.latched != "ACTIVE":
        return finish("NORMAL", "NO_FAULT", latched="NORMAL", last_known_at=observation.observed_at)
    before = observation.events[-2] if len(observation.events) >= 2 else None
    activation = observation.events[-3] if len(observation.events) == 3 else None
    proof = (
        isinstance(activation, FaultEvent)
        and activation.payload.active
        and isinstance(before, StateEvent)
        and isinstance(latest, FaultEvent)
        and activation.payload.fault_code == latest.payload.fault_code
        and not latest.payload.active
        and latest.seq == before.seq + 1
        and latest.execution_id == before.execution_id
        and latest.occurred_at == before.occurred_at
        and before.payload.previous_state == "FAULT"
        and before.payload.state == "STOPPED"
        and observation.control_version is not None
        and observation.control_version >= before.payload.control_version
        and state.occurrence_event_fence is not None
        and before.seq > state.occurrence_event_fence
    )
    if not proof:
        return finish("UNKNOWN", "RESET_PROOF_REQUIRED", quality="UNKNOWN")
    return finish(
        "NORMAL",
        "PLC_RESET_COMPLETED",
        latched="NORMAL",
        occurrence_event_fence=None,
        last_fault_code=0,
        last_known_at=observation.observed_at,
    )


def fault_condition(result: FaultQualificationResult, *, new_alarm_id=None) -> QualifiedCondition:
    observation = result.observation
    references = tuple(
        dict.fromkeys(
            (
                observation.batch_id,
                *(sample.sample_id for sample in observation.samples),
                *(event.event_id for event in observation.events),
            )
        )
    )
    readings = [
        AlarmReading(name="fault_qualification_reason", value=result.reason),
        AlarmReading(name="fault_input_sha256", value=_digest(observation.model_dump(mode="json"))),
        AlarmReading(name="batch_sha256", value=observation.batch_sha256),
    ]
    if observation.forced_unknown_reason is not None:
        readings.append(
            AlarmReading(name="forced_unknown_reason", value=observation.forced_unknown_reason)
        )
    for sample in observation.samples:
        value = sample.reading.value
        if sample.reading.data_type == "UInt64" and value is not None:
            value = str(value)
        readings.extend(
            (
                AlarmReading(name=sample.tag_id, value=value),
                AlarmReading(name=sample.tag_id + ".status_code", value=sample.status_code),
                AlarmReading(
                    name=sample.tag_id + ".acquired_at", value=sample.acquired_at.isoformat()
                ),
            )
        )
    for event in observation.events:
        readings.append(AlarmReading(name="source_event", value=event.model_dump_json()))
    return QualifiedCondition(
        event_id=observation.observation_id,
        scope=observation.scope,
        rule_version=observation.rule_version,
        position=observation.position,
        condition=result.condition,
        historical=observation.historical or result.disposition == "HISTORY_ONLY",
        new_alarm_id=new_alarm_id,
        evidence=AlarmEvidence(
            observed_at=observation.observed_at,
            references=references,
            readings=tuple(readings),
            quality=result.quality,
            time_quality=result.time_quality,
        ),
    )
