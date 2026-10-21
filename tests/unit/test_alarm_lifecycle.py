"""Qualified alarm transitions, immutable evidence and exact-instance user actions."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from apps.api.alarm_lifecycle import (
    AcknowledgeAlarm,
    AddAlarmNote,
    AlarmConflict,
    AlarmEvidence,
    AlarmInstance,
    AlarmReading,
    AlarmRule,
    AlarmScope,
    AlarmState,
    ChangeAlarmRule,
    EditAlarmNote,
    QualifiedCondition,
    apply_alarm,
)
from contracts.models import UINT64_MAX

AT = datetime(2026, 10, 21, tzinfo=UTC)
SCOPE = AlarmScope(line_id="line-A", source_id="source-A", stream_id="stream-A", asset_id="Sealer")
RULE = AlarmRule(rule_id="plc-fault", version="v1", severity="WARNING")


def initial():
    return AlarmState(scope=SCOPE, rules=(RULE,))


def condition(position, value="ACTIVE", *, alarm_id="alarm-A", **changes):
    return QualifiedCondition.model_validate(
        {
            "event_id": f"condition-{position}",
            "scope": SCOPE,
            "rule_version": "v1",
            "position": position,
            "condition": value,
            "new_alarm_id": alarm_id,
            "evidence": AlarmEvidence(
                observed_at=AT + timedelta(seconds=position),
                references=(f"fault-event-{position}",),
                readings=(AlarmReading(name="FaultCode", value=1002),),
            ),
            **changes,
        }
    )


def opened():
    return apply_alarm(initial(), condition(1)).state


def acknowledge(event_id="ack-A", alarm_id="alarm-A", actor="operator-A", seconds=3):
    return AcknowledgeAlarm(
        event_id=event_id,
        scope=SCOPE,
        alarm_id=alarm_id,
        actor=actor,
        at=AT + timedelta(seconds=seconds),
    )


def replace(model, **changes):
    return type(model).model_validate({**model.model_dump(), **changes})


@pytest.mark.parametrize("ack_first", [True, False])
def test_acknowledgement_and_return_are_independent_in_both_orders(ack_first):
    state = opened()
    assert state.active.status == "ACTIVE_UNACKNOWLEDGED"
    if ack_first:
        state = apply_alarm(state, acknowledge(seconds=2)).state
        assert state.active.status == "ACTIVE_ACKNOWLEDGED"
        state = apply_alarm(state, condition(3, "NORMAL")).state
    else:
        state = apply_alarm(state, condition(2, "NORMAL")).state
        assert state.instances[0].status == "RETURNED_UNACKNOWLEDGED"
        state = apply_alarm(state, acknowledge()).state
    instance = state.instances[0]
    assert instance.status == "RETURNED_ACKNOWLEDGED"
    assert instance.acknowledged_by == "operator-A"
    assert instance.closure_reason == "NORMAL_RETURN"
    assert instance.return_evidence.references
    assert state.active is None


def test_continuing_fault_keeps_original_instance_and_exact_replay_is_noop():
    state = opened()
    repeated = condition(2, alarm_id="unused-new-id")
    transition = apply_alarm(state, repeated)
    assert transition.created_alarm_id is None
    assert not transition.notify_current
    assert transition.state.instances == state.instances
    assert transition.state.receipts[-1].disposition == "NO_CHANGE"
    assert apply_alarm(transition.state, repeated).state == transition.state
    with pytest.raises(AlarmConflict, match="different content"):
        apply_alarm(transition.state, replace(repeated, condition="NORMAL"))


def test_recurrence_preserves_old_unacknowledged_instance_and_ack_targets_exact_id():
    state = apply_alarm(opened(), condition(2, "NORMAL")).state
    transition = apply_alarm(state, condition(3, alarm_id="alarm-B"))
    state = transition.state
    assert transition.notify_current and transition.created_alarm_id == "alarm-B"
    assert [item.status for item in state.instances] == [
        "RETURNED_UNACKNOWLEDGED",
        "ACTIVE_UNACKNOWLEDGED",
    ]
    state = apply_alarm(state, acknowledge(seconds=4)).state
    assert state.instances[0].status == "RETURNED_ACKNOWLEDGED"
    assert state.active.alarm_id == "alarm-B"
    assert state.active.status == "ACTIVE_UNACKNOWLEDGED"
    state = apply_alarm(state, condition(5, "NORMAL")).state
    with pytest.raises(AlarmConflict, match="cannot be reused"):
        apply_alarm(state, condition(6, alarm_id="alarm-A"))


@pytest.mark.parametrize("field", ["line_id", "source_id", "stream_id", "asset_id"])
def test_source_scope_cannot_be_crossed_for_observations_or_user_actions(field):
    scope = replace(SCOPE, **{field: "foreign"})
    for event in (replace(condition(2), scope=scope), replace(acknowledge(), scope=scope)):
        with pytest.raises(AlarmConflict, match="scope"):
            apply_alarm(opened(), event)


def test_ack_replay_and_repeated_ack_preserve_original_user_and_time():
    ack = acknowledge()
    state = apply_alarm(opened(), ack).state
    assert apply_alarm(state, ack).state == state
    repeated = acknowledge(event_id="ack-B", actor="operator-B", seconds=4)
    changed = apply_alarm(state, repeated).state
    assert changed.instances == state.instances
    assert changed.actions == state.actions
    assert changed.receipts[-1].disposition == "NO_CHANGE"
    assert changed.instances[0].acknowledged_by == "operator-A"
    with pytest.raises(AlarmConflict, match="different content"):
        apply_alarm(state, replace(ack, actor="operator-B"))
    with pytest.raises(AlarmConflict, match="exact alarm"):
        apply_alarm(state, acknowledge(event_id="ack-C", alarm_id="wrong-alarm"))


def test_notes_are_append_only_with_explicit_before_after_and_edit_revision():
    added = AddAlarmNote(
        event_id="note-action-A",
        scope=SCOPE,
        alarm_id="alarm-A",
        actor="operator-A",
        at=AT + timedelta(seconds=2),
        note_id="note-A",
        text="가드와 정지 상태 확인",
    )
    state = apply_alarm(opened(), added).state
    original = state.actions[0]
    edit = EditAlarmNote(
        event_id="note-action-B",
        scope=SCOPE,
        alarm_id="alarm-A",
        actor="operator-B",
        at=AT + timedelta(seconds=3),
        note_id="note-A",
        text="가드 닫힘 확인, 원인 해소 대기",
        expected_previous_action_id="note-action-A",
    )
    state = apply_alarm(state, edit).state
    assert state.actions[0] == original
    assert state.actions[1].before == original.after
    assert state.actions[1].after == edit.text
    assert state.actions[1].previous_action_id == original.action_id
    assert state.actions[1].actor == "operator-B"
    assert state.active.status == "ACTIVE_UNACKNOWLEDGED"
    assert apply_alarm(state, edit).state == state
    with pytest.raises(AlarmConflict, match="exact latest action"):
        apply_alarm(state, replace(edit, event_id="stale-edit", text="stale replacement"))
    with pytest.raises(AlarmConflict, match="note identity"):
        apply_alarm(state, replace(added, event_id="duplicate-note"))


@pytest.mark.parametrize("unusable", ["condition", "quality", "time_quality", "backward_clock"])
def test_unknown_evidence_never_returns_active_alarm_or_erases_known_time(unusable):
    state = opened()
    event = condition(2, "NORMAL")
    if unusable == "condition":
        event = replace(event, condition="UNKNOWN")
    elif unusable == "backward_clock":
        event = replace(event, evidence=replace(event.evidence, observed_at=AT))
    else:
        event = replace(event, evidence=replace(event.evidence, **{unusable: "UNKNOWN"}))
    transition = apply_alarm(state, event)
    assert transition.state.instances == state.instances
    assert transition.state.last_position == 2
    assert transition.state.last_observed_at == state.last_observed_at
    assert transition.state.receipts[-1].disposition == "UNKNOWN_EVIDENCE"
    assert not transition.notify_current


def test_unknown_active_does_not_create_occurrence_and_normal_without_active_is_noop():
    event = condition(1, evidence=replace(condition(1).evidence, quality="UNKNOWN"))
    state = apply_alarm(initial(), event).state
    assert not state.instances
    state = apply_alarm(state, condition(2, "NORMAL")).state
    assert not state.instances
    with pytest.raises(AlarmConflict, match="caller-supplied"):
        apply_alarm(state, condition(3, alarm_id=None))


@pytest.mark.parametrize("historical", [False, True])
def test_late_history_cannot_close_or_reopen_live_condition(historical):
    state = apply_alarm(initial(), condition(10)).state
    late = condition(2, "NORMAL", historical=historical)
    transition = apply_alarm(state, late)
    assert transition.state.instances == state.instances
    assert transition.state.last_position == 10
    assert transition.state.receipts[-1].event == late
    assert transition.state.receipts[-1].disposition == "HISTORY_ONLY"
    assert not transition.notify_current
    state = apply_alarm(transition.state, condition(11, "NORMAL")).state
    replay = apply_alarm(state, condition(3, alarm_id="must-not-open"))
    assert replay.state.active is None
    assert len(replay.state.instances) == 1
    future_history = condition(100, historical=True)
    assert apply_alarm(state, future_history).state.last_position == 11
    with pytest.raises(AlarmConflict, match="position"):
        apply_alarm(state, replace(condition(11), event_id="same-position-other-content"))


@pytest.mark.parametrize("acknowledged", [False, True])
def test_rule_change_has_separate_closure_cause_and_keeps_old_version_and_ack(acknowledged):
    state = opened()
    if acknowledged:
        state = apply_alarm(state, acknowledge(seconds=2)).state
    old_instance = state.instances[0]
    event = ChangeAlarmRule(
        event_id="change-rule",
        scope=SCOPE,
        expected_version="v1",
        actor="equipment-admin",
        at=AT + timedelta(seconds=3),
        new_rule=AlarmRule(rule_id=RULE.rule_id, version="v2", severity="CRITICAL"),
    )
    transition = apply_alarm(state, event)
    state = transition.state
    assert not transition.notify_current
    closed = state.instances[0]
    assert closed.closure_reason == "CONFIG_CHANGED"
    assert closed.return_evidence is None
    assert closed.rule == old_instance.rule == RULE
    assert closed.acknowledged_at == old_instance.acknowledged_at
    assert closed.status == ("RETURNED_ACKNOWLEDGED" if acknowledged else "RETURNED_UNACKNOWLEDGED")
    assert state.actions[-1].kind == "RULE_CHANGE_CLOSE"
    assert apply_alarm(state, event).state == state
    state = apply_alarm(state, condition(4, rule_version="v2", alarm_id="alarm-B")).state
    assert state.active.rule.severity == "CRITICAL"
    assert state.instances[0].rule.severity == "WARNING"
    with pytest.raises(AlarmConflict, match="currently installed"):
        apply_alarm(state, condition(5, rule_version="v1"))
    with pytest.raises(AlarmConflict, match="cannot be reused"):
        apply_alarm(
            state, replace(event, event_id="reuse-v1", expected_version="v2", new_rule=RULE)
        )


def test_config_change_blocks_backdated_normal_qualification_even_without_active_alarm():
    event = ChangeAlarmRule(
        event_id="change-rule",
        scope=SCOPE,
        expected_version="v1",
        actor="equipment-admin",
        at=AT + timedelta(seconds=10),
        new_rule=replace(RULE, version="v2"),
    )
    state = apply_alarm(initial(), event).state
    transition = apply_alarm(state, condition(2, rule_version="v2"))
    assert not transition.state.instances
    assert transition.state.receipts[-1].disposition == "UNKNOWN_EVIDENCE"


def test_immutable_dtos_preserve_exact_measurements_and_round_trip_state():
    event = condition(
        1,
        evidence=replace(
            condition(1).evidence,
            readings=(AlarmReading(name="EventSeq", value=str(UINT64_MAX)),),
        ),
    )
    state = apply_alarm(initial(), event).state
    restored = AlarmState.model_validate_json(state.model_dump_json())
    assert restored == state
    assert restored.instances[0].status == "ACTIVE_UNACKNOWLEDGED"
    assert restored.instances[0].raised_evidence.readings[0].value == str(UINT64_MAX)
    with pytest.raises(ValidationError, match="frozen"):
        state.instances[0].rule.severity = "other"
    with pytest.raises(ValidationError, match="frozen"):
        state.instances[0].raised_evidence.readings[0].value = 0
    with pytest.raises(ValidationError):
        AlarmInstance(
            alarm_id="invalid",
            scope=SCOPE,
            rule=RULE,
            raised_evidence=event.evidence,
            closure_event_id="partial-closure",
        )


def test_note_edits_after_return_do_not_acknowledge_or_change_lifecycle():
    note = AddAlarmNote(
        event_id="note-A",
        scope=SCOPE,
        alarm_id="alarm-A",
        actor="operator-A",
        at=AT + timedelta(seconds=2),
        note_id="note-A",
        text="원인 확인 중",
    )
    state = apply_alarm(opened(), note).state
    state = apply_alarm(state, condition(3, "NORMAL")).state
    edit = EditAlarmNote(
        event_id="note-edit",
        scope=SCOPE,
        alarm_id="alarm-A",
        actor="operator-A",
        at=AT + timedelta(seconds=4),
        note_id="note-A",
        text="원인 해소 기록 추가",
        expected_previous_action_id="note-A",
    )
    changed = apply_alarm(state, edit).state
    assert changed.instances == state.instances
    assert changed.instances[0].status == "RETURNED_UNACKNOWLEDGED"
    assert changed.actions[-1].before == note.text
    changed = apply_alarm(changed, condition(5, alarm_id="alarm-B")).state
    with pytest.raises(AlarmConflict, match="exact latest action"):
        apply_alarm(
            changed,
            replace(
                edit,
                event_id="wrong-instance-edit",
                alarm_id="alarm-B",
                at=AT + timedelta(seconds=6),
            ),
        )
