"""Caller-owned PostgreSQL alarm transactions; no ingestion, HTTP or delivery loop.

The caller supplies trusted qualification/order/clock context and holds current
user/session authorization locks before entering operator/configuration methods.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy import Connection, text

from apps.api.alarm_capture import AlarmProcessingContext
from apps.api.alarm_fault import (
    FaultCheckpoint,
    FaultUnknownReason,
    fault_condition,
    normalize_fault_capture,
    qualify_fault,
)
from apps.api.alarm_lifecycle import (
    AcknowledgeAlarm,
    AddAlarmNote,
    AlarmAction,
    AlarmConflict,
    AlarmInstance,
    AlarmScope,
    AlarmState,
    ChangeAlarmRule,
    EditAlarmNote,
    QualifiedCondition,
    apply_alarm,
)
from apps.api.alarm_qualification import (
    QualificationState,
    RuleObservation,
    as_qualified_condition,
    qualify,
    replace_rule_config,
)
from apps.api.alarm_rules import PLCFaultRuleConfig, StoredRuleConfig, require_rule_target
from apps.api.auth import PERMISSIONS, AuthorizationError, Principal
from apps.api.database import SCHEMA_LOCK_ID, SUPPORTED_ALARM_REVISIONS
from contracts.models import ContractModel, FaultEvent, Identifier, Reason, StateEvent, UTCTimestamp
from contracts.transport import BatchBody, batch_digest

_CONFIG = TypeAdapter(StoredRuleConfig)
_ID = TypeAdapter(Identifier)
_TIME = TypeAdapter(UTCTimestamp)


class AlarmStoreUnavailable(RuntimeError):
    pass


class StoredAlarmResult(ContractModel):
    input_id: Identifier
    disposition: str
    created_alarm_id: Identifier | None = None
    changed_alarm_id: Identifier | None = None
    replayed: bool = False
    notify_current: bool = False


def _json(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _channel(scope, rule_id):
    return _hash([scope.model_dump(mode="json"), _ID.validate_python(rule_id)])


def _ready(db):
    if db.dialect.name != "postgresql" or not db.in_transaction():
        raise AlarmStoreUnavailable("Caller-owned PostgreSQL transaction required")
    db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID})
    heads = db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(heads) != 1 or heads[0] not in SUPPORTED_ALARM_REVISIONS:
        raise AlarmStoreUnavailable("Alarm persistence schema required")


def _permission(principal, permission):
    if (
        not isinstance(principal, Principal)
        or principal.must_change_password
        or not principal.roles & PERMISSIONS[permission]
    ):
        raise AuthorizationError("Alarm permission denied")
    return _ID.validate_python(principal.login)


def _lock_input_channel(db, scope, rule_id, input_id):
    _ready(db)
    scope = AlarmScope.model_validate_json(scope.model_dump_json())
    channel = _channel(scope, rule_id)
    # Shared schema lock -> caller's auth locks -> sorted input/channel locks.
    for key in sorted(["alarm-channel:" + channel, "alarm-input:" + input_id]):
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key})
    return channel


def _begin(db, scope, rule_id, input_id, intent):
    channel = _lock_input_channel(db, scope, rule_id, input_id)
    old = (
        db.execute(text("SELECT * FROM alarm_receipts WHERE input_id=:id"), {"id": input_id})
        .mappings()
        .first()
    )
    if old:
        if old["channel_id"] != channel or old["input_sha256"] != _hash(intent):
            raise AlarmConflict("Input identity already belongs to different immutable content")
        result = StoredAlarmResult.model_validate(old["receipt"])
        return channel, result.model_copy(update={"replayed": True, "notify_current": False})
    return channel, None


def _row(db, channel):
    row = (
        db.execute(
            text("SELECT * FROM alarm_channels WHERE channel_id=:id FOR UPDATE"), {"id": channel}
        )
        .mappings()
        .first()
    )
    if row is None:
        raise AlarmConflict("Alarm rule channel is not installed")
    return row


def _new_checkpoint(scope, config):
    require_rule_target(scope, config)
    if isinstance(config, PLCFaultRuleConfig):
        return FaultCheckpoint(scope=scope, config=config)
    return QualificationState(scope=scope, config=config)


def _decode_checkpoint(raw):
    if raw["config"]["kind"] == "PLC_FAULT":
        return FaultCheckpoint.model_validate(raw)
    return QualificationState.model_validate(raw)


def _config(db, channel, version):
    value = db.execute(
        text("SELECT config FROM alarm_rule_versions WHERE channel_id=:c AND version=:v"),
        {"c": channel, "v": version},
    ).scalar_one()
    return _CONFIG.validate_python(value)


def _require_version(db, channel, version):
    if (
        db.execute(
            text("SELECT 1 FROM alarm_rule_versions WHERE channel_id=:c AND version=:v"),
            {"c": channel, "v": version},
        ).first()
        is None
    ):
        raise AlarmConflict("Evidence requires an installed immutable rule version")


def _load(db, channel, row, event=None):
    target = getattr(event, "alarm_id", None)
    instances = tuple(
        AlarmInstance.model_validate(value)
        for value in db.execute(
            text(
                "SELECT payload FROM alarm_instances WHERE channel_id=:c "
                "AND (returned_at IS NULL OR alarm_id=:target)"
            ),
            {"c": channel, "target": target},
        ).scalars()
    )
    actions = ()
    note = getattr(event, "note_id", None)
    if note is not None:
        # The note identity is global, including when its owner is omitted history.
        action = (
            db.execute(
                text(
                    "SELECT channel_id,payload FROM alarm_actions WHERE note_id=:n "
                    "ORDER BY sequence DESC LIMIT 1"
                ),
                {"n": note},
            )
            .mappings()
            .first()
        )
        if action:
            if (
                isinstance(event, AddAlarmNote)
                or action["channel_id"] != channel
                or action["payload"]["alarm_id"] != event.alarm_id
            ):
                raise AlarmConflict("Note identity already exists in another revision or channel")
            actions = (AlarmAction.model_validate(action["payload"]),)
    current = _config(db, channel, row["current_version"]).rule
    versions = {item.rule.version: item.rule for item in instances}
    versions.pop(current.version, None)
    return AlarmState(
        scope=AlarmScope(**{k: row[k] for k in AlarmScope.model_fields}),
        rules=(*versions.values(), current),
        instances=instances,
        actions=actions,
        last_position=int(row["last_position"]) if row["last_position"] is not None else None,
        last_observed_at=row["last_observed_at"],
    )


def _receipt(db, channel, input_id, intent, result):
    db.execute(
        text(
            "INSERT INTO alarm_receipts VALUES (:id,:c,:hash,CAST(:intent AS jsonb),"
            "CAST(:receipt AS jsonb))"
        ),
        {
            "id": input_id,
            "c": channel,
            "hash": _hash(intent),
            "intent": _json(intent),
            "receipt": result.model_dump_json(),
        },
    )


def _checkpoint(db, channel, state, qualification):
    db.execute(
        text(
            "UPDATE alarm_channels SET current_version=:v,last_position=:p,"
            "last_observed_at=:at,qualification=CAST(:q AS jsonb) WHERE channel_id=:c"
        ),
        {
            "c": channel,
            "v": state.rule.version,
            "p": state.last_position,
            "at": state.last_observed_at,
            "q": qualification.model_dump_json(),
        },
    )


def _persist(db, channel, before, transition, qualification, intent, *, preserve_checkpoint=False):
    state = transition.state
    event = state.receipts[-1].event
    original = {item.alarm_id: item for item in before.instances}
    for instance in state.instances:
        values = {
            "id": instance.alarm_id,
            "c": channel,
            "v": instance.rule.version,
            "at": instance.raised_evidence.observed_at,
            "returned": instance.condition_closed_at,
            "acked": instance.acknowledged_at,
            "payload": instance.model_dump_json(),
        }
        if instance.alarm_id not in original:
            # Global PK enforces non-reuse even when historical instances were not hydrated.
            db.execute(
                text(
                    "INSERT INTO alarm_instances VALUES "
                    "(:id,:c,:v,:at,:returned,:acked,CAST(:payload AS jsonb))"
                ),
                values,
            )
        elif original[instance.alarm_id] != instance:
            db.execute(
                text(
                    "UPDATE alarm_instances SET returned_at=:returned,"
                    "acknowledged_at=:acked,payload=CAST(:payload AS jsonb) "
                    "WHERE alarm_id=:id AND channel_id=:c"
                ),
                values,
            )
    old_actions = {item.action_id for item in before.actions}
    for action in state.actions:
        if action.action_id not in old_actions:
            db.execute(
                text(
                    "INSERT INTO alarm_actions "
                    "(action_id,channel_id,alarm_id,actor,kind,note_id,previous_action_id,"
                    "payload) VALUES (:id,:c,:alarm,:actor,:kind,:note,:previous,"
                    "CAST(:payload AS jsonb))"
                ),
                {
                    "id": action.action_id,
                    "c": channel,
                    "alarm": action.alarm_id,
                    "actor": action.actor,
                    "kind": action.kind,
                    "note": action.note_id,
                    "previous": action.previous_action_id,
                    "payload": action.model_dump_json(),
                },
            )
    if not preserve_checkpoint:
        _checkpoint(db, channel, state, qualification)
    result = StoredAlarmResult(
        input_id=event.event_id,
        disposition=state.receipts[-1].disposition,
        created_alarm_id=transition.created_alarm_id,
        changed_alarm_id=transition.changed_alarm_id,
        notify_current=transition.notify_current,
    )
    _receipt(db, channel, event.event_id, intent, result)
    if transition.notify_current:
        db.execute(
            text("INSERT INTO alarm_outbox VALUES (:alarm,:c,:input,CAST(:payload AS jsonb))"),
            {
                "alarm": transition.created_alarm_id,
                "c": channel,
                "input": event.event_id,
                "payload": state.active.model_dump_json(),
            },
        )
    return result


def install_rule(
    db: Connection,
    scope: AlarmScope,
    config: StoredRuleConfig,
    *,
    input_id: str,
    principal: Principal,
    at: datetime,
) -> StoredAlarmResult:
    actor = _permission(principal, "equipment_manage")
    config = _CONFIG.validate_json(_json(config))
    require_rule_target(scope, config)
    input_id, at = _ID.validate_python(input_id), _TIME.validate_python(at)
    intent = {
        "kind": "INSTALL",
        "scope": scope.model_dump(mode="json"),
        "config": config.model_dump(mode="json"),
        "actor": actor,
        "at": at.isoformat(),
    }
    channel, replay = _begin(db, scope, config.rule.rule_id, input_id, intent)
    if replay:
        return replay
    if db.execute(text("SELECT 1 FROM alarm_channels WHERE channel_id=:c"), {"c": channel}).first():
        raise AlarmConflict("Rule channel already installed; replacement requires expected version")
    qualification = _new_checkpoint(scope, config)
    db.execute(
        text(
            "INSERT INTO alarm_channels (channel_id,line_id,source_id,stream_id,asset_id,rule_id,"
            "current_version,last_position,last_observed_at,qualification) VALUES "
            "(:c,:line,:source,:stream,:asset,:rule,:version,NULL,NULL,CAST(:q AS jsonb))"
        ),
        {
            "c": channel,
            "line": scope.line_id,
            "source": scope.source_id,
            "stream": scope.stream_id,
            "asset": scope.asset_id,
            "rule": config.rule.rule_id,
            "version": config.rule.version,
            "q": qualification.model_dump_json(),
        },
    )
    _version(db, channel, config, actor, at, input_id)
    result = StoredAlarmResult(input_id=input_id, disposition="INSTALLED")
    _receipt(db, channel, input_id, intent, result)
    return result


def _version(db, channel, config, actor, at, input_id):
    db.execute(
        text(
            "INSERT INTO alarm_rule_versions VALUES (:c,:v,CAST(:config AS jsonb),:actor,:at,:id)"
        ),
        {
            "c": channel,
            "v": config.rule.version,
            "config": config.model_dump_json(),
            "actor": actor,
            "at": at,
            "id": input_id,
        },
    )


def replace_rule(
    db: Connection,
    scope: AlarmScope,
    config: StoredRuleConfig,
    *,
    input_id: str,
    expected_version: str,
    principal: Principal,
    at: datetime,
) -> StoredAlarmResult:
    actor = _permission(principal, "equipment_manage")
    config = _CONFIG.validate_json(_json(config))
    require_rule_target(scope, config)
    event = ChangeAlarmRule(
        event_id=input_id,
        scope=scope,
        expected_version=expected_version,
        new_rule=config.rule,
        actor=actor,
        at=at,
    )
    intent = {"event": event.model_dump(mode="json"), "config": config.model_dump(mode="json")}
    channel, replay = _begin(db, scope, config.rule.rule_id, event.event_id, intent)
    if replay:
        return replay
    row = _row(db, channel)
    installed_at = db.execute(
        text("SELECT changed_at FROM alarm_rule_versions WHERE channel_id=:c AND version=:v"),
        {"c": channel, "v": row["current_version"]},
    ).scalar_one()
    if event.at < installed_at:
        raise AlarmConflict("Rule replacement cannot precede the current installed version")
    state = _load(db, channel, row)
    old_checkpoint = _decode_checkpoint(row["qualification"])
    if isinstance(old_checkpoint, FaultCheckpoint):
        if not isinstance(config, PLCFaultRuleConfig) or config.rule.rule_id != state.rule.rule_id:
            raise AlarmConflict("Rule replacement must retain its identity and kind")
        qualification = FaultCheckpoint(
            scope=scope,
            config=config,
            highest_event_ceiling=old_checkpoint.highest_event_ceiling,
            last_known_at=old_checkpoint.last_known_at,
        )
    else:
        if isinstance(config, PLCFaultRuleConfig):
            raise AlarmConflict("Rule replacement cannot change analog/digital kind")
        qualification = replace_rule_config(old_checkpoint, config)
    transition = apply_alarm(state, event)
    _version(db, channel, config, actor, event.at, event.event_id)
    return _persist(db, channel, state, transition, qualification, intent)


def apply_condition(db: Connection, rule_id: str, event: QualifiedCondition) -> StoredAlarmResult:
    """Trusted already-qualified conditions; unsuitable for raw network JSON."""
    event = QualifiedCondition.model_validate_json(event.model_dump_json())
    intent = {"kind": "QUALIFIED", "event": event.model_dump(mode="json")}
    channel, replay = _begin(db, event.scope, rule_id, event.event_id, intent)
    if replay:
        return replay
    row = _row(db, channel)
    _require_version(db, channel, event.rule_version)
    state = _load(db, channel, row)
    qualification = _decode_checkpoint(row["qualification"])
    if isinstance(qualification, FaultCheckpoint):
        raise AlarmConflict("Line fault channels require verified original capture/reset evidence")
    # Crossing adapter modes cannot preserve a partially accumulated dwell.
    transition = apply_alarm(state, event)
    disposition = transition.state.receipts[-1].disposition
    if disposition != "HISTORY_ONLY":
        qualification = _break(qualification, "QUALIFIED_INPUT")
        if transition.state.active:
            qualification = qualification.model_copy(update={"latched": "ACTIVE"})
        elif disposition != "UNKNOWN_EVIDENCE" and event.condition == "NORMAL":
            qualification = qualification.model_copy(update={"latched": "NORMAL"})
    return _persist(db, channel, state, transition, qualification, intent)


def apply_observation(
    db: Connection, rule_id: str, observation: RuleObservation, *, new_alarm_id: str | None = None
) -> StoredAlarmResult:
    """Trusted normalized capture with durable order and verified phase/clock context."""
    observation = RuleObservation.model_validate_json(observation.model_dump_json())
    intent = {
        "kind": "OBSERVATION",
        "observation": observation.model_dump(mode="json"),
        "new_alarm_id": new_alarm_id,
    }
    channel, replay = _begin(db, observation.scope, rule_id, observation.observation_id, intent)
    if replay:
        return replay
    row = _row(db, channel)
    _require_version(db, channel, observation.rule_version)
    state = _load(db, channel, row)
    qualification = _decode_checkpoint(row["qualification"])
    if isinstance(qualification, FaultCheckpoint):
        raise AlarmConflict("Analog observations cannot enter a digital fault channel")
    original_qualification = qualification
    # Direct qualified inputs and settings changes also establish a live UTC barrier.
    # The qualifier must see it before mutating its latch/candidate, not only after
    # lifecycle decides that older wall-clock evidence is UNKNOWN.
    if state.last_observed_at is not None and (
        qualification.last_known_at is None or qualification.last_known_at < state.last_observed_at
    ):
        qualification = qualification.model_copy(update={"last_known_at": state.last_observed_at})
    if state.last_position is not None and observation.position < state.last_position:
        observation = observation.model_copy(update={"historical": True})
    result = qualify(qualification, observation)
    event = as_qualified_condition(result, new_alarm_id=new_alarm_id)
    transition = apply_alarm(state, event)
    return _persist(
        db,
        channel,
        state,
        transition,
        original_qualification if result.disposition == "HISTORY_ONLY" else result.state,
        intent,
    )


def _break(qualification, reason):
    if isinstance(qualification, FaultCheckpoint):
        return qualification.model_copy(
            update={
                "last_condition": "UNKNOWN",
                "last_reason": reason,
                "last_quality": "UNKNOWN",
                "last_time_quality": "UNKNOWN",
            }
        )
    return qualification.model_copy(
        update={
            "candidate": None,
            "candidate_since": None,
            "clock_anchor_at": None,
            "clock_anchor_seconds": None,
            "last_condition": "UNKNOWN",
            "last_reason": reason,
            "last_quality": "UNKNOWN",
            "last_time_quality": "UNKNOWN",
        }
    )


def break_continuity(
    db: Connection,
    scope: AlarmScope,
    rule_id: str,
    *,
    input_id: str,
    position: int,
    reason: Literal["UNSUPPORTED_CAPTURE", "GAP", "RESTART"],
    evidence_reference: str,
    historical: bool = False,
) -> StoredAlarmResult:
    """Persist unsupported input/gap evidence without fabricating a sample or normal return."""
    input_id = _ID.validate_python(input_id)
    from contracts.models import UInt64

    position = TypeAdapter(UInt64).validate_python(position)
    if reason not in {"UNSUPPORTED_CAPTURE", "GAP", "RESTART"} or type(historical) is not bool:
        raise ValueError("Explicit continuity reason and historical classification required")
    intent = {
        "kind": "BREAK",
        "scope": scope.model_dump(mode="json"),
        "position": position,
        "reason": reason,
        "reference": _ID.validate_python(evidence_reference),
        "historical": historical,
    }
    channel, replay = _begin(db, scope, rule_id, input_id, intent)
    if replay:
        return replay
    row = _row(db, channel)
    if historical or (row["last_position"] is not None and position < row["last_position"]):
        result = StoredAlarmResult(input_id=input_id, disposition="HISTORY_ONLY")
    else:
        if row["last_position"] == position:
            raise AlarmConflict("Continuity position already used")
        state = _load(db, channel, row).model_copy(update={"last_position": position})
        qualification = _break(_decode_checkpoint(row["qualification"]), reason)
        _checkpoint(db, channel, state, qualification)
        result = StoredAlarmResult(input_id=input_id, disposition="CONTINUITY_BROKEN")
    _receipt(db, channel, input_id, intent, result)
    return result


def act_on_alarm(
    db: Connection,
    scope: AlarmScope,
    rule_id: str,
    *,
    input_id: str,
    alarm_id: str,
    principal: Principal,
    at: datetime,
    kind: Literal["ACKNOWLEDGE", "NOTE_ADD", "NOTE_EDIT"],
    note_id: str | None = None,
    text_value: str | None = None,
    expected_previous_action_id: str | None = None,
) -> StoredAlarmResult:
    actor = _permission(principal, "alarm_action")
    values = dict(event_id=input_id, scope=scope, alarm_id=alarm_id, actor=actor, at=at)
    if kind == "ACKNOWLEDGE":
        if any(v is not None for v in (note_id, text_value, expected_previous_action_id)):
            raise ValueError("Acknowledgement does not accept note fields")
        event = AcknowledgeAlarm(**values)
    elif kind == "NOTE_ADD":
        if expected_previous_action_id is not None:
            raise ValueError("New note has no previous revision")
        event = AddAlarmNote(**values, note_id=note_id, text=text_value)
    elif kind == "NOTE_EDIT":
        event = EditAlarmNote(
            **values,
            note_id=note_id,
            text=text_value,
            expected_previous_action_id=expected_previous_action_id,
        )
    else:
        raise ValueError("Unsupported action")
    intent = event.model_dump(mode="json")
    channel, replay = _begin(db, scope, rule_id, event.event_id, intent)
    if replay:
        return replay
    row = _row(db, channel)
    state = _load(db, channel, row, event)
    return _persist(
        db,
        channel,
        state,
        apply_alarm(state, event),
        _decode_checkpoint(row["qualification"]),
        intent,
    )


def read_instances(
    db: Connection,
    scope: AlarmScope,
    rule_id: str,
    *,
    limit: int = 100,
    before_alarm_id: str | None = None,
) -> tuple[AlarmInstance, ...]:
    """Bounded keyset read by occurrence time/identity, including returned unacknowledged."""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    _ready(db)
    channel = _channel(scope, rule_id)
    cursor = None
    if before_alarm_id is not None:
        cursor = db.execute(
            text(
                "SELECT occurred_at,alarm_id FROM alarm_instances "
                "WHERE channel_id=:c AND alarm_id=:id"
            ),
            {"c": channel, "id": _ID.validate_python(before_alarm_id)},
        ).first()
        if cursor is None:
            raise AlarmConflict("Page cursor is outside this channel")
    rows = db.execute(
        text(
            "SELECT payload FROM alarm_instances WHERE channel_id=:c "
            + ("AND (occurred_at,alarm_id)<(:at,:id) " if cursor else "")
            + "ORDER BY occurred_at DESC,alarm_id DESC LIMIT :limit"
        ),
        {
            "c": channel,
            "limit": limit,
            "at": cursor[0] if cursor else None,
            "id": cursor[1] if cursor else None,
        },
    ).scalars()
    return tuple(AlarmInstance.model_validate(row) for row in rows)


def read_actions(
    db: Connection,
    scope: AlarmScope,
    rule_id: str,
    alarm_id: str,
    *,
    limit: int = 100,
    before_action_id: str | None = None,
) -> tuple[AlarmAction, ...]:
    """Bounded exact-instance audit/note history; missing targets cannot leak other scopes."""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    _ready(db)
    channel, alarm_id = _channel(scope, rule_id), _ID.validate_python(alarm_id)
    exists = db.execute(
        text("SELECT 1 FROM alarm_instances WHERE channel_id=:c AND alarm_id=:a"),
        {"c": channel, "a": alarm_id},
    ).first()
    if exists is None:
        raise AlarmConflict("Exact alarm instance is outside this channel")
    cursor = None
    if before_action_id is not None:
        cursor = db.execute(
            text(
                "SELECT sequence FROM alarm_actions WHERE channel_id=:c "
                "AND alarm_id=:a AND action_id=:id"
            ),
            {"c": channel, "a": alarm_id, "id": _ID.validate_python(before_action_id)},
        ).scalar_one_or_none()
        if cursor is None:
            raise AlarmConflict("Action cursor is outside this instance")
    rows = db.execute(
        text(
            "SELECT payload FROM alarm_actions WHERE channel_id=:c AND alarm_id=:a "
            + ("AND sequence<:cursor " if cursor is not None else "")
            + "ORDER BY sequence DESC LIMIT :limit"
        ),
        {"c": channel, "a": alarm_id, "limit": limit, "cursor": cursor},
    ).scalars()
    return tuple(AlarmAction.model_validate(row) for row in rows)


class AlarmNotFound(LookupError):
    """Exact requested scope, instance or operator receipt does not exist."""


class AlarmCursorError(ValueError):
    pass


class _OperatorIntent(ContractModel):
    action_id: Identifier
    scope: AlarmScope
    rule_id: Identifier
    alarm_id: Identifier
    actor: Identifier


class AcknowledgeIntent(_OperatorIntent):
    kind: Literal["ACKNOWLEDGE"]


class AddNoteIntent(_OperatorIntent):
    kind: Literal["NOTE_ADD"]
    note_id: Identifier
    text: Reason


class EditNoteIntent(_OperatorIntent):
    kind: Literal["NOTE_EDIT"]
    note_id: Identifier
    text: Reason
    expected_previous_action_id: Identifier


OperatorAlarmIntent = Annotated[
    AcknowledgeIntent | AddNoteIntent | EditNoteIntent, Field(discriminator="kind")
]
OPERATOR_INTENT = TypeAdapter(OperatorAlarmIntent)


class OperatorActionReceipt(ContractModel):
    intent: OperatorAlarmIntent
    recorded_at: UTCTimestamp
    receipt: StoredAlarmResult
    action: AlarmAction | None


def _scope_values(scope):
    return {name: getattr(scope, name) for name in AlarmScope.model_fields}


def _scope_sql():
    return " AND ".join(f"c.{name}=:{name}" for name in AlarmScope.model_fields)


def _registered_scope(db, scope):
    if scope.asset_id == "Line":
        if (
            db.execute(
                text(
                    "SELECT 1 FROM source_streams WHERE line_id=:line_id "
                    "AND source_id=:source_id AND stream_id=:stream_id"
                ),
                _scope_values(scope),
            ).first()
            is None
        ):
            raise AlarmNotFound("Logical line scope not found")
        return
    if scope.asset_id != "Sealer":
        raise AlarmNotFound("Alarm target is not implemented")
    if (
        db.execute(
            text(
                "SELECT 1 FROM source_streams s JOIN assets a ON a.line_id=s.line_id "
                "WHERE s.line_id=:line_id AND s.source_id=:source_id "
                "AND s.stream_id=:stream_id AND a.code=:asset_id"
            ),
            _scope_values(scope),
        ).first()
        is None
    ):
        raise AlarmNotFound("Alarm scope not found")


def read_instance(db: Connection, scope: AlarmScope, rule_id: str, alarm_id: str) -> AlarmInstance:
    _ready(db)
    row = db.execute(
        text("SELECT payload FROM alarm_instances WHERE channel_id=:c AND alarm_id=:id"),
        {"c": _channel(scope, rule_id), "id": _ID.validate_python(alarm_id)},
    ).scalar_one_or_none()
    if row is None:
        raise AlarmNotFound("Alarm instance not found in exact scope")
    return AlarmInstance.model_validate(row)


def read_alarm_page(
    db: Connection,
    scope: AlarmScope,
    *,
    rule_id: str | None = None,
    view: Literal["attention", "all"] = "attention",
    severity: str | None = None,
    limit: int = 50,
    before_alarm_id: str | None = None,
) -> dict:
    """Cross-rule page inside one exact source/stream/asset; immutable occurrence order."""
    _ready(db)
    if type(limit) is not int or not 1 <= limit <= 100 or view not in {"attention", "all"}:
        raise ValueError("Invalid alarm page")
    _registered_scope(db, scope)
    params = _scope_values(scope) | {"limit": limit + 1}
    where = _scope_sql()
    if rule_id is not None:
        params["rule_id"] = _ID.validate_python(rule_id)
        where += " AND c.rule_id=:rule_id"
    if severity is not None:
        params["severity"] = _ID.validate_python(severity)
        where += " AND i.payload->'rule'->>'severity'=:severity"
    cursor = None
    if before_alarm_id is not None:
        params["cursor"] = _ID.validate_python(before_alarm_id)
        cursor = db.execute(
            text(
                "SELECT i.occurred_at,i.alarm_id FROM alarm_instances i "
                "JOIN alarm_channels c USING(channel_id) WHERE " + where + " AND i.alarm_id=:cursor"
            ),
            params,
        ).first()
        if cursor is None:
            raise AlarmCursorError("Alarm page cursor outside requested scope or rule")
    if view == "attention":
        where += " AND (i.returned_at IS NULL OR i.acknowledged_at IS NULL)"
    if cursor is not None:
        params.update(cursor_at=cursor.occurred_at)
        where += " AND (i.occurred_at,i.alarm_id)<(:cursor_at,:cursor)"
    rows = (
        db.execute(
            text(
                "SELECT i.payload FROM alarm_instances i JOIN alarm_channels c USING(channel_id) "
                "WHERE " + where + " ORDER BY i.occurred_at DESC,i.alarm_id DESC LIMIT :limit"
            ),
            params,
        )
        .scalars()
        .all()
    )
    instances = [AlarmInstance.model_validate(row) for row in rows[:limit]]
    has_more = len(rows) > limit
    installed = (
        db.execute(
            text(
                "SELECT c.rule_id,c.current_version,v.config,count(*) OVER() AS total "
                "FROM alarm_channels c JOIN alarm_rule_versions v "
                "ON v.channel_id=c.channel_id AND v.version=c.current_version WHERE "
                + _scope_sql()
                + " ORDER BY c.rule_id LIMIT 101"
            ),
            _scope_values(scope),
        )
        .mappings()
        .all()
    )
    coverage = {
        "scope": scope,
        "implemented_kinds": ["PLC_FAULT"]
        if scope.asset_id == "Line"
        else ["TEMPERATURE_DEVIATION", "PRESSURE_LOW"],
        "installed_channel_count": installed[0]["total"] if installed else 0,
        "installed_channels": [
            {
                "rule_id": row["rule_id"],
                "rule_version": row["current_version"],
                "kind": row["config"]["kind"],
                "severity": row["config"]["rule"]["severity"],
            }
            for row in installed[:100]
        ],
        "has_more_installed_channels": len(installed) > 100,
        "consumer_status": "UNKNOWN",
        "runtime_configuration_status": "UNKNOWN",
    }
    return {
        "coverage": coverage,
        "items": [{"instance": instance, "status": instance.status} for instance in instances],
        "has_more": has_more,
        "next_before_alarm_id": instances[-1].alarm_id if has_more else None,
    }


def read_action_page(
    db: Connection,
    scope: AlarmScope,
    rule_id: str,
    alarm_id: str,
    *,
    limit: int = 50,
    before_action_id: str | None = None,
) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid action page limit")
    read_instance(db, scope, rule_id, alarm_id)
    try:
        actions = read_actions(
            db, scope, rule_id, alarm_id, limit=limit + 1, before_action_id=before_action_id
        )
    except AlarmConflict:
        raise AlarmCursorError("Action cursor outside exact alarm instance") from None
    has_more = len(actions) > limit
    return {
        "items": actions[:limit],
        "has_more": has_more,
        "next_before_action_id": actions[limit - 1].action_id if has_more else None,
    }


def _receipt_operator_intent(raw, rule_id):
    values = dict(raw)
    values["action_id"] = values.pop("event_id")
    values["rule_id"] = rule_id
    values.pop("at")
    return OPERATOR_INTENT.validate_python(values)


def read_operator_receipt(
    db: Connection,
    scope: AlarmScope,
    rule_id: str,
    alarm_id: str,
    action_id: str,
) -> OperatorActionReceipt:
    """Passive recovery of the original intent/time/receipt, including no-change ACK."""
    _ready(db)
    channel = _channel(scope, rule_id)
    row = (
        db.execute(
            text("SELECT intent,receipt FROM alarm_receipts WHERE input_id=:id AND channel_id=:c"),
            {"id": _ID.validate_python(action_id), "c": channel},
        )
        .mappings()
        .first()
    )
    try:
        intent = _receipt_operator_intent(row["intent"], rule_id) if row else None
    except (ValidationError, KeyError, TypeError):
        intent = None
    if intent is None or intent.alarm_id != alarm_id or intent.scope != scope:
        raise AlarmNotFound("Operator receipt not found for exact alarm instance")
    action = db.execute(
        text(
            "SELECT payload FROM alarm_actions WHERE action_id=:id "
            "AND channel_id=:c AND alarm_id=:a"
        ),
        {"id": action_id, "c": channel, "a": alarm_id},
    ).scalar_one_or_none()
    return OperatorActionReceipt(
        intent=intent,
        recorded_at=row["intent"]["at"],
        receipt=StoredAlarmResult.model_validate(row["receipt"]),
        action=AlarmAction.model_validate(action) if action is not None else None,
    )


def submit_operator_action(
    db: Connection,
    intent: OperatorAlarmIntent,
    *,
    principal: Principal,
    clock: Callable[[], datetime],
) -> OperatorActionReceipt:
    """Choose time once under the store's sorted locks; exact retries reuse original time.

    Caller must hold live user/session permission and mutation protection locks.
    GET recovery uses read_operator_receipt and never enters this mutation path.
    """
    actor = _permission(principal, "alarm_action")
    intent = OPERATOR_INTENT.validate_json(OPERATOR_INTENT.dump_json(intent))
    if intent.actor != actor:
        raise AuthorizationError("Operator intent actor does not match live principal")
    channel = _lock_input_channel(db, intent.scope, intent.rule_id, intent.action_id)
    old = (
        db.execute(
            text("SELECT intent,channel_id FROM alarm_receipts WHERE input_id=:id"),
            {"id": intent.action_id},
        )
        .mappings()
        .first()
    )
    if old is not None:
        try:
            original = _receipt_operator_intent(old["intent"], intent.rule_id)
        except (ValidationError, KeyError, TypeError):
            raise AlarmConflict(
                "Action identity already belongs to another immutable input"
            ) from None
        if old["channel_id"] != channel or original != intent:
            raise AlarmConflict("Action identity already belongs to different immutable content")
        at = _TIME.validate_python(old["intent"]["at"])
    else:
        read_instance(db, intent.scope, intent.rule_id, intent.alarm_id)
        at = _TIME.validate_python(clock())
    result = act_on_alarm(
        db,
        intent.scope,
        intent.rule_id,
        input_id=intent.action_id,
        alarm_id=intent.alarm_id,
        principal=principal,
        at=at,
        kind=intent.kind,
        note_id=getattr(intent, "note_id", None),
        text_value=getattr(intent, "text", None),
        expected_previous_action_id=getattr(intent, "expected_previous_action_id", None),
    )
    recovered = read_operator_receipt(
        db, intent.scope, intent.rule_id, intent.alarm_id, intent.action_id
    )
    return recovered.model_copy(update={"receipt": result})


def apply_fault_capture(
    db: Connection,
    batch: BatchBody,
    context: AlarmProcessingContext,
    *,
    new_alarm_id: str | None = None,
    forced_unknown_reason: FaultUnknownReason | None = None,
) -> StoredAlarmResult:
    """Trusted adapter seam, not an HTTP/network input or mixed runtime consumer.

    Reuses one original stored capture and server-owned order/clock classification.
    Replays recover the original derived proof before consulting changing progress.
    The future coordinator must hold its whole sorted channel lock union before
    calling this function alongside any other alarm consumer.
    """
    from apps.api.alarm_capture import CaptureIdentity, _digest
    from apps.api.ingestion import IngestionService

    forced_unknown_reason = TypeAdapter(FaultUnknownReason | None).validate_python(
        forced_unknown_reason
    )
    batch = BatchBody.model_validate_json(batch.model_dump_json())
    context = AlarmProcessingContext.model_validate_json(context.model_dump_json())
    if (
        context.scope.asset_id != "Line"
        or context.scope.source_id != batch.source_id
        or context.scope.stream_id != batch.stream_id
        or context.capture != CaptureIdentity.from_batch(batch)
        or context.batch_sha256 != batch_digest(batch)
    ):
        raise AlarmConflict("Fault capture requires exact original logical Line scope")
    identity = {
        "scope": context.scope.model_dump(mode="json"),
        "rule_id": context.rule.rule_id,
        "rule_version": context.rule.version,
        "capture": context.capture.model_dump(mode="json"),
    }
    input_id = "fault-observation:" + _digest(identity)
    stable_input = {
        "batch": batch.model_dump(mode="json"),
        "context": context.model_dump(mode="json"),
        "new_alarm_id": new_alarm_id,
    }
    # Omit the absent key to preserve exact pre-seam immutable receipt inputs.
    if forced_unknown_reason is not None:
        stable_input["forced_unknown_reason"] = forced_unknown_reason
    _ready(db)
    # Same source-first order as ingestion/runtime; caller-owned locks re-enter.
    IngestionService._advisory(db, "gateway", batch.gateway_id)
    source = IngestionService._lock_stream(db, batch.source_id, batch.stream_id)
    channel = _lock_input_channel(db, context.scope, context.rule.rule_id, input_id)
    old = (
        db.execute(
            text("SELECT channel_id,intent,receipt FROM alarm_receipts WHERE input_id=:id"),
            {"id": input_id},
        )
        .mappings()
        .first()
    )
    if old is not None:
        if (
            old["channel_id"] != channel
            or old["intent"].get("kind") != "FAULT_CAPTURE"
            or old["intent"].get("input") != stable_input
        ):
            raise AlarmConflict("Fault capture identity already has different immutable intent")
        return StoredAlarmResult.model_validate(old["receipt"]).model_copy(
            update={"replayed": True, "notify_current": False}
        )
    # _lock_stream returns a row after line/source locks in the ingestion service.
    if (
        (source["line_id"], source["gateway_id"], int(source["capture_generation"]))
        != (context.scope.line_id, batch.gateway_id, batch.capture_generation)
        or source["conflicted"]
        or source["reconciliation_required"]
    ):
        raise AlarmConflict("Fault source requires exact binding and reconciliation")
    original = (
        db.execute(
            text(
                "SELECT content_sha256,body FROM ingest_batches WHERE gateway_id=:g "
                "AND batch_id=:b AND source_id=:s AND stream_id=:t "
                "AND capture_generation=:generation "
                "AND capture_seq=:seq"
            ),
            {
                "g": batch.gateway_id,
                "b": batch.batch_id,
                "s": batch.source_id,
                "t": batch.stream_id,
                "generation": batch.capture_generation,
                "seq": batch.capture_seq,
            },
        )
        .mappings()
        .first()
    )
    if (
        original is None
        or original["content_sha256"] != batch_digest(batch)
        or original["body"] != batch.model_dump(mode="json")
    ):
        raise AlarmConflict("Fault evidence must reference an exact ingested original capture")
    row = _row(db, channel)
    selected = _config(db, channel, context.rule.version)
    if not isinstance(selected, PLCFaultRuleConfig):
        raise AlarmConflict("Fault capture requires installed digital config")
    if (
        forced_unknown_reason == "CONFIG_INTERVAL_UNKNOWN"
        and context.rule.version != row["current_version"]
    ):
        raise AlarmConflict("Ambiguous config requires the locked current installed fault owner")
    state = _load(db, channel, row)
    checkpoint = _decode_checkpoint(row["qualification"])
    if not isinstance(checkpoint, FaultCheckpoint):
        raise AlarmConflict("Digital fault checkpoint required")
    if state.last_observed_at is not None and (
        checkpoint.last_known_at is None or checkpoint.last_known_at < state.last_observed_at
    ):
        checkpoint = checkpoint.model_copy(update={"last_known_at": state.last_observed_at})
    if state.last_position is not None and context.position < state.last_position:
        context = context.model_copy(update={"classification": "HISTORICAL"})
    verified = int(
        db.execute(
            text(
                "SELECT verified_through_seq FROM stream_progress "
                "WHERE source_id=:s AND stream_id=:t"
            ),
            {"s": batch.source_id, "t": batch.stream_id},
        ).scalar_one()
    )
    samples = {sample.tag_id: sample for sample in batch.samples}
    sample = samples.get("Line.EventSeq")
    ceiling = sample.reading.value if sample is not None and sample.status_code >> 30 == 0 else None
    events = []
    if ceiling is not None:
        latest = (
            db.execute(
                text(
                    "SELECT body,seq FROM event_inbox WHERE source_id=:s AND stream_id=:t "
                    "AND kind='FAULT_CHANGED' AND applied_at IS NOT NULL "
                    "AND body->>'equipment_id'='Line' "
                    "AND seq<=:ceiling ORDER BY seq DESC LIMIT 1"
                ),
                {"s": batch.source_id, "t": batch.stream_id, "ceiling": min(ceiling, verified)},
            )
            .mappings()
            .first()
        )
        if latest is not None:
            if not latest["body"]["payload"]["active"]:
                activation = db.execute(
                    text(
                        "SELECT body FROM event_inbox WHERE source_id=:s AND stream_id=:t "
                        "AND seq<:seq AND kind='FAULT_CHANGED' AND applied_at IS NOT NULL "
                        "AND body->>'equipment_id'='Line' ORDER BY seq DESC LIMIT 1"
                    ),
                    {"s": batch.source_id, "t": batch.stream_id, "seq": int(latest["seq"])},
                ).scalar_one_or_none()
                if activation is not None:
                    events.append(FaultEvent.model_validate(activation))
            previous = db.execute(
                text(
                    "SELECT body FROM event_inbox WHERE source_id=:s AND stream_id=:t "
                    "AND seq=:seq AND kind='STATE_CHANGED' AND applied_at IS NOT NULL "
                    "AND body->>'equipment_id'='Line'"
                ),
                {"s": batch.source_id, "t": batch.stream_id, "seq": int(latest["seq"]) - 1},
            ).scalar_one_or_none()
            if previous is not None:
                events.append(StateEvent.model_validate(previous))
            events.append(FaultEvent.model_validate(latest["body"]))
    observation = normalize_fault_capture(
        batch,
        selected,
        context,
        events=tuple(events),
        verified_through_event_seq=verified,
        forced_unknown_reason=forced_unknown_reason,
    )
    result = qualify_fault(checkpoint, observation)
    event = fault_condition(result, new_alarm_id=new_alarm_id)
    transition = apply_alarm(state, event)
    # Persist the original derived event/verification proof along with the stable
    # input. Later replay never regenerates it from a newer verification frontier.
    intent = {
        "kind": "FAULT_CAPTURE",
        "input": stable_input,
        "observation": observation.model_dump(mode="json"),
    }
    return _persist(
        db,
        channel,
        state,
        transition,
        result.state,
        intent,
        # Historical decisions must not reserialize a legacy checkpoint: new
        # default fields could otherwise change its stored raw JSON.
        preserve_checkpoint=result.disposition == "HISTORY_ONLY",
    )
