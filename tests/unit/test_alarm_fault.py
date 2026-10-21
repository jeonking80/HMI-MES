"""Pinned simulator logical-Line fault/reset proof, independent of I/O."""

from datetime import timedelta

import pytest
from test_alarm_capture import AT, capture

from apps.api.alarm_capture import AlarmProcessingContext, CaptureIdentity, ClockAssessment
from apps.api.alarm_fault import (
    FaultCheckpoint,
    fault_condition,
    normalize_fault_capture,
    qualify_fault,
)
from apps.api.alarm_lifecycle import AlarmRule, AlarmScope
from apps.api.alarm_rules import PLCFaultRuleConfig
from contracts.models import FaultEvent, LineState, StateEvent
from contracts.transport import batch_digest

SCOPE = AlarmScope(line_id="line", source_id="source", stream_id="stream", asset_id="Line")
CONFIG = PLCFaultRuleConfig(rule=AlarmRule(rule_id="plc-fault", version="v1", severity="HIGH"))


def fault(seq=2, code=123, active=True):
    return FaultEvent(
        kind="FAULT_CHANGED",
        event_id=f"fault-{seq}",
        source_id="source",
        stream_id="stream",
        seq=seq,
        equipment_id="Line",
        execution_id=None,
        occurred_at=AT + timedelta(seconds=seq),
        payload={"fault_code": code, "active": active},
    )


def reset(seq=3, code=123, *, version=2):
    state = StateEvent(
        kind="STATE_CHANGED",
        event_id=f"state-{seq}",
        source_id="source",
        stream_id="stream",
        seq=seq,
        equipment_id="Line",
        execution_id=None,
        occurred_at=AT + timedelta(seconds=seq),
        payload={"previous_state": "FAULT", "state": "STOPPED", "control_version": version},
    )
    clear = fault(seq + 1, code, False).model_copy(update={"occurred_at": state.occurred_at})
    return state, clear


def observation(
    second=0,
    *,
    code=123,
    line_state=5,
    ceiling=2,
    version=2,
    events=None,
    verified=None,
    historical=False,
    proof=True,
    missing=(),
    statuses=None,
    span=0.0,
    forced=None,
):
    batch = capture(
        second,
        values={
            "Line.FaultCode": code,
            "Line.State": line_state,
            "Line.EventSeq": ceiling,
            "Line.ControlVersion": version,
        },
        missing=missing,
        statuses=statuses,
        span=span,
    )
    digest = batch_digest(batch)
    ctx = AlarmProcessingContext(
        capture=CaptureIdentity.from_batch(batch),
        batch_sha256=digest,
        scope=SCOPE,
        rule=CONFIG.rule,
        position=second + 1,
        classification="HISTORICAL" if historical else "LIVE",
        clock_assessment=ClockAssessment(
            method="CONFIGURED_API_REFERENCE",
            reference_id="same-host",
            gateway_id=batch.gateway_id,
            clock_epoch="epoch",
            batch_sha256=digest,
            offset_lower_seconds=0.0,
            offset_upper_seconds=0.0,
        )
        if proof
        else None,
    )
    return normalize_fault_capture(
        batch,
        CONFIG,
        ctx,
        events=(fault(),) if events is None else events,
        verified_through_event_seq=ceiling if verified is None else verified,
        forced_unknown_reason=forced,
    )


def active():
    return qualify_fault(FaultCheckpoint(scope=SCOPE, config=CONFIG), observation()).state


def test_immediate_global_latch_then_only_exact_applied_reset_pair_returns():
    state = active()
    assert state.latched == "ACTIVE" and state.occurrence_event_fence == 2
    no_reset = qualify_fault(state, observation(1, code=0, line_state=0))
    assert no_reset.condition == "UNKNOWN" and no_reset.state.latched == "ACTIVE"
    pair = reset()
    returned = qualify_fault(
        no_reset.state, observation(2, code=0, line_state=0, ceiling=4, events=(fault(), *pair))
    )
    assert returned.condition == "NORMAL" and returned.reason == "PLC_RESET_COMPLETED"
    evidence = fault_condition(returned).evidence
    assert {event.event_id for event in pair} <= set(evidence.references)
    assert any(reading.name == "source_event" for reading in evidence.readings)


@pytest.mark.parametrize("line_state", [5, 6])
def test_fault_code_remains_authoritative_when_estop_has_state_priority(line_state):
    result = qualify_fault(
        FaultCheckpoint(scope=SCOPE, config=CONFIG), observation(line_state=line_state)
    )
    assert result.condition == "ACTIVE"
    continued = qualify_fault(result.state, observation(1, line_state=6))
    assert continued.condition == "ACTIVE" and continued.state.occurrence_event_fence == 2


