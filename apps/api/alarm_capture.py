"""Pure schema-1.2 capture normalization; trusted context comes from a later store.

No clock reads, latest-value lookups, receipt-age classification or I/O occur here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import model_validator

from apps.api.alarm_lifecycle import AlarmRule, AlarmScope
from apps.api.alarm_qualification import (
    Finite,
    RuleConfig,
    RuleObservation,
    TemperatureRuleConfig,
)
from contracts.models import ContractModel, Identifier, UInt64
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, Digest, batch_digest

_TAGS = frozenset(tag["logical_name"] for tag in load_contract()["tags"])
_PHASE = (
    "Line.State",
    "Line.OperationMode",
    "Line.ActiveExecutionId",
    "Sealer.State",
    "Sealer.RunFeedback",
)


class CaptureIdentity(ContractModel):
    gateway_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    capture_generation: UInt64
    capture_seq: UInt64
    batch_id: Identifier

    @classmethod
    def from_batch(cls, batch: BatchBody) -> Self:
        return cls(**{name: getattr(batch, name) for name in cls.model_fields})


class ClockAssessment(ContractModel):
    """Caller-trusted, capture-specific relative offset interval, including uncertainty.

    CONFIGURED_API_REFERENCE explicitly assumes the named deployment reference is
    trusted. AUTHENTICATED_PROBE requires the caller to verify the exchange, same
    epoch continuity and capture enclosure before constructing this assessment.
    Neither method asserts absolute UTC accuracy, or makes raw capture_time proof.
    """

    method: Literal["CONFIGURED_API_REFERENCE", "AUTHENTICATED_PROBE"]
    reference_id: Identifier
    gateway_id: Identifier
    clock_epoch: Identifier
    batch_sha256: Digest
    offset_lower_seconds: Finite
    offset_upper_seconds: Finite

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        if self.offset_lower_seconds > self.offset_upper_seconds:
            raise ValueError("clock assessment requires an ordered conservative interval")
        return self


class AlarmProcessingContext(ContractModel):
    """Server-owned immutable binding/order/classification, never accepted from batch JSON."""

    capture: CaptureIdentity
    batch_sha256: Digest
    scope: AlarmScope
    rule: AlarmRule
    position: UInt64
    classification: Literal["LIVE", "HISTORICAL"]
    clock_assessment: ClockAssessment | None = None


class NormalizedAlarmCapture(ContractModel):
    observation: RuleObservation
    context: AlarmProcessingContext


class CaptureNormalizationConflict(ValueError):
    pass


class UnsupportedAlarmCapture(ValueError):
    """Consumer MUST break pending dwell before progressing past an unsupported capture."""


def _digest(value: dict) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _phase(samples, captured_at):
    # Presence is a complete-capture assertion, not a quality assertion on all 48.
    if set(samples) != _TAGS:
        return "UNKNOWN"
    phase_samples = [samples[name] for name in _PHASE]
    if any(
        sample.status_code >> 30 != 0
        or sample.reading.value is None
        or sample.acquired_at != captured_at
        for sample in phase_samples
    ):
        return "UNKNOWN"
    line, mode, execution, sealer, running = [sample.reading.value for sample in phase_samples]
    valid_execution = (
        bool(execution)
        and len(execution) <= 128
        and not any(character.isspace() for character in execution)
    )
    if execution and not valid_execution:
        return "UNKNOWN"
    if line != sealer:
        return "UNKNOWN"
    if line == 2:
        return "PRODUCTION" if mode == 1 and valid_execution and running is True else "UNKNOWN"
    if running is True:
        return "UNKNOWN"
    if line == 3:
        return "PAUSED" if mode == 1 and valid_execution else "UNKNOWN"
    # STOPPED/READY/STOPPING/FAULT/ESTOP/RECOVERY are known nonproduction;
    # loaded STOPPED does not prove that physical warmup is in progress.
    return "OTHER"


def normalize_alarm_capture(
    batch: BatchBody, config: RuleConfig, context: AlarmProcessingContext
) -> NormalizedAlarmCapture:
    """Normalize one original capture; no current-state inference or caller-side mutation.

    A future durable consumer must look up stored replay before regenerating this
    context. Unsupported captures must interrupt dwell, never be silently skipped.
    """
    batch = BatchBody.model_validate(batch.model_dump(mode="python", warnings=False))
    config = type(config).model_validate(config.model_dump(mode="python", warnings=False))
    context = AlarmProcessingContext.model_validate(
        context.model_dump(mode="python", warnings=False)
    )
    if batch.schema_version != "1.2" or batch.capture_time is None or not batch.samples:
        raise UnsupportedAlarmCapture("alarm normalization requires a schema 1.2 sample capture")
    digest = batch_digest(batch)
    if (
        context.capture != CaptureIdentity.from_batch(batch)
        or context.batch_sha256 != digest
        or context.scope.source_id != batch.source_id
        or context.scope.stream_id != batch.stream_id
        or context.scope.asset_id != "Sealer"
        or context.rule != config.rule
    ):
        raise CaptureNormalizationConflict("capture, scope or immutable rule binding differs")
    assessment = context.clock_assessment
    if assessment is not None and (
        assessment.batch_sha256 != digest
        or assessment.gateway_id != batch.gateway_id
        or assessment.clock_epoch != batch.capture_time.clock_epoch
    ):
        raise CaptureNormalizationConflict("clock assessment does not bind this capture and epoch")
    known_time = assessment is not None and (
        -2.0 <= assessment.offset_lower_seconds <= assessment.offset_upper_seconds <= 2.0
    )
    samples = {sample.tag_id: sample for sample in batch.samples}
    phase_samples = tuple(samples[name] for name in _PHASE if name in samples)
    identity = {
        "scope": context.scope.model_dump(mode="json"),
        "rule_id": config.rule.rule_id,
        "rule_version": config.rule.version,
        "capture": context.capture.model_dump(mode="json"),
    }
    observation = RuleObservation(
        observation_id="alarm-observation:" + _digest(identity),
        scope=context.scope,
        rule_version=config.rule.version,
        position=context.position,
        observed_at=batch.captured_at,
        clock_epoch=batch.capture_time.clock_epoch,
        monotonic_seconds=batch.capture_time.monotonic_seconds,
        monotonic_span_seconds=batch.capture_time.utc_read_span_seconds,
        time_quality="KNOWN" if known_time else "UNKNOWN",
        phase=_phase(samples, batch.captured_at),
        phase_references=tuple(sample.sample_id for sample in phase_samples) or (batch.batch_id,),
        phase_samples=phase_samples,
        batch_sha256=digest,
        clock_assessment_sha256=_digest(assessment.model_dump(mode="json")) if assessment else None,
        actual=samples.get(
            "Sealer.TemperaturePV"
            if isinstance(config, TemperatureRuleConfig)
            else "Sealer.AirPressure"
        ),
        setpoint=samples.get("Sealer.TemperatureSP")
        if isinstance(config, TemperatureRuleConfig)
        else None,
        historical=context.classification == "HISTORICAL",
    )
    return NormalizedAlarmCapture(observation=observation, context=context)
