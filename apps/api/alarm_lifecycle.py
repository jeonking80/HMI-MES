"""Pure alarm lifecycle over qualified evidence; no DB, rule evaluation, clock or I/O."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictFloat, StrictInt, StrictStr, model_validator

from contracts.models import ContractModel, Identifier, Reason, StrictBool, UInt64, UTCTimestamp

AlarmStatus = Literal[
    "ACTIVE_UNACKNOWLEDGED",
    "ACTIVE_ACKNOWLEDGED",
    "RETURNED_UNACKNOWLEDGED",
    "RETURNED_ACKNOWLEDGED",
]


class AlarmScope(ContractModel):
    line_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    asset_id: Identifier


class AlarmRule(ContractModel):
    rule_id: Identifier
    version: Identifier
    severity: Identifier


class AlarmReading(ContractModel):
    name: Identifier
    value: StrictBool | StrictInt | StrictFloat | StrictStr | None


class AlarmEvidence(ContractModel):
    """Normalized decision evidence, not a claim that raw OPC status is Good."""

    observed_at: UTCTimestamp
    references: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    readings: tuple[AlarmReading, ...] = ()
    quality: Literal["KNOWN", "UNKNOWN"] = "KNOWN"
    time_quality: Literal["KNOWN", "UNKNOWN"] = "KNOWN"


class QualifiedCondition(ContractModel):
    kind: Literal["CONDITION"] = "CONDITION"
    event_id: Identifier
    scope: AlarmScope
    rule_version: Identifier
    # Caller-owned durable order of normalized decisions for this scope/rule.
    # Never compare a raw event seq with a capture seq to populate this value.
    position: UInt64
    condition: Literal["ACTIVE", "NORMAL", "UNKNOWN"]
    evidence: AlarmEvidence
    new_alarm_id: Identifier | None = None
    historical: StrictBool = False


class AcknowledgeAlarm(ContractModel):
    kind: Literal["ACKNOWLEDGE"] = "ACKNOWLEDGE"
    event_id: Identifier
    scope: AlarmScope
    alarm_id: Identifier
    actor: Identifier
    at: UTCTimestamp


class AddAlarmNote(ContractModel):
    kind: Literal["NOTE_ADD"] = "NOTE_ADD"
    event_id: Identifier
    scope: AlarmScope
    alarm_id: Identifier
    actor: Identifier
    at: UTCTimestamp
    note_id: Identifier
    text: Reason


class EditAlarmNote(ContractModel):
    kind: Literal["NOTE_EDIT"] = "NOTE_EDIT"
    event_id: Identifier
    scope: AlarmScope
    alarm_id: Identifier
    actor: Identifier
    at: UTCTimestamp
    note_id: Identifier
    expected_previous_action_id: Identifier
    text: Reason


class ChangeAlarmRule(ContractModel):
    kind: Literal["RULE_CHANGE"] = "RULE_CHANGE"
    event_id: Identifier
    scope: AlarmScope
    expected_version: Identifier
    new_rule: AlarmRule
    actor: Identifier
    at: UTCTimestamp


AlarmEvent = Annotated[
    QualifiedCondition | AcknowledgeAlarm | AddAlarmNote | EditAlarmNote | ChangeAlarmRule,
    Field(discriminator="kind"),
]


class AlarmInstance(ContractModel):
    alarm_id: Identifier
    scope: AlarmScope
    rule: AlarmRule
    raised_evidence: AlarmEvidence
    condition_closed_at: UTCTimestamp | None = None
    closure_reason: Literal["NORMAL_RETURN", "CONFIG_CHANGED"] | None = None
    closure_event_id: Identifier | None = None
    return_evidence: AlarmEvidence | None = None
    acknowledged_at: UTCTimestamp | None = None
    acknowledged_by: Identifier | None = None

    @property
    def status(self) -> AlarmStatus:
        if self.condition_closed_at is None:
            return "ACTIVE_ACKNOWLEDGED" if self.acknowledged_at else "ACTIVE_UNACKNOWLEDGED"
        return "RETURNED_ACKNOWLEDGED" if self.acknowledged_at else "RETURNED_UNACKNOWLEDGED"

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.raised_evidence.quality != "KNOWN" or self.raised_evidence.time_quality != "KNOWN":
            raise ValueError("occurrence requires qualified known evidence and time")
        closed = self.condition_closed_at is not None
        if closed != (self.closure_reason is not None) or closed != (
            self.closure_event_id is not None
        ):
            raise ValueError("closed condition requires time, cause and event identity")
        if (self.closure_reason == "NORMAL_RETURN") != (self.return_evidence is not None):
            raise ValueError("only normal return carries normal-return evidence")
        if self.return_evidence is not None and (
            self.return_evidence.quality != "KNOWN"
            or self.return_evidence.time_quality != "KNOWN"
            or self.return_evidence.observed_at != self.condition_closed_at
        ):
            raise ValueError("normal return requires matching qualified evidence and time")
        if (self.acknowledged_at is None) != (self.acknowledged_by is None):
            raise ValueError("acknowledgement requires actor and time")
        for at in (self.condition_closed_at, self.acknowledged_at):
            if at is not None and at < self.raised_evidence.observed_at:
                raise ValueError("action or closure cannot precede qualification")
        return self


class AlarmAction(ContractModel):
    action_id: Identifier
    alarm_id: Identifier
    actor: Identifier
    at: UTCTimestamp
    kind: Literal["ACKNOWLEDGE", "NOTE_ADD", "NOTE_EDIT", "RULE_CHANGE_CLOSE"]
    note_id: Identifier | None = None
    previous_action_id: Identifier | None = None
    before: Reason | None = None
    after: Reason | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.kind == "NOTE_ADD":
            valid = self.note_id is not None and self.after is not None
            valid = valid and self.previous_action_id is None and self.before is None
        elif self.kind == "NOTE_EDIT":
            valid = all(
                value is not None
                for value in (self.note_id, self.previous_action_id, self.before, self.after)
            )
        else:
            valid = all(
                value is None
                for value in (self.note_id, self.previous_action_id, self.before, self.after)
            )
        if not valid:
            raise ValueError("note actions require matching immutable before/after evidence")
        return self


class AlarmReceipt(ContractModel):
    event: AlarmEvent
    disposition: Literal["APPLIED", "NO_CHANGE", "HISTORY_ONLY", "UNKNOWN_EVIDENCE"]


class AlarmState(ContractModel):
    """One scope/rule, retaining returned-unacknowledged instances across recurrence."""

    scope: AlarmScope
    rules: Annotated[tuple[AlarmRule, ...], Field(min_length=1)]
    instances: tuple[AlarmInstance, ...] = ()
    actions: tuple[AlarmAction, ...] = ()
    receipts: tuple[AlarmReceipt, ...] = ()
    last_position: UInt64 | None = None
    last_observed_at: UTCTimestamp | None = None

    @property
    def rule(self) -> AlarmRule:
        return self.rules[-1]

    @property
    def active(self) -> AlarmInstance | None:
        return next((item for item in self.instances if item.condition_closed_at is None), None)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len({rule.version for rule in self.rules}) != len(self.rules) or any(
            rule.rule_id != self.rule.rule_id for rule in self.rules
        ):
            raise ValueError("rule history requires one identity and unique immutable versions")
        if len({item.alarm_id for item in self.instances}) != len(self.instances):
            raise ValueError("alarm identities cannot be reused")
        if sum(item.condition_closed_at is None for item in self.instances) > 1:
            raise ValueError("only one condition can be open for this scope/rule")
        if self.active is not None and self.active.rule != self.rule:
            raise ValueError("open condition must use the current immutable rule version")
        if len({action.action_id for action in self.actions}) != len(self.actions) or any(
            action.alarm_id not in {item.alarm_id for item in self.instances}
            for action in self.actions
        ):
            raise ValueError("actions require unique identities and an exact existing instance")
        if any(item.scope != self.scope or item.rule not in self.rules for item in self.instances):
            raise ValueError("instance scope/rule does not belong to this state")
        if len({receipt.event.event_id for receipt in self.receipts}) != len(self.receipts):
            raise ValueError("event identities cannot be duplicated")
        if any(receipt.event.scope != self.scope for receipt in self.receipts):
            raise ValueError("receipt scope does not belong to this state")
        return self


class AlarmTransition(ContractModel):
    state: AlarmState
    created_alarm_id: Identifier | None = None
    changed_alarm_id: Identifier | None = None
    notify_current: StrictBool = False


class AlarmConflict(ValueError):
    """Scope, immutable identity, rule version or action revision does not match."""


def _replace(model, **changes):
    return type(model).model_validate({**model.model_dump(), **changes})


def apply_alarm(state: AlarmState, event: AlarmEvent) -> AlarmTransition:
    """Return a new immutable snapshot; caller persists the complete change atomically.

    Actor authorization and evidence qualification belong to a later adapter.
    Identical event replay returns the original state without another action/receipt.
    """
    if event.scope != state.scope:
        raise AlarmConflict("alarm source/stream/asset scope does not match")
    existing = next(
        (item for item in state.receipts if item.event.event_id == event.event_id), None
    )
    if existing:
        if existing.event != event:
            raise AlarmConflict("event identity already belongs to different content")
        return AlarmTransition(state=state)
    if isinstance(event, QualifiedCondition):
        return _condition(state, event)
    if isinstance(event, ChangeAlarmRule):
        return _rule_change(state, event)
    return _action(state, event)


def _receipt(state, event, disposition, **changes):
    return _replace(
        state,
        receipts=(*state.receipts, AlarmReceipt(event=event, disposition=disposition)),
        **changes,
    )


def _instances(state, updated):
    return tuple(updated if item.alarm_id == updated.alarm_id else item for item in state.instances)


def _condition(state: AlarmState, event: QualifiedCondition) -> AlarmTransition:
    if event.historical or (
        state.last_position is not None and event.position < state.last_position
    ):
        return AlarmTransition(state=_receipt(state, event, "HISTORY_ONLY"))
    if event.rule_version != state.rule.version:
        raise AlarmConflict("live condition requires the currently installed rule version")
    if event.position == state.last_position:
        raise AlarmConflict("condition position already belongs to another event")
    observed = event.evidence.observed_at
    unknown = (
        event.condition == "UNKNOWN"
        or event.evidence.quality == "UNKNOWN"
        or event.evidence.time_quality == "UNKNOWN"
        or (state.last_observed_at is not None and observed < state.last_observed_at)
    )
    progress = {"last_position": event.position}
    if unknown:
        return AlarmTransition(state=_receipt(state, event, "UNKNOWN_EVIDENCE", **progress))
    progress["last_observed_at"] = observed
    active = state.active
    if event.condition == "ACTIVE" and active is None:
        if event.new_alarm_id is None:
            raise AlarmConflict("a new occurrence requires a caller-supplied alarm identity")
        if any(item.alarm_id == event.new_alarm_id for item in state.instances):
            raise AlarmConflict("alarm identity cannot be reused for recurrence")
        instance = AlarmInstance(
            alarm_id=event.new_alarm_id,
            scope=state.scope,
            rule=state.rule,
            raised_evidence=event.evidence,
        )
        return AlarmTransition(
            state=_receipt(
                state, event, "APPLIED", instances=(*state.instances, instance), **progress
            ),
            created_alarm_id=instance.alarm_id,
            notify_current=True,
        )
    if event.condition == "NORMAL" and active is not None:
        closed = _replace(
            active,
            condition_closed_at=observed,
            closure_reason="NORMAL_RETURN",
            closure_event_id=event.event_id,
            return_evidence=event.evidence,
        )
        return AlarmTransition(
            state=_receipt(
                state, event, "APPLIED", instances=_instances(state, closed), **progress
            ),
            changed_alarm_id=closed.alarm_id,
        )
    return AlarmTransition(state=_receipt(state, event, "NO_CHANGE", **progress))


def _action(state: AlarmState, event: AcknowledgeAlarm | AddAlarmNote | EditAlarmNote):
    instance = next((item for item in state.instances if item.alarm_id == event.alarm_id), None)
    if instance is None:
        raise AlarmConflict("action must target an existing exact alarm identity")
    if event.at < instance.raised_evidence.observed_at:
        raise AlarmConflict("action time cannot precede qualification")
    action = dict(
        action_id=event.event_id,
        alarm_id=event.alarm_id,
        actor=event.actor,
        at=event.at,
        kind=event.kind,
    )
    updated = instance
    if isinstance(event, AcknowledgeAlarm):
        if instance.acknowledged_at is not None:
            return AlarmTransition(state=_receipt(state, event, "NO_CHANGE"))
        updated = _replace(instance, acknowledged_at=event.at, acknowledged_by=event.actor)
    else:
        previous = next(
            (item for item in reversed(state.actions) if item.note_id == event.note_id), None
        )
        if isinstance(event, AddAlarmNote):
            if previous is not None:
                raise AlarmConflict("note identity already exists")
        elif (
            previous is None
            or previous.alarm_id != event.alarm_id
            or previous.action_id != event.expected_previous_action_id
            or event.at < previous.at
        ):
            raise AlarmConflict("note edit requires its exact latest action and nondecreasing time")
        action.update(
            note_id=event.note_id,
            previous_action_id=previous.action_id if previous else None,
            before=previous.after if previous else None,
            after=event.text,
        )
    return AlarmTransition(
        state=_receipt(
            state,
            event,
            "APPLIED",
            instances=_instances(state, updated),
            actions=(*state.actions, AlarmAction(**action)),
        ),
        changed_alarm_id=instance.alarm_id,
    )


def _rule_change(state: AlarmState, event: ChangeAlarmRule):
    if event.expected_version != state.rule.version or event.new_rule.rule_id != state.rule.rule_id:
        raise AlarmConflict("rule replacement requires its current identity and version")
    if any(rule.version == event.new_rule.version for rule in state.rules):
        raise AlarmConflict("rule versions are immutable and cannot be reused")
    if state.last_observed_at is not None and event.at < state.last_observed_at:
        raise AlarmConflict("configuration change cannot precede current live evidence")
    active = state.active
    changes = {"rules": (*state.rules, event.new_rule), "last_observed_at": event.at}
    if active:
        closed = _replace(
            active,
            condition_closed_at=event.at,
            closure_reason="CONFIG_CHANGED",
            closure_event_id=event.event_id,
        )
        changes.update(
            instances=_instances(state, closed),
            actions=(
                *state.actions,
                AlarmAction(
                    action_id=event.event_id,
                    alarm_id=closed.alarm_id,
                    actor=event.actor,
                    at=event.at,
                    kind="RULE_CHANGE_CLOSE",
                ),
            ),
        )
    return AlarmTransition(
        state=_receipt(state, event, "APPLIED", **changes),
        changed_alarm_id=active.alarm_id if active else None,
    )