@pytest.mark.parametrize(
    "bad",
    [
        "pair-absent",
        "future",
        "nonadjacent",
        "timestamp",
        "execution",
        "version",
        "wrong-code",
        "wrong-state",
        "old-pair",
        "missing-active",
        "gap",
    ],
)
def test_zero_tag_does_not_clear_without_complete_current_reset_proof(bad):
    pair = reset()
    kwargs = dict(code=0, line_state=0, ceiling=4, events=(fault(), *pair))
    if bad == "pair-absent":
        kwargs["events"] = (fault(),)
    elif bad == "future":
        kwargs["ceiling"] = 2
    elif bad == "nonadjacent":
        kwargs["events"] = (fault(), pair[0], pair[1].model_copy(update={"seq": 5}))
        kwargs["ceiling"] = 5
    elif bad == "timestamp":
        kwargs["events"] = (fault(), pair[0], pair[1].model_copy(update={"occurred_at": AT}))
    elif bad == "execution":
        kwargs["events"] = (
            fault(),
            pair[0],
            pair[1].model_copy(update={"execution_id": "foreign"}),
        )
    elif bad == "version":
        kwargs["version"] = 1
    elif bad == "wrong-code":
        kwargs["events"] = (fault(), *reset(code=999))
    elif bad == "wrong-state":
        altered = pair[0].model_copy(
            update={"payload": pair[0].payload.model_copy(update={"state": LineState.READY})}
        )
        kwargs["events"] = (fault(), altered, pair[1])
    elif bad == "old-pair":
        kwargs["events"] = (fault(1), *reset(2))
    elif bad == "missing-active":
        kwargs["events"] = pair
    else:
        kwargs["verified"] = 3
    result = qualify_fault(active(), observation(1, **kwargs))
    assert result.condition == "UNKNOWN" and result.state.latched == "ACTIVE"


def test_fault_code_change_without_clear_and_during_unknown_does_not_duplicate_or_strand():
    state = active()
    changed = qualify_fault(state, observation(1, code=456, ceiling=5, events=(fault(5, 456),)))
    assert changed.condition == "ACTIVE" and changed.state.occurrence_event_fence == 2
    unknown = observation(
        2, code=0, line_state=0, ceiling=7, events=(fault(5, 456), *reset(6, 456))
    )
    unknown = unknown.model_copy(update={"communication_good": False})
    held = qualify_fault(state, unknown)
    assert held.state.latched == "ACTIVE" and held.condition == "UNKNOWN"
    returned = qualify_fault(
        held.state,
        observation(3, code=0, line_state=0, ceiling=7, events=(fault(5, 456), *reset(6, 456))),
    )
    assert returned.condition == "NORMAL"


@pytest.mark.parametrize(
    "missing",
    ["Line.FaultCode", "Line.State", "Line.EventSeq", "Line.ControlVersion", "Outfeed.ActualSpeed"],
)
def test_incomplete_capture_cannot_clear(missing):
    result = qualify_fault(active(), observation(1, code=0, line_state=0, missing=(missing,)))
    assert result.condition == "UNKNOWN" and result.state.latched == "ACTIVE"


@pytest.mark.parametrize("status", [0x40000000, 0x80000000])
@pytest.mark.parametrize(
    "tag", ["Line.FaultCode", "Line.State", "Line.EventSeq", "Line.ControlVersion"]
)
def test_required_bad_or_uncertain_evidence_is_unknown(tag, status):
    assert qualify_fault(active(), observation(1, statuses={tag: status})).condition == "UNKNOWN"


def test_unrelated_alias_quality_does_not_fabricate_independent_incidents():
    result = qualify_fault(
        FaultCheckpoint(scope=SCOPE, config=CONFIG),
        observation(statuses={"Sealer.FaultCode": 0x80000000}),
    )
    assert result.condition == "ACTIVE" and result.observation.scope.asset_id == "Line"


@pytest.mark.parametrize("failure", ["time", "comm", "span", "zero-fault-state"])
def test_unknown_never_false_clears(failure):
    obs = observation(
        1,
        code=0,
        line_state=0,
        ceiling=4,
        events=(fault(), *reset()),
        proof=failure != "time",
        span=3.0 if failure == "span" else 0.0,
    )
    if failure == "comm":
        obs = obs.model_copy(update={"communication_good": False})
    if failure == "zero-fault-state":
        obs = obs.model_copy(update={"line_state": 5})
    result = qualify_fault(active(), obs)
    assert result.condition == "UNKNOWN" and result.state.latched == "ACTIVE"


def test_initial_zero_no_occurrence_needs_no_prior_reset_and_history_does_not_mutate():
    state = FaultCheckpoint(scope=SCOPE, config=CONFIG)
    normal = qualify_fault(state, observation(code=0, line_state=0, events=()))
    assert normal.condition == "NORMAL"
    history = qualify_fault(normal.state, observation(1, historical=True))
    assert history.disposition == "HISTORY_ONLY" and history.state == normal.state
    duplicate = qualify_fault(normal.state, normal.observation)
    assert duplicate.disposition == "DUPLICATE"
    with pytest.raises(ValueError):
        qualify_fault(normal.state, normal.observation.model_copy(update={"fault_code": 456}))
    assert FaultCheckpoint.model_validate_json(normal.state.model_dump_json()) == normal.state


