"""Pure analog qualification and composition with the existing alarm lifecycle."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from apps.api.alarm_lifecycle import AlarmRule, AlarmScope, AlarmState, ChangeAlarmRule, apply_alarm
from apps.api.alarm_qualification import (
    PressureRuleConfig,
    QualificationConflict,
    QualificationState,
    RuleObservation,
    TemperatureRuleConfig,
    as_qualified_condition,
    qualify,
    qualify_and_apply,
    replace_rule_config,
)
from contracts.transport import TagSample

AT = datetime(2026, 10, 21, tzinfo=UTC)
SCOPE = AlarmScope(line_id="line-1", source_id="source-1", stream_id="stream-1", asset_id="Sealer")


def rule(version="v1", identity="temperature-deviation"):
    return AlarmRule(rule_id=identity, version=version, severity="WARNING")


def temperature():
    return QualificationState(
        scope=SCOPE, config=TemperatureRuleConfig(rule=rule(), setpoint_c=150.0)
    )


def pressure():
    return QualificationState(
        scope=SCOPE, config=PressureRuleConfig(rule=rule(identity="pressure-low"))
    )


def sample(tag, value, seconds, *, status=0, acquired=None):
    return TagSample(
        sample_id=f"{tag}-{seconds}",
        tag_id=tag,
        acquired_at=acquired or AT + timedelta(seconds=seconds),
        source_timestamp=AT - timedelta(days=30),
        server_timestamp=None,
        status_code=status,
        reading={"data_type": "Double", "value": value},
    )


def observation(
    seconds, value=160.0, *, state=None, phase="PRODUCTION", status=0, setpoint=150.0, **changes
):
    state = state or temperature()
    is_temperature = isinstance(state.config, TemperatureRuleConfig)
    arguments = dict(
        observation_id=f"observation-{seconds}",
        scope=SCOPE,
        rule_version=state.config.rule.version,
        position=int(seconds) + 1,
        observed_at=AT + timedelta(seconds=seconds),
        clock_epoch="clock-1",
        monotonic_seconds=float(seconds),
        time_quality="KNOWN",
        phase=phase,
        phase_references=(f"phase-{seconds}",),
        actual=sample(
            "Sealer.TemperaturePV" if is_temperature else "Sealer.AirPressure",
            value,
            seconds,
            status=status,
        ),
        setpoint=(
            setpoint
            if isinstance(setpoint, TagSample)
            else sample("Sealer.TemperatureSP", setpoint, seconds)
        )
        if is_temperature
        else None,
    )
    arguments.update(changes)
    return RuleObservation(**arguments)


def points(state, start, stop, value, **kwargs):
    result = None
    for seconds in range(start, stop + 1):
        result = qualify(state, observation(seconds, value, state=state, **kwargs))
        state = result.state
    return result


@pytest.mark.parametrize("value", [160.0, 140.0])
def test_temperature_inclusive_activation_and_return_boundaries(value):
    pending = points(temperature(), 0, 4, value)
    assert pending.condition == "UNKNOWN"
    active = qualify(pending.state, observation(5, value))
    assert active.condition == "ACTIVE"
    band = points(active.state, 6, 10, 158.0)
    assert band.condition == "ACTIVE"
    returning = points(band.state, 11, 15, 157.0)
    assert returning.condition == "UNKNOWN" and returning.state.latched == "ACTIVE"
    assert qualify(returning.state, observation(16, 157.0)).condition == "NORMAL"
    assert points(temperature(), 0, 8, 159.999).state.latched is None


def test_pressure_frozen_plc_baseline_strict_low_and_inclusive_return():
    state = pressure()
    assert (state.config.low_bar, state.config.return_bar) == (4.0, 4.5)
    assert points(state, 0, 4, 4.0).state.latched is None
    active = points(state, 0, 3, 3.999)
    assert active.condition == "ACTIVE"
    band = points(active.state, 4, 8, 4.499)
    assert band.condition == "ACTIVE"
    assert points(band.state, 9, 11, 4.5).condition == "UNKNOWN"
    assert points(band.state, 9, 12, 4.5).condition == "NORMAL"


@pytest.mark.parametrize("phase", ["WARMUP", "PAUSED", "OTHER"])
def test_phase_blocks_new_occurrence_but_fresh_normal_can_return_active(phase):
    assert points(temperature(), 0, 8, 165.0, phase=phase).state.latched is None
    active = points(temperature(), 0, 5, 165.0)
    unchanged = qualify(active.state, observation(6, 165.0, phase=phase))
    assert unchanged.condition == "ACTIVE"
    assert points(unchanged.state, 7, 12, 150.0, phase=phase).condition == "NORMAL"


@pytest.mark.parametrize(
    "unknown", ["bad", "uncertain", "phase", "time", "missing", "mixed_capture"]
)
def test_unknown_resets_activation_dwell_and_never_clears_active(unknown):
    state = points(temperature(), 0, 4, 165.0).state
    if unknown in {"bad", "uncertain", "missing"}:
        status = 0x80000000 if unknown != "uncertain" else 0x40000000
        obs = observation(5, None if unknown == "missing" else 165.0, status=status)
    elif unknown == "phase":
        obs = observation(5, phase="UNKNOWN")
    elif unknown == "time":
        obs = observation(5, time_quality="UNKNOWN")
    else:
        obs = observation(5, actual=sample("Sealer.TemperaturePV", 165.0, 5, acquired=AT))
    reset = qualify(state, obs)
    assert reset.condition == "UNKNOWN" and reset.state.candidate is None
    assert points(reset.state, 6, 10, 165.0).condition == "UNKNOWN"
    assert points(reset.state, 6, 11, 165.0).condition == "ACTIVE"
    active = points(temperature(), 0, 5, 165.0).state
    unknown_active = qualify(
        active, obs.model_copy(update={"position": 7, "observation_id": "unknown-active"})
    )
    assert unknown_active.condition == "UNKNOWN" and unknown_active.state.latched == "ACTIVE"


@pytest.mark.parametrize("seconds", [3, 10])
def test_gap_restarts_dwell_without_adding_unobserved_duration(seconds):
    before = qualify(temperature(), observation(0)).state
    gap = qualify(before, observation(seconds))
    assert gap.state.candidate_since == seconds and gap.condition == "UNKNOWN"
    assert points(gap.state, seconds + 1, seconds + 4, 160.0).condition == "UNKNOWN"
    assert points(gap.state, seconds + 1, seconds + 5, 160.0).condition == "ACTIVE"


def test_epoch_change_and_monotonic_regression_reset_dwell():
    before = points(temperature(), 0, 4, 165.0).state
    changed = qualify(before, observation(5, clock_epoch="clock-2", monotonic_seconds=0.0))
    assert changed.condition == "UNKNOWN" and changed.state.candidate_since == 0.0
    reverse = qualify(before, observation(5, monotonic_seconds=3.0))
    assert reverse.condition == "UNKNOWN" and reverse.time_quality == "UNKNOWN"
    assert reverse.state.candidate is None


def test_utc_reverse_and_more_than_two_seconds_drift_are_unknown():
    before = points(temperature(), 0, 4, 165.0).state
    for observed in (AT + timedelta(seconds=3), AT + timedelta(seconds=7.001)):
        result = qualify(before, observation(5, observed_at=observed))
        assert result.condition == "UNKNOWN" and result.time_quality == "UNKNOWN"
    boundary = observation(
        5,
        observed_at=AT + timedelta(seconds=7),
        actual=sample("Sealer.TemperaturePV", 165.0, 7),
        setpoint=sample("Sealer.TemperatureSP", 150.0, 7),
    )
    assert qualify(before, boundary).condition == "ACTIVE"
    static = qualify(temperature(), observation(0)).state
    for seconds in range(1, 6):
        result = qualify(
            static,
            observation(
                seconds,
                observed_at=AT,
                actual=sample("Sealer.TemperaturePV", 165.0, 0),
                setpoint=sample("Sealer.TemperatureSP", 150.0, 0),
            ),
        )
        static = result.state
    assert static.latched is None


def test_duplicate_and_historical_observations_cannot_advance_current_dwell():
    before = points(temperature(), 0, 4, 165.0)
    duplicate = qualify(before.state, before.observation)
    assert duplicate.state == before.state and duplicate.disposition == "DUPLICATE"
    assert as_qualified_condition(duplicate) == as_qualified_condition(before)
    late = qualify(before.state, observation(3, value=150.0))
    assert late.disposition == "HISTORY_ONLY" and late.state == before.state
    assert qualify(before.state, observation(100, historical=True)).state == before.state
    with pytest.raises(QualificationConflict):
        qualify(before.state, observation(4, value=150.0))
    with pytest.raises(QualificationConflict):
        qualify(before.state, observation(5, scope=SCOPE.model_copy(update={"stream_id": "other"})))


def test_setting_change_requires_new_version_and_closes_lifecycle_as_configuration():
    state = temperature()
    alarm = AlarmState(scope=SCOPE, rules=(state.config.rule,))
    for seconds in range(6):
        result, transition = qualify_and_apply(
            alarm, state, observation(seconds), new_alarm_id="alarm-1"
        )
        state, alarm = result.state, transition.state
    assert alarm.active is not None
    mismatch = qualify(state, observation(6, value=160.0, setpoint=160.0))
    assert mismatch.condition == "UNKNOWN" and mismatch.reason == "SETPOINT_VERSION_MISMATCH"
    config = TemperatureRuleConfig(rule=rule("v2"), setpoint_c=160.0)
    state = replace_rule_config(state, config)
    alarm = apply_alarm(
        alarm,
        ChangeAlarmRule(
            event_id="change-rule",
            scope=SCOPE,
            expected_version="v1",
            new_rule=config.rule,
            actor="engineer",
            at=AT + timedelta(seconds=6),
        ),
    ).state
    assert alarm.instances[0].closure_reason == "CONFIG_CHANGED"
    assert state.candidate is None and state.latched is None
    pending = qualify(state, observation(7, value=170.0, setpoint=160.0, state=state))
    assert pending.condition == "UNKNOWN" and pending.state.candidate_since == 7
    with pytest.raises(QualificationConflict, match="immutable"):
        replace_rule_config(state, config.model_copy(update={"setpoint_c": 170.0}))
    with pytest.raises(QualificationConflict, match="currently installed"):
        qualify(state, observation(7))


def test_adapter_preserves_active_and_replays_old_input_idempotently():
    state = temperature()
    alarm = AlarmState(scope=SCOPE, rules=(state.config.rule,))
    inputs = []
    for seconds in range(6):
        obs = observation(seconds)
        inputs.append(obs)
        result, transition = qualify_and_apply(alarm, state, obs, new_alarm_id="alarm-1")
        state, alarm = result.state, transition.state
    assert transition.notify_current
    result, transition = qualify_and_apply(alarm, state, observation(6, 150.0, status=0x80000000))
    state, alarm = result.state, transition.state
    assert alarm.active.alarm_id == "alarm-1"
    replay, repeated = qualify_and_apply(alarm, state, inputs[0])
    assert replay.state == state and repeated.state == alarm
    with pytest.raises(QualificationConflict):
        qualify_and_apply(alarm, state, inputs[0].model_copy(update={"clock_epoch": "changed"}))
    assert QualificationState.model_validate_json(state.model_dump_json()) == state
    condition = as_qualified_condition(result)
    assert condition.evidence.references == (
        "phase-6",
        "Sealer.TemperaturePV-6",
        "Sealer.TemperatureSP-6",
    )
    assert any(
        reading.name.endswith("source_timestamp") and str(reading.value).startswith("2026-09")
        for reading in condition.evidence.readings
    )


def test_unknown_during_return_requires_new_five_second_normal_dwell():
    state = temperature()
    alarm = AlarmState(scope=SCOPE, rules=(state.config.rule,))
    for seconds in range(17):
        value = 165.0 if seconds <= 5 else 150.0
        status = 0x80000000 if seconds == 10 else 0
        result, transition = qualify_and_apply(
            alarm,
            state,
            observation(seconds, value, status=status),
            new_alarm_id="alarm-1" if seconds <= 5 else None,
        )
        state, alarm = result.state, transition.state
        if 5 <= seconds < 16:
            assert alarm.active is not None
    assert alarm.active is None
    assert alarm.instances[0].condition_closed_at == AT + timedelta(seconds=16)


def test_invalid_time_duplicate_retains_exact_normalized_evidence():
    before = points(temperature(), 0, 4, 165.0).state
    invalid = qualify(before, observation(5, monotonic_seconds=3.0))
    duplicate = qualify(invalid.state, invalid.observation)
    assert as_qualified_condition(duplicate) == as_qualified_condition(invalid)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "150"])
def test_nonfinite_or_coerced_numbers_are_rejected(value):
    with pytest.raises(ValidationError):
        TemperatureRuleConfig(rule=rule(), setpoint_c=value)
    with pytest.raises(ValidationError):
        sample("Sealer.TemperaturePV", value, 0)


@pytest.mark.parametrize(
    "config",
    [
        {"activation_delta_c": 7.0, "return_delta_c": 7.0},
        {"activation_seconds": 0},
        {"return_seconds": -1.0},
        {"max_gap_seconds": 4.0},
    ],
)
def test_rule_duration_and_hysteresis_bounds_are_strict(config):
    with pytest.raises(ValidationError):
        TemperatureRuleConfig(rule=rule(), setpoint_c=150.0, **config)
    with pytest.raises(ValidationError):
        PressureRuleConfig(rule=rule(identity="pressure-low"), low_bar=4.5, return_bar=4.5)
