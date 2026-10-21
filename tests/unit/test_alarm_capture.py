"""Pure same-capture normalization, explicit trust and qualification composition."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from apps.api.alarm_capture import (
    AlarmProcessingContext,
    CaptureIdentity,
    CaptureNormalizationConflict,
    ClockAssessment,
    NormalizedAlarmCapture,
    UnsupportedAlarmCapture,
    normalize_alarm_capture,
)
from apps.api.alarm_lifecycle import AlarmRule, AlarmScope, AlarmState
from apps.api.alarm_qualification import (
    PressureRuleConfig,
    QualificationConflict,
    QualificationState,
    TemperatureRuleConfig,
    as_qualified_condition,
    qualify,
    qualify_and_apply,
)
from contracts.tags.validate import load_contract
from contracts.transport import BatchBody, TagSample, batch_digest

AT = datetime(2026, 10, 21, tzinfo=UTC)
SCOPE = AlarmScope(line_id="line", source_id="source", stream_id="stream", asset_id="Sealer")
CONFIG = TemperatureRuleConfig(
    rule=AlarmRule(rule_id="temperature-deviation", version="v1", severity="WARNING"),
    setpoint_c=150.0,
)
PHASE = (
    "Line.State",
    "Line.OperationMode",
    "Line.ActiveExecutionId",
    "Sealer.State",
    "Sealer.RunFeedback",
)


def capture(seconds=0, *, values=None, missing=(), statuses=None, version="1.2", span=0.0):
    contract = load_contract()
    overrides = {
        "Line.State": 2,
        "Line.OperationMode": 1,
        "Line.ActiveExecutionId": "execution",
        "Sealer.State": 2,
        "Sealer.RunFeedback": True,
        "Sealer.Ready": False,
        "Sealer.TemperaturePV": 165.0,
        "Sealer.TemperatureSP": 150.0,
        "Sealer.AirPressure": 3.0,
    } | (values or {})
    samples = []
    for tag in contract["tags"]:
        name, kind = tag["logical_name"], tag["data_type"]
        if name in missing:
            continue
        value = {
            "Boolean": False,
            "Int32": 0,
            "UInt32": 0,
            "UInt64": 0,
            "Double": 0.0,
            "String": "",
        }[kind]
        if tag["enum"]:
            value = int(next(iter(contract["enums"][tag["enum"]])))
        samples.append(
            TagSample(
                sample_id=f"{name}-{seconds}",
                tag_id=name,
                acquired_at=AT + timedelta(seconds=seconds),
                source_timestamp=AT - timedelta(days=30),
                server_timestamp=None,
                status_code=(statuses or {}).get(name, 0),
                reading={"data_type": kind, "value": overrides.get(name, value)},
            )
        )
    kwargs = dict(
        schema_version=version,
        batch_id=f"batch-{seconds}",
        gateway_id="gateway",
        source_id="source",
        stream_id="stream",
        capture_generation=1,
        capture_seq=seconds + 1,
        captured_at=AT + timedelta(seconds=seconds),
        samples=tuple(samples),
    )
    if version != "1.0":
        kwargs["communication"] = dict(
            state="GOOD",
            connected=True,
            read_age_seconds=0.0,
            heartbeat_ages_seconds={
                name: 0.0
                for name in (
                    "Line.Heartbeat",
                    "Infeed.LinkHeartbeat",
                    "Sealer.LinkHeartbeat",
                    "Outfeed.LinkHeartbeat",
                )
            },
        )
    if version == "1.2":
        kwargs["capture_time"] = dict(
            clock_epoch="epoch", monotonic_seconds=100.0 + seconds, utc_read_span_seconds=span
        )
    return BatchBody(**kwargs)


def context(batch, *, config=CONFIG, proof=True, **changes):
    digest = batch_digest(batch)
    assessment = (
        ClockAssessment(
            method="CONFIGURED_API_REFERENCE",
            reference_id="trusted-deployment-api",
            gateway_id=batch.gateway_id,
            clock_epoch="epoch",
            batch_sha256=digest,
            offset_lower_seconds=-0.1,
            offset_upper_seconds=0.1,
        )
        if proof
        else None
    )
    return AlarmProcessingContext(
        capture=CaptureIdentity.from_batch(batch),
        batch_sha256=digest,
        scope=SCOPE,
        rule=config.rule,
        position=1000 + batch.capture_seq,
        classification="LIVE",
        clock_assessment=assessment,
    ).model_copy(update=changes)


def normalize(batch, **kwargs):
    return normalize_alarm_capture(batch, CONFIG, context(batch, **kwargs)).observation


def test_full_capture_preserves_original_samples_phase_ids_and_provenance():
    batch = capture(
        span=0.001, statuses={"Sealer.Ready": 0x80000000, "Outfeed.ActualSpeed": 0x40000000}
    )
    result = normalize_alarm_capture(batch, CONFIG, context(batch))
    observation = result.observation
    assert observation.phase == "PRODUCTION" and observation.time_quality == "KNOWN"
    assert observation.position == 1001 and observation.position != batch.capture_seq
    assert observation.observed_at == batch.captured_at
    assert observation.monotonic_seconds == batch.capture_time.monotonic_seconds
    assert observation.monotonic_span_seconds == batch.capture_time.utc_read_span_seconds
    originals = {sample.tag_id: sample for sample in batch.samples}
    assert observation.actual == originals["Sealer.TemperaturePV"]
    assert observation.setpoint == originals["Sealer.TemperatureSP"]
    assert observation.phase_samples == tuple(originals[name] for name in PHASE)
    assert observation.phase_references == tuple(originals[name].sample_id for name in PHASE)
    assert observation.batch_sha256 == batch_digest(batch)
    assert NormalizedAlarmCapture.model_validate_json(result.model_dump_json()) == result
    qualified = as_qualified_condition(
        qualify(QualificationState(scope=SCOPE, config=CONFIG), observation)
    )
    assert set(observation.phase_references) <= set(qualified.evidence.references)
    assert any(
        reading.name == "Line.State" and reading.value == 2
        for reading in qualified.evidence.readings
    )
    assert any(
        reading.name == "batch_sha256" and reading.value == batch_digest(batch)
        for reading in qualified.evidence.readings
    )


@pytest.mark.parametrize(
    "values,expected",
    [
        ({"Line.State": 3, "Sealer.State": 3, "Sealer.RunFeedback": False}, "PAUSED"),
        ({"Line.State": 0, "Sealer.State": 0, "Sealer.RunFeedback": False}, "OTHER"),
        ({"Line.State": 1, "Sealer.State": 1, "Sealer.RunFeedback": False}, "OTHER"),
        ({"Line.OperationMode": 0}, "UNKNOWN"),
        ({"Line.ActiveExecutionId": ""}, "UNKNOWN"),
        ({"Line.ActiveExecutionId": "invalid execution"}, "UNKNOWN"),
        ({"Sealer.State": 3}, "UNKNOWN"),
        ({"Sealer.RunFeedback": False}, "UNKNOWN"),
    ],
)
def test_phase_table_never_invents_warmup(values, expected):
    assert normalize(capture(values=values)).phase == expected


@pytest.mark.parametrize("tag", PHASE)
@pytest.mark.parametrize("failure", ["missing", "bad", "uncertain", "null"])
def test_every_required_phase_input_must_be_same_capture_good_and_present(tag, failure):
    kwargs = (
        {"missing": (tag,)}
        if failure == "missing"
        else {"statuses": {tag: 0x80000000 if failure != "uncertain" else 0x40000000}}
    )
    if failure == "null":
        kwargs["values"] = {tag: None}
    assert normalize(capture(**kwargs)).phase == "UNKNOWN"


def test_missing_unrelated_tag_gates_phase_but_unrelated_bad_quality_does_not():
    assert normalize(capture(missing=("Outfeed.ActualSpeed",))).phase == "UNKNOWN"
    assert normalize(capture(statuses={"Outfeed.ActualSpeed": 0x80000000})).phase == "PRODUCTION"
    batch = capture(missing=PHASE)
    observation = normalize(batch)
    assert observation.phase_samples == () and observation.phase_references == (batch.batch_id,)


def test_mixed_acquisition_time_is_rejected_before_any_normalization():
    batch = capture()
    altered = batch.model_dump()
    altered["samples"][0]["acquired_at"] += timedelta(seconds=1)
    with pytest.raises(ValidationError):
        normalize_alarm_capture(BatchBody.model_construct(**altered), CONFIG, context(batch))


@pytest.mark.parametrize(
    "bounds,expected",
    [
        ((-2.0, 2.0), "KNOWN"),
        ((2.0, 2.0), "KNOWN"),
        ((-2.00001, 0.0), "UNKNOWN"),
        ((1.9, 2.1), "UNKNOWN"),
        ((3.0, 4.0), "UNKNOWN"),
    ],
)
def test_clock_quality_uses_entire_explicit_relative_interval(bounds, expected):
    batch = capture()
    ctx = context(batch)
    assessment = ctx.clock_assessment.model_copy(
        update=dict(zip(("offset_lower_seconds", "offset_upper_seconds"), bounds, strict=True))
    )
    ctx = ctx.model_copy(update={"clock_assessment": assessment})
    assert normalize_alarm_capture(batch, CONFIG, ctx).observation.time_quality == expected


def test_no_proof_unknown_and_fixed_source_timestamp_does_not_infer_skew_or_history():
    batch = capture()
    assert normalize(batch, proof=False).time_quality == "UNKNOWN"
    observation = normalize(batch)
    assert observation.time_quality == "KNOWN" and observation.historical is False
    assert observation.actual.source_timestamp == AT - timedelta(days=30)
    historical = normalize(batch, classification="HISTORICAL")
    assert historical.historical and historical.observation_id == observation.observation_id
    assert (
        qualify(QualificationState(scope=SCOPE, config=CONFIG), historical).disposition
        == "HISTORY_ONLY"
    )


@pytest.mark.parametrize(
    "change", ["body", "source", "asset", "rule", "capture", "epoch", "clockbody"]
)
def test_exact_scope_rule_capture_and_proof_binding(change):
    batch = capture()
    ctx = context(batch)
    if change == "body":
        ctx = ctx.model_copy(update={"batch_sha256": "0" * 64})
    elif change == "source":
        ctx = ctx.model_copy(update={"scope": SCOPE.model_copy(update={"source_id": "foreign"})})
    elif change == "asset":
        ctx = ctx.model_copy(update={"scope": SCOPE.model_copy(update={"asset_id": "Outfeed"})})
    elif change == "rule":
        ctx = ctx.model_copy(update={"rule": CONFIG.rule.model_copy(update={"version": "v2"})})
    elif change == "capture":
        ctx = ctx.model_copy(update={"capture": ctx.capture.model_copy(update={"capture_seq": 9})})
    else:
        key, value = ("clock_epoch", "foreign") if change == "epoch" else ("batch_sha256", "0" * 64)
        ctx = ctx.model_copy(
            update={"clock_assessment": ctx.clock_assessment.model_copy(update={key: value})}
        )
    with pytest.raises(CaptureNormalizationConflict):
        normalize_alarm_capture(batch, CONFIG, ctx)


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_unsupported_legacy_is_explicit_error_not_silently_skipped(version):
    batch = capture(version=version)
    with pytest.raises(UnsupportedAlarmCapture):
        normalize_alarm_capture(batch, CONFIG, context(batch))


def test_nullable_analog_and_missing_setpoint_interrupt_pending_dwell_without_fabrication():
    state = QualificationState(scope=SCOPE, config=CONFIG)
    for second in range(5):
        state = qualify(state, normalize(capture(second))).state
    assert state.candidate_since == 100.0
    for missing in (("Sealer.TemperaturePV",), ("Sealer.TemperatureSP",)):
        observation = normalize(capture(5, missing=missing))
        result = qualify(state, observation)
        assert result.condition == "UNKNOWN" and result.state.candidate is None
        assert (observation.actual is None) == (missing == ("Sealer.TemperaturePV",))
        condition = as_qualified_condition(result)
        assert condition.evidence.quality == "UNKNOWN"
        assert not any("missing" in reference for reference in condition.evidence.references)
        after = qualify(result.state, normalize(capture(6)))
        assert after.state.candidate_since == 106.0
    valid = normalize(capture(5))
    absent = valid.model_copy(update={"actual": None})
    assert qualify(state, absent).quality == "UNKNOWN"
    absent_sp = valid.model_copy(update={"setpoint": None})
    assert qualify(state, absent_sp).quality == "UNKNOWN"
    wrong = valid.model_copy(update={"actual": valid.setpoint})
    with pytest.raises(QualificationConflict):
        qualify(state, wrong)
    wrong_sp = valid.model_copy(update={"setpoint": valid.actual})
    with pytest.raises(QualificationConflict):
        qualify(state, wrong_sp)


def test_normalize_qualify_lifecycle_activation_return_and_replay_content_conflict():
    state = QualificationState(scope=SCOPE, config=CONFIG)
    alarm = AlarmState(scope=SCOPE, rules=(CONFIG.rule,))
    for second in range(6):
        result, transition = qualify_and_apply(
            alarm, state, normalize(capture(second)), new_alarm_id="alarm"
        )
        state, alarm = result.state, transition.state
    assert alarm.active is not None
    replay = normalize(capture(0))
    result, transition = qualify_and_apply(alarm, state, replay, new_alarm_id="alarm")
    assert result.disposition == "DUPLICATE" and transition.state == alarm
    altered = capture(0, values={"Sealer.Ready": True})
    assert normalize(altered).observation_id == replay.observation_id
    with pytest.raises(QualificationConflict):
        qualify_and_apply(alarm, state, normalize(altered), new_alarm_id="alarm")
    for second in range(6, 12):
        observation = normalize(
            capture(
                second,
                values={
                    "Sealer.TemperaturePV": 150.0,
                    "Line.State": 3,
                    "Sealer.State": 3,
                    "Sealer.RunFeedback": False,
                },
            )
        )
        result, transition = qualify_and_apply(alarm, state, observation)
        state, alarm = result.state, transition.state
    assert alarm.active is None and alarm.instances[0].closure_reason == "NORMAL_RETURN"


def test_pressure_adapter_has_no_setpoint_and_retains_exact_pressure_sample():
    config = PressureRuleConfig(
        rule=AlarmRule(rule_id="pressure-low", version="p1", severity="WARNING")
    )
    batch = capture()
    observation = normalize_alarm_capture(batch, config, context(batch, config=config)).observation
    assert observation.actual.tag_id == "Sealer.AirPressure" and observation.setpoint is None
    assert (
        qualify(QualificationState(scope=SCOPE, config=config), observation).reason
        == "DWELL_PENDING"
    )


def test_nonzero_capture_bracket_cannot_activate_temperature_early():
    state = QualificationState(scope=SCOPE, config=CONFIG)
    for second in range(5):
        state = qualify(state, normalize(capture(second))).state
    early = qualify(state, normalize(capture(5, span=1.0)))
    assert early.condition == "UNKNOWN" and early.state.candidate_since == 100.0
    exact = qualify(early.state, normalize(capture(6, span=1.0)))
    assert exact.condition == "ACTIVE"  # Earliest106-1 minus latest100 is exactly5.
    evidence = as_qualified_condition(exact).evidence
    assert any(
        item.name == "monotonic_span_seconds" and item.value == 1.0 for item in evidence.readings
    )


def test_nonzero_capture_bracket_cannot_return_active_alarm_early():
    state = QualificationState(scope=SCOPE, config=CONFIG)
    alarm = AlarmState(scope=SCOPE, rules=(CONFIG.rule,))
    for second in range(6):
        result, transition = qualify_and_apply(
            alarm, state, normalize(capture(second)), new_alarm_id="alarm"
        )
        state, alarm = result.state, transition.state
    assert alarm.active is not None
    for second in range(6, 11):
        observation = normalize(capture(second, values={"Sealer.TemperaturePV": 150.0}))
        result, transition = qualify_and_apply(alarm, state, observation)
        state, alarm = result.state, transition.state
    early = normalize(capture(11, span=0.25, values={"Sealer.TemperaturePV": 150.0}))
    result, transition = qualify_and_apply(alarm, state, early)
    assert result.condition == "UNKNOWN" and transition.state.active is not None
    exact = normalize(capture(12, span=1.0, values={"Sealer.TemperaturePV": 150.0}))
    result, transition = qualify_and_apply(transition.state, result.state, exact)
    assert result.condition == "NORMAL" and transition.state.active is None


@pytest.mark.parametrize("previous_span,expected", [(1.999, "ACTIVE"), (2.0, "UNKNOWN")])
def test_gap_uses_current_latest_minus_previous_earliest_at_three_second_boundary(
    previous_span, expected
):
    state = QualificationState(scope=SCOPE, config=CONFIG)
    for second in range(5):
        observation = normalize(capture(second, span=previous_span if second == 4 else 0.0))
        state = qualify(state, observation).state
    result = qualify(state, normalize(capture(5)))
    assert result.condition == expected
    if expected == "UNKNOWN":
        assert result.state.candidate_since == 105.0


@pytest.mark.parametrize("span", [3.0, 3.1])
def test_capture_bracket_at_gap_limit_is_time_unknown_and_keeps_active_latch(span):
    state = QualificationState(scope=SCOPE, config=CONFIG)
    for second in range(6):
        state = qualify(state, normalize(capture(second))).state
    assert state.latched == "ACTIVE"
    result = qualify(
        state, normalize(capture(6, span=span, values={"Sealer.TemperaturePV": 150.0}))
    )
    assert result.time_quality == "UNKNOWN" and result.condition == "UNKNOWN"
    assert result.state.latched == "ACTIVE" and result.state.candidate_since is None


def test_observation_bracket_default_preserves_compatibility_and_rejects_negative_start():
    observation = normalize(capture())
    legacy = observation.model_dump()
    del legacy["monotonic_span_seconds"]
    restored = type(observation).model_validate(legacy)
    assert restored.monotonic_span_seconds == 0.0
    with pytest.raises(ValidationError):
        type(observation).model_validate(legacy | {"monotonic_span_seconds": 101.0})
