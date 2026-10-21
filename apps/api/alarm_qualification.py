"""Pure analog alarm dwell qualification; no clock reads, IDs, storage or I/O."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from apps.api.alarm_lifecycle import (
    AlarmEvidence,
    AlarmReading,
    AlarmRule,
    AlarmScope,
    AlarmState,
    AlarmTransition,
    QualifiedCondition,
    apply_alarm,
)
from contracts.models import ContractModel, Identifier, StrictBool, UInt64, UTCTimestamp
from contracts.transport import Digest, TagSample

Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Nonnegative = Annotated[Finite, Field(ge=0)]
Positive = Annotated[Finite, Field(gt=0)]
Condition = Literal["ACTIVE", "NORMAL", "UNKNOWN"]


class _RuleConfig(ContractModel):
    rule: AlarmRule
    # The normal 1 Hz observation stream must break dwell at the 3 s delay boundary.
    max_gap_seconds: Annotated[Positive, Field(le=3)] = 3.0


class TemperatureRuleConfig(_RuleConfig):
    kind: Literal["TEMPERATURE_DEVIATION"] = "TEMPERATURE_DEVIATION"
    setpoint_c: Finite
    activation_delta_c: Positive = 10.0
    return_delta_c: Nonnegative = 7.0
    activation_seconds: Positive = 5.0
    return_seconds: Positive = 5.0

    @model_validator(mode="after")
    def hysteresis(self) -> Self:
        if self.return_delta_c >= self.activation_delta_c:
            raise ValueError("temperature return band must be inside activation band")
        return self


class PressureRuleConfig(_RuleConfig):
    kind: Literal["PRESSURE_LOW"] = "PRESSURE_LOW"
    # Explicit versioned example values from simulator PLCConfig, not a PLC import.
    low_bar: Nonnegative = 4.0
    return_bar: Positive = 4.5
    activation_seconds: Positive = 3.0
    return_seconds: Positive = 3.0

    @model_validator(mode="after")
    def hysteresis(self) -> Self:
        if self.low_bar >= self.return_bar:
            raise ValueError("pressure return threshold must exceed low threshold")
        return self


RuleConfig = Annotated[TemperatureRuleConfig | PressureRuleConfig, Field(discriminator="kind")]


class RuleObservation(ContractModel):
    """Caller-normalized live order, phase and clock proof over original tag samples.

    Monotonic seconds belong to an explicit clock epoch. A future ingestion adapter
    must establish time_quality against an explicitly trusted project API reference,
    rather than infer it from a fixed SourceTimestamp or network delivery latency.
    """

    observation_id: Identifier
    scope: AlarmScope
    rule_version: Identifier
    position: UInt64
    observed_at: UTCTimestamp
    clock_epoch: Identifier
    monotonic_seconds: Nonnegative
    monotonic_span_seconds: Nonnegative = 0.0
    time_quality: Literal["KNOWN", "UNKNOWN"]
    phase: Literal["PRODUCTION", "WARMUP", "PAUSED", "OTHER", "UNKNOWN"]
    phase_references: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    actual: TagSample | None
    phase_samples: Annotated[tuple[TagSample, ...], Field(max_length=5)] = ()
    batch_sha256: Digest | None = None
    clock_assessment_sha256: Digest | None = None
    setpoint: TagSample | None = None
    historical: StrictBool = False

    @model_validator(mode="after")
    def ordered_monotonic_bracket(self) -> Self:
        if self.monotonic_span_seconds > self.monotonic_seconds:
            raise ValueError("capture bracket must begin at a nonnegative monotonic coordinate")
        return self


class QualificationState(ContractModel):
    scope: AlarmScope
    config: RuleConfig
    last_observation: RuleObservation | None = None
    last_known_at: UTCTimestamp | None = None
    clock_anchor_at: UTCTimestamp | None = None
    clock_anchor_seconds: Nonnegative | None = None
    latched: Literal["ACTIVE", "NORMAL"] | None = None
    candidate: Literal["ACTIVE", "NORMAL"] | None = None
    candidate_since: Nonnegative | None = None
    last_condition: Condition = "UNKNOWN"
    last_reason: Identifier = "NO_OBSERVATION"
    last_quality: Literal["KNOWN", "UNKNOWN"] = "UNKNOWN"
    last_time_quality: Literal["KNOWN", "UNKNOWN"] = "UNKNOWN"

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.candidate is None) != (self.candidate_since is None):
            raise ValueError("dwell candidate requires its exact monotonic start")
        if (self.clock_anchor_at is None) != (self.clock_anchor_seconds is None):
            raise ValueError("clock anchor requires both UTC and monotonic evidence")
        if self.last_observation is not None and self.last_observation.scope != self.scope:
            raise ValueError("observation and qualification scope differ")
        return self


class QualificationResult(ContractModel):
    state: QualificationState
    observation: RuleObservation
    condition: Condition
    reason: Identifier
    quality: Literal["KNOWN", "UNKNOWN"]
    time_quality: Literal["KNOWN", "UNKNOWN"]
    disposition: Literal["LIVE", "DUPLICATE", "HISTORY_ONLY"] = "LIVE"


class QualificationConflict(ValueError):
    pass


def _replace(state: QualificationState, **changes) -> QualificationState:
    return QualificationState.model_validate({**state.model_dump(), **changes})


def replace_rule_config(state: QualificationState, config: RuleConfig) -> QualificationState:
    """Pair with lifecycle ChangeAlarmRule; a settings edit cannot be a normal return."""
    state = QualificationState.model_validate(state.model_dump(mode="python", warnings=False))
    config = type(config).model_validate(config.model_dump(mode="python", warnings=False))
    if config.rule.rule_id != state.config.rule.rule_id or type(config) is not type(state.config):
        raise QualificationConflict("replacement must keep rule identity and kind")
    if config.rule.version == state.config.rule.version:
        if config != state.config:
            raise QualificationConflict("rule configuration versions are immutable")
        return state
    return _replace(
        state,
        config=config,
        latched=None,
        candidate=None,
        candidate_since=None,
        clock_anchor_at=None,
        clock_anchor_seconds=None,
        last_condition="UNKNOWN",
        last_reason="CONFIG_CHANGED",
        last_quality="UNKNOWN",
        last_time_quality="UNKNOWN",
    )


def qualify(state: QualificationState, observation: RuleObservation) -> QualificationResult:
    """Qualify known continuous observations; UNKNOWN never clears the latched condition."""
    state = QualificationState.model_validate(state.model_dump(mode="python", warnings=False))
    observation = RuleObservation.model_validate(
        observation.model_dump(mode="python", warnings=False)
    )
    if observation.scope != state.scope:
        raise QualificationConflict("observation scope differs from rule scope")
    previous = state.last_observation
    if previous is not None and observation.observation_id == previous.observation_id:
        if observation != previous:
            raise QualificationConflict("observation identity already has different content")
        if observation.rule_version == state.config.rule.version:
            return QualificationResult(
                state=state,
                observation=observation,
                condition=state.last_condition,
                reason=state.last_reason,
                quality=state.last_quality,
                time_quality=state.last_time_quality,
                disposition="DUPLICATE",
            )
    if observation.historical or (
        previous is not None
        and (
            observation.position < previous.position
            or (
                observation.position == previous.position
                and observation.rule_version != state.config.rule.version
            )
        )
    ):
        return QualificationResult(
            state=state,
            observation=observation,
            condition="UNKNOWN",
            reason="HISTORY_ONLY",
            quality="UNKNOWN",
            time_quality="UNKNOWN",
            disposition="HISTORY_ONLY",
        )
    if observation.rule_version != state.config.rule.version:
        raise QualificationConflict("live observation requires currently installed rule version")
    if previous is not None and observation.position == previous.position:
        raise QualificationConflict("normalized observation position is already used")
    config = state.config
    actual_tag = (
        "Sealer.TemperaturePV"
        if isinstance(config, TemperatureRuleConfig)
        else "Sealer.AirPressure"
    )
    if observation.actual is not None and (
        observation.actual.tag_id != actual_tag or observation.actual.reading.data_type != "Double"
    ):
        raise QualificationConflict("rule requires its exact analog tag/type")
    if isinstance(config, TemperatureRuleConfig):
        if (
            observation.setpoint is not None
            and observation.setpoint.tag_id != "Sealer.TemperatureSP"
        ):
            raise QualificationConflict("temperature rule requires the exact setpoint tag")
    elif observation.setpoint is not None:
        raise QualificationConflict("pressure rule does not accept a temperature setpoint")

    changes = {"last_observation": observation}

    def finish(condition, reason, *, quality="KNOWN", time_quality="KNOWN", **extra):
        new_state = _replace(
            state,
            **changes,
            last_condition=condition,
            last_reason=reason,
            last_quality=quality,
            last_time_quality=time_quality,
            **extra,
        )
        return QualificationResult(
            state=new_state,
            observation=observation,
            condition=condition,
            reason=reason,
            quality=quality,
            time_quality=time_quality,
        )

    anchor_at, anchor_seconds = state.clock_anchor_at, state.clock_anchor_seconds
    reset_timeline = anchor_at is None
    invalid_time = (
        observation.time_quality == "UNKNOWN"
        or observation.monotonic_span_seconds >= config.max_gap_seconds
    ) or (state.last_known_at is not None and observation.observed_at < state.last_known_at)
    if previous is not None:
        if observation.clock_epoch != previous.clock_epoch:
            reset_timeline = True
        elif observation.monotonic_seconds <= previous.monotonic_seconds:
            invalid_time = True
        elif (
            observation.monotonic_seconds
            - (previous.monotonic_seconds - previous.monotonic_span_seconds)
            >= config.max_gap_seconds
        ):
            reset_timeline = True
    if not reset_timeline and anchor_at is not None and anchor_seconds is not None:
        drift = (observation.observed_at - anchor_at).total_seconds() - (
            observation.monotonic_seconds - anchor_seconds
        )
        invalid_time = invalid_time or abs(drift) > 2.0
    if reset_timeline or invalid_time:
        changes.update(
            clock_anchor_at=observation.observed_at,
            clock_anchor_seconds=observation.monotonic_seconds,
        )
    if invalid_time:
        return finish(
            "UNKNOWN", "TIME_UNKNOWN", time_quality="UNKNOWN", candidate=None, candidate_since=None
        )
    changes["last_known_at"] = observation.observed_at
    samples = tuple(
        sample for sample in (observation.actual, observation.setpoint) if sample is not None
    )
    if (
        observation.actual is None
        or (isinstance(config, TemperatureRuleConfig) and observation.setpoint is None)
        or observation.phase == "UNKNOWN"
    ) or any(
        sample.status_code >> 30 != 0
        or sample.reading.value is None
        or sample.acquired_at != observation.observed_at
        for sample in samples
    ):
        return finish(
            "UNKNOWN",
            "QUALITY_OR_PHASE_UNKNOWN",
            quality="UNKNOWN",
            candidate=None,
            candidate_since=None,
        )
    value = observation.actual.reading.value
    if isinstance(config, TemperatureRuleConfig):
        if observation.setpoint.reading.value != config.setpoint_c:
            return finish(
                "UNKNOWN",
                "SETPOINT_VERSION_MISMATCH",
                quality="UNKNOWN",
                candidate=None,
                candidate_since=None,
            )
        deviation = abs(value - config.setpoint_c)
        abnormal = deviation >= config.activation_delta_c
        normal = deviation <= config.return_delta_c
    else:
        abnormal, normal = value < config.low_bar, value >= config.return_bar
    if abnormal and state.latched == "ACTIVE":
        return finish("ACTIVE", "ACTIVE_CONTINUES", candidate=None, candidate_since=None)
    if abnormal and observation.phase != "PRODUCTION":
        return finish("UNKNOWN", "PRODUCTION_ONLY", candidate=None, candidate_since=None)
    target = "ACTIVE" if abnormal else "NORMAL" if normal else None
    if target is None:
        return finish(
            state.latched or "UNKNOWN", "HYSTERESIS_BAND", candidate=None, candidate_since=None
        )
    if state.latched == target:
        return finish(target, "QUALIFIED_CONTINUES", candidate=None, candidate_since=None)
    since = state.candidate_since if state.candidate == target and not reset_timeline else None
    since = observation.monotonic_seconds if since is None else since
    duration = config.activation_seconds if target == "ACTIVE" else config.return_seconds
    # Candidate starts at its latest possible coordinate; current capture may
    # have occurred at its earliest bracket coordinate. Never credit uncertainty.
    if observation.monotonic_seconds - observation.monotonic_span_seconds - since >= duration:
        return finish(
            target, "DWELL_QUALIFIED", latched=target, candidate=None, candidate_since=None
        )
    return finish("UNKNOWN", "DWELL_PENDING", candidate=target, candidate_since=since)


def _observation_digest(observation: RuleObservation) -> str:
    encoded = json.dumps(
        observation.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def as_qualified_condition(
    result: QualificationResult, *, new_alarm_id: Identifier | None = None
) -> QualifiedCondition:
    """Pure caller adapter; IDs/order/phase/time proof remain caller responsibilities."""
    observation = result.observation
    samples = tuple(
        sample for sample in (observation.actual, observation.setpoint) if sample is not None
    )
    references = tuple(
        dict.fromkeys((*observation.phase_references, *(sample.sample_id for sample in samples)))
    )
    readings = [
        AlarmReading(name="phase", value=observation.phase),
        AlarmReading(name="clock_epoch", value=observation.clock_epoch),
        AlarmReading(name="monotonic_seconds", value=observation.monotonic_seconds),
        AlarmReading(name="monotonic_span_seconds", value=observation.monotonic_span_seconds),
        AlarmReading(name="input_time_quality", value=observation.time_quality),
        AlarmReading(name="qualification_reason", value=result.reason),
        AlarmReading(name="qualification_input_sha256", value=_observation_digest(observation)),
    ]
    for name in ("batch_sha256", "clock_assessment_sha256"):
        value = getattr(observation, name)
        if value is not None:
            readings.append(AlarmReading(name=name, value=value))
    for sample in (*samples, *observation.phase_samples):
        readings.extend(
            (
                AlarmReading(name=sample.tag_id, value=sample.reading.value),
                AlarmReading(name=sample.tag_id + ".status_code", value=sample.status_code),
                AlarmReading(
                    name=sample.tag_id + ".acquired_at", value=sample.acquired_at.isoformat()
                ),
                AlarmReading(
                    name=sample.tag_id + ".source_timestamp",
                    value=sample.source_timestamp.isoformat() if sample.source_timestamp else None,
                ),
                AlarmReading(
                    name=sample.tag_id + ".server_timestamp",
                    value=sample.server_timestamp.isoformat() if sample.server_timestamp else None,
                ),
            )
        )
    return QualifiedCondition(
        event_id=observation.observation_id,
        scope=observation.scope,
        rule_version=observation.rule_version,
        position=observation.position,
        condition=result.condition,
        new_alarm_id=new_alarm_id,
        historical=observation.historical or result.disposition == "HISTORY_ONLY",
        evidence=AlarmEvidence(
            observed_at=observation.observed_at,
            references=references,
            readings=tuple(readings),
            quality=result.quality,
            time_quality=result.time_quality,
        ),
    )


def qualify_and_apply(
    alarm: AlarmState,
    state: QualificationState,
    observation: RuleObservation,
    *,
    new_alarm_id: Identifier | None = None,
) -> tuple[QualificationResult, AlarmTransition]:
    """Return both pure changes; the later durable adapter must persist them atomically."""
    if alarm.scope != state.scope or alarm.rule != state.config.rule:
        raise QualificationConflict("lifecycle and qualification scope/rule must agree")
    existing = next(
        (
            receipt.event
            for receipt in alarm.receipts
            if receipt.event.event_id == observation.observation_id
        ),
        None,
    )
    if existing is not None:
        if (
            not isinstance(existing, QualifiedCondition)
            or not any(
                reading.name == "qualification_input_sha256"
                and reading.value == _observation_digest(observation)
                for reading in existing.evidence.readings
            )
            or (new_alarm_id is not None and new_alarm_id != existing.new_alarm_id)
        ):
            raise QualificationConflict("observation identity already has different content")
        result = QualificationResult(
            state=state,
            observation=observation,
            condition=existing.condition,
            reason=next(
                reading.value
                for reading in existing.evidence.readings
                if reading.name == "qualification_reason"
            ),
            quality=existing.evidence.quality,
            time_quality=existing.evidence.time_quality,
            disposition="DUPLICATE",
        )
        return result, apply_alarm(alarm, existing)
    if alarm.last_position is not None and observation.position < alarm.last_position:
        result = QualificationResult(
            state=state,
            observation=observation,
            condition="UNKNOWN",
            reason="HISTORY_ONLY",
            quality="UNKNOWN",
            time_quality="UNKNOWN",
            disposition="HISTORY_ONLY",
        )
    else:
        result = qualify(state, observation)
    return result, apply_alarm(alarm, as_qualified_condition(result, new_alarm_id=new_alarm_id))