def test_real_pinned_plc_projection_keeps_global_fault_during_estop_priority():
    from apps.simulator.plc import PLCInputs, PLCState, update_inputs
    from apps.simulator.tags import project_tags

    before = PLCState(state=LineState.STOPPED, recovery_required=False)
    after = update_inputs(before, PLCInputs(fault_code=123, estop=True))
    values = project_tags(after, None, counter_epoch="epoch", heartbeat=1)
    assert values["Line.State"] == 6
    assert {values[group + ".FaultCode"] for group in ("Line", "Infeed", "Sealer", "Outfeed")} == {
        123
    }
    result = qualify_fault(
        FaultCheckpoint(scope=SCOPE, config=CONFIG),
        observation(code=values["Line.FaultCode"], line_state=values["Line.State"]),
    )
    assert result.condition == "ACTIVE"


def test_regressed_source_event_ceiling_cannot_replay_old_fault_as_current_recurrence():
    returned = qualify_fault(
        active(), observation(1, code=0, line_state=0, ceiling=4, events=(fault(), *reset()))
    )
    replayed_tags = qualify_fault(returned.state, observation(2, code=123, ceiling=2))
    assert replayed_tags.condition == "UNKNOWN"
    assert replayed_tags.reason == "SOURCE_EVENT_CEILING_REGRESSED"
    assert replayed_tags.state.latched == "NORMAL"
    assert replayed_tags.state.highest_event_ceiling == 4
    again = qualify_fault(replayed_tags.state, observation(3, code=123, ceiling=2))
    assert again.condition == "UNKNOWN" and again.state.latched == "NORMAL"
    assert again.state.highest_event_ceiling == 4


@pytest.mark.parametrize(
    "reason", ["CAPTURE_SEQUENCE_GAP", "CAPTURE_FRESHNESS_UNKNOWN", "CONFIG_INTERVAL_UNKNOWN"]
)
@pytest.mark.parametrize("returning", [False, True])
def test_forced_unknown_preserves_latch_proof_and_known_time_but_advances_floor(reason, returning):
    before = active() if returning else FaultCheckpoint(scope=SCOPE, config=CONFIG)
    obs = observation(
        1,
        ceiling=4,
        code=0 if returning else 123,
        line_state=0 if returning else 5,
        events=(fault(), *reset()) if returning else (fault(),),
        forced=reason,
    )
    result = qualify_fault(before, obs)
    assert (result.condition, result.reason, result.quality, result.time_quality) == (
        "UNKNOWN",
        reason,
        "UNKNOWN",
        "KNOWN",
    )
    assert result.state.highest_event_ceiling == 4
    for field in ("latched", "occurrence_event_fence", "last_fault_code", "last_known_at"):
        assert getattr(result.state, field) == getattr(before, field)
    assert result.observation.events == obs.events
    assert any(
        r.name == "forced_unknown_reason" and r.value == reason
        for r in fault_condition(result).evidence.readings
    )


def test_forced_history_is_byte_identical_and_bad_time_stays_unknown():
    before = active()
    history = observation(1, ceiling=100, historical=True, forced="CAPTURE_SEQUENCE_GAP")
    assert qualify_fault(before, history).state.model_dump_json() == before.model_dump_json()
    bad_time = observation(1, ceiling=4, proof=False, forced="CAPTURE_FRESHNESS_UNKNOWN")
    result = qualify_fault(before, bad_time)
    assert result.time_quality == "UNKNOWN" and result.state.highest_event_ceiling == 4


@pytest.mark.parametrize("kwargs", [{"statuses": {"Line.EventSeq": 0x80000000}}, {"verified": 2}])
def test_forced_unknown_cannot_advance_unverified_or_bad_event_floor(kwargs):
    result = qualify_fault(
        active(), observation(1, ceiling=4, forced="CAPTURE_SEQUENCE_GAP", **kwargs)
    )
    assert result.state.highest_event_ceiling == 2


def test_forced_floor_blocks_repeated_stale_fault_and_default_old_observation_decodes():
    state = qualify_fault(
        FaultCheckpoint(scope=SCOPE, config=CONFIG),
        observation(ceiling=4, forced="CAPTURE_SEQUENCE_GAP"),
    ).state
    for second in (1, 2):
        result = qualify_fault(state, observation(second, ceiling=2))
        assert result.reason == "SOURCE_EVENT_CEILING_REGRESSED"
        state = result.state
    old = observation().model_dump()
    old.pop("forced_unknown_reason")
    from apps.api.alarm_fault import FaultObservation

    assert FaultObservation.model_validate(old).forced_unknown_reason is None
