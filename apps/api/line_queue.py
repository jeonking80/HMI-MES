"""Explicit, audited line queues; automatic advancement only journals managed LOAD_JOB."""

import json
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, TypeAdapter, model_validator
from sqlalchemy import Connection, text

from apps.api.auth import PERMISSIONS, AuthorizationError, Principal
from apps.api.capture_clock import schema_revision
from apps.api.commands import CommandConflict
from apps.api.control_http import _configured
from apps.api.database import SCHEMA_LOCK_ID, SUPPORTED_LINE_QUEUE_REVISIONS
from apps.api.settlement import reconcile_execution
from apps.api.snapshot import SnapshotNotFound, read_snapshot
from apps.api.work_order_workflow import BindIntent, _order_lock, bind_work_order
from apps.api.work_orders import (
    RegistrationConflict,
    RegistrationNotFound,
    WorkOrdersUnavailable,
    _lock,
)
from contracts.models import (
    UINT64_MAX,
    ContractModel,
    Identifier,
    PositiveUInt64,
    StrictBool,
    UInt64,
    UTCTimestamp,
)

REENROLL_REVISIONS = frozenset({"0026_queue_reenroll"})

QUEUE_LOCK = "mes-line-queue"


class QueueOrder(ContractModel):
    work_order_id: Identifier
    expected_row_version: PositiveUInt64


class QueueUpdate(ContractModel):
    request_id: Identifier
    expected_revision: UInt64
    source_id: Identifier
    stream_id: Identifier
    auto_advance: StrictBool
    entries: tuple[QueueOrder, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_orders(self):
        ids = [entry.work_order_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("Queue orders must be unique")
        return self


def _schema(db):
    if db.dialect.name != "postgresql" or not db.in_transaction():
        raise WorkOrdersUnavailable("Caller-owned PostgreSQL transaction required")
    db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID})
    revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    return revision in SUPPORTED_LINE_QUEUE_REVISIONS


def _policy(db, line_id):
    return (
        db.execute(text("SELECT * FROM line_queue_policies WHERE line_id=:line"), {"line": line_id})
        .mappings()
        .first()
    )


def _entries(db, line_id):
    return (
        db.execute(
            text(
                "SELECT * FROM line_queue_entries WHERE line_id=:line "
                "AND state<>'REMOVED' ORDER BY position,entry_id"
            ),
            {"line": line_id},
        )
        .mappings()
        .all()
    )


def read_queue(db: Connection, line_id: str) -> dict:
    line_id = TypeAdapter(Identifier).validate_python(line_id)
    if not _schema(db):
        raise WorkOrdersUnavailable("Line queue schema required")
    if not db.execute(text("SELECT 1 FROM lines WHERE id=:line"), {"line": line_id}).first():
        raise RegistrationNotFound("Line not found")
    policy = _policy(db, line_id)
    result = {
        "line_id": line_id,
        "revision": "0" if policy is None else str(policy["revision"]),
        "auto_advance": False if policy is None else policy["auto_advance"],
    }
    for name in (
        "source_id",
        "stream_id",
        "gateway_id",
        "capture_generation",
        "enabled_by",
        "blocked_reason",
    ):
        value = None if policy is None else policy[name]
        result[name] = str(value) if value is not None and name == "capture_generation" else value
    result["entries"] = [
        {
            **{
                name: row[name]
                for name in (
                    "entry_id",
                    "position",
                    "work_order_id",
                    "load_command_id",
                    "state",
                    "binding_receipt",
                )
            },
            "expected_row_version": str(row["expected_row_version"]),
        }
        for row in _entries(db, line_id)
    ]
    return result


def _audit(db, line_id, actor, action, now, evidence):
    db.execute(
        text(
            "INSERT INTO line_queue_audit(line_id,actor,action,occurred_at,evidence) "
            "VALUES(:line,:actor,:action,:now,CAST(:evidence AS jsonb))"
        ),
        {
            "line": line_id,
            "actor": actor,
            "action": action,
            "now": now,
            "evidence": json.dumps(evidence),
        },
    )


def _current_principal(db, login):
    user = (
        db.execute(
            text("SELECT active,must_change_password FROM users WHERE login=:login FOR SHARE"),
            {"login": login},
        )
        .mappings()
        .first()
    )
    roles = frozenset(
        db.execute(
            text("SELECT role FROM user_roles WHERE login=:login FOR SHARE"), {"login": login}
        ).scalars()
    )
    if user is None or not user["active"] or user["must_change_password"]:
        return None
    if not roles & PERMISSIONS["production_manage"] or not roles & PERMISSIONS["load_job"]:
        return None
    return Principal(login, roles, False)


def update_queue(
    db: Connection,
    line_id: str,
    intent: QueueUpdate,
    *,
    principal: Principal,
    clock: Callable[[], datetime],
) -> dict:
    line_id = TypeAdapter(Identifier).validate_python(line_id)
    intent = QueueUpdate.model_validate_json(intent.model_dump_json())
    if principal.must_change_password or not principal.roles & PERMISSIONS["production_manage"]:
        raise AuthorizationError("Queue management permission denied")
    if not _schema(db):
        raise WorkOrdersUnavailable("Line queue schema required")
    if _current_principal(db, principal.login) is None:
        raise AuthorizationError("Current queue management permission required")
    _lock(db, QUEUE_LOCK)
    original = {"line_id": line_id, "actor": principal.login, **intent.model_dump(mode="json")}
    previous = (
        db.execute(
            text("SELECT * FROM line_queue_updates WHERE request_id=:id"), {"id": intent.request_id}
        )
        .mappings()
        .first()
    )
    if previous is not None:
        if previous["original_intent"] != original:
            raise RegistrationConflict("Queue request belongs to another intent")
        return previous["receipt"]
    policy = _policy(db, line_id)
    revision = 0 if policy is None else int(policy["revision"])
    if revision != intent.expected_revision or revision == UINT64_MAX:
        raise RegistrationConflict("Queue revision changed")
    # Work-order advisory locks precede the line/source locks used by manual binding.
    orders = {}
    for entry in sorted(intent.entries, key=lambda item: item.work_order_id):
        _lock(db, json.dumps(["mes-work-order", entry.work_order_id], separators=(",", ":")))
    for entry in sorted(intent.entries, key=lambda item: item.work_order_id):
        orders[entry.work_order_id] = _order_lock(db, entry.work_order_id, line=line_id)
    scope = SimpleNamespace(line_id=line_id, source_id=intent.source_id, stream_id=intent.stream_id)
    source = _configured(db, scope, lock=True)
    for entry in intent.entries:
        order = orders[entry.work_order_id]
        linked = db.execute(
            text("SELECT 1 FROM work_order_execution_links WHERE work_order_id=:id"),
            {"id": entry.work_order_id},
        ).first()
        if order["status"] != "RELEASED" or linked:
            raise RegistrationConflict("Queue enrollment requires an unbound released order")
        if int(order["row_version"]) != entry.expected_row_version:
            raise RegistrationConflict("Queued work-order version changed")
    now = TypeAdapter(UTCTimestamp).validate_python(clock())
    db.execute(
        text(
            "INSERT INTO line_queue_policies(line_id,revision,source_id,stream_id,gateway_id,"
            "capture_generation,auto_advance,enabled_by,updated_at,blocked_reason) VALUES "
            "(:line,:revision,:source,:stream,:gateway,:generation,:auto,:actor,:now,NULL) "
            "ON CONFLICT(line_id) DO UPDATE SET revision=:revision,source_id=:source,"
            "stream_id=:stream,gateway_id=:gateway,capture_generation=:generation,"
            "auto_advance=:auto,enabled_by=:actor,updated_at=:now,blocked_reason=NULL"
        ),
        {
            "line": line_id,
            "revision": revision + 1,
            "source": intent.source_id,
            "stream": intent.stream_id,
            "gateway": source["gateway_id"],
            "generation": source["capture_generation"],
            "auto": intent.auto_advance,
            "actor": principal.login,
            "now": now,
        },
    )
    db.execute(
        text(
            "UPDATE line_queue_entries SET state='REMOVED' WHERE line_id=:line AND state='WAITING'"
        ),
        {"line": line_id},
    )
    reenroll = schema_revision(db) in REENROLL_REVISIONS
    for position, entry in enumerate(intent.entries):
        # Reuse the entry already enrolled at this exact version; an edited order gets a new
        # entry (its enrolled version is immutable evidence), which needs schema 0026.
        same = db.execute(
            text(
                "SELECT entry_id FROM line_queue_entries WHERE work_order_id=:order "
                "AND expected_row_version=:version AND state IN ('WAITING','REMOVED') "
                "ORDER BY created_at DESC,entry_id LIMIT 1"
            ),
            {"order": entry.work_order_id, "version": entry.expected_row_version},
        ).scalar_one_or_none()
        if same is not None:
            identity = same
        else:
            enrolled = db.execute(
                text("SELECT 1 FROM line_queue_entries WHERE work_order_id=:order LIMIT 1"),
                {"order": entry.work_order_id},
            ).first()
            if enrolled is None:
                key = json.dumps(["mes-queue", line_id, entry.work_order_id])
            elif reenroll:
                key = json.dumps(
                    ["mes-queue", line_id, entry.work_order_id, str(entry.expected_row_version)]
                )
            else:
                raise RegistrationConflict("Edited work order re-enrollment requires schema 0026")
            identity = str(uuid5(NAMESPACE_URL, key))
        load_id = str(uuid5(NAMESPACE_URL, "mes-queue-load:" + identity))
        db.execute(
            text(
                "INSERT INTO line_queue_entries(entry_id,line_id,position,work_order_id,"
                "expected_row_version,load_command_id,state,created_at) VALUES "
                "(:entry,:line,:position,:order,:version,:load,'WAITING',:now) "
                "ON CONFLICT(entry_id) DO UPDATE SET position=:position,state='WAITING' "
                "WHERE line_queue_entries.state='REMOVED'"
            ),
            {
                "entry": identity,
                "line": line_id,
                "position": position,
                "order": entry.work_order_id,
                "version": entry.expected_row_version,
                "load": load_id,
                "now": now,
            },
        )
    receipt = read_queue(db, line_id)
    _audit(
        db, line_id, principal.login, "UPDATE_QUEUE", now, {"intent": original, "receipt": receipt}
    )
    db.execute(
        text(
            "INSERT INTO line_queue_updates(request_id,line_id,actor,original_intent,receipt,"
            "created_at) VALUES(:id,:line,:actor,CAST(:intent AS jsonb),"
            "CAST(:receipt AS jsonb),:now)"
        ),
        {
            "id": intent.request_id,
            "line": line_id,
            "actor": principal.login,
            "intent": json.dumps(original),
            "receipt": json.dumps(receipt),
            "now": now,
        },
    )
    return receipt


def _block(db, policy, reason, now):
    db.execute(
        text(
            "UPDATE line_queue_policies SET auto_advance=false,blocked_reason=:reason,"
            "revision=LEAST(revision+1,18446744073709551615),updated_at=:now "
            "WHERE line_id=:line"
        ),
        {"line": policy["line_id"], "reason": reason, "now": now},
    )
    _audit(db, policy["line_id"], policy["enabled_by"], "BLOCK_QUEUE", now, {"reason": reason})


def _record_binding(db, policy, entry, receipt, now, *, manual=False):
    _wait_reason(db, policy, None)
    db.execute(
        text(
            "UPDATE line_queue_entries SET state='BOUND',binding_receipt=CAST(:receipt AS jsonb) "
            "WHERE entry_id=:entry AND state='WAITING'"
        ),
        {"entry": entry["entry_id"], "receipt": json.dumps(receipt)},
    )
    _audit(
        db,
        policy["line_id"],
        policy["enabled_by"],
        "OBSERVE_MANUAL_BINDING" if manual else "ADVANCE_QUEUE",
        now,
        {"entry_id": entry["entry_id"], "receipt": receipt},
    )


def _wait_reason(db, policy, reason):
    """Expose recoverable waits without changing the operator's queue policy."""
    db.execute(
        text(
            "UPDATE line_queue_policies SET blocked_reason=:reason WHERE line_id=:line "
            "AND blocked_reason IS DISTINCT FROM :reason"
        ),
        {"line": policy["line_id"], "reason": reason},
    )


def _safe_values(snapshot):
    if (
        snapshot.health.state != "CAPTURE_FRESH"
        or snapshot.health.heartbeat != "GOOD"
        or snapshot.health.source_conflicted
        or snapshot.health.reconciliation_required
        or snapshot.missing_tag_ids
        or any(not item.fresh for item in snapshot.evidence)
    ):
        return None, "FRESH_SOURCE_REQUIRED"
    samples = snapshot.screen.view.samples
    if any(sample.status_code >> 30 or sample.reading.value is None for sample in samples):
        return None, "FRESH_SOURCE_REQUIRED"
    values = {sample.tag_id: sample.reading.value for sample in samples}
    expected = {
        "Line.State": 0,
        "Line.WipCount": 0,
        "Line.ControlMode": 1,
        "Line.OperationMode": 1,
        "Line.EStop": False,
        "Line.GuardClosed": True,
        "Line.Permissive": True,
        "Line.FaultCode": 0,
        "Sealer.GuardClosed": True,
    }
    for group in ("Infeed", "Sealer", "Outfeed"):
        expected.update(
            {
                f"{group}.State": 0,
                f"{group}.RunFeedback": False,
                f"{group}.ActualSpeed": 0,
                f"{group}.FaultCode": 0,
            }
        )
    for name, value in expected.items():
        if values.get(name) != value:
            return None, "LOAD_CONDITION_NOT_MET:" + name
    # Sealer.Ready is a START condition. ENDED runs legitimately clear readiness
    # before the next managed LOAD establishes its recipe and warm-up state.
    return values, None


def advance_line_once(
    db: Connection,
    *,
    authenticated_gateway_id: str,
    authenticated_source_id: str,
    authenticated_stream_id: str,
    now: datetime,
) -> dict | None:
    """At most one durable LOAD; caller commits before the ordinary dispatch claim."""
    if not _schema(db):
        return None
    now = TypeAdapter(UTCTimestamp).validate_python(now)
    source = (
        db.execute(
            text("SELECT * FROM source_streams WHERE source_id=:source AND stream_id=:stream"),
            {"source": authenticated_source_id, "stream": authenticated_stream_id},
        )
        .mappings()
        .first()
    )
    if source is None or source["gateway_id"] != authenticated_gateway_id:
        return None
    policy = _policy(db, source["line_id"])
    if policy is None or not policy["auto_advance"]:
        return None
    principal = _current_principal(db, policy["enabled_by"])
    _lock(db, QUEUE_LOCK)
    current = _policy(db, source["line_id"])
    if current != policy:
        return None
    pins = ("gateway_id", "source_id", "stream_id", "capture_generation")
    if any(policy[name] != source[name] for name in pins):
        _block(db, policy, "SOURCE_CONFIGURATION_CHANGED", now)
        return None
    if principal is None:
        _block(db, policy, "ENABLER_PERMISSION_REVOKED", now)
        return None
    # Inspect one unresolved original LOAD in SQL, even after a long production history.
    command = (
        db.execute(
            text(
                "SELECT commands.status,commands.collision_latched FROM line_queue_entries entries "
                "LEFT JOIN command_requests commands ON "
                "commands.command_id=entries.binding_receipt->>'command_id' "
                "WHERE entries.line_id=:line AND entries.state='BOUND' AND "
                "(commands.status IS NULL OR commands.status<>'SUCCEEDED' "
                "OR commands.collision_latched) ORDER BY CASE WHEN commands.status IN "
                "('REQUESTED','DELIVERED','ACCEPTED') AND NOT commands.collision_latched "
                "THEN 1 ELSE 0 END LIMIT 1"
            ),
            {"line": source["line_id"]},
        )
        .mappings()
        .first()
    )
    if command is not None:
        if (
            command["status"] not in {"REQUESTED", "DELIVERED", "ACCEPTED"}
            or (command["collision_latched"])
        ):
            _block(db, policy, "ORIGINAL_LOAD_REQUIRES_HUMAN", now)
        else:
            _wait_reason(db, policy, "ORIGINAL_LOAD_PENDING")
        return None
    entry = (
        db.execute(
            text(
                "SELECT * FROM line_queue_entries WHERE line_id=:line AND state='WAITING' "
                "ORDER BY position,entry_id LIMIT 1"
            ),
            {"line": source["line_id"]},
        )
        .mappings()
        .first()
    )
    if entry is None:
        _wait_reason(db, policy, None)
        return None
    # Manual binding can consume an explicitly enrolled first order, with original evidence.
    link = (
        db.execute(
            text("SELECT * FROM work_order_execution_links WHERE work_order_id=:id"),
            {"id": entry["work_order_id"]},
        )
        .mappings()
        .first()
    )
    lock_id = entry["load_command_id"] if link is None else link["load_command_id"]
    _lock(db, "api-command:" + lock_id)
    order = _order_lock(db, entry["work_order_id"], line=policy["line_id"])
    scope = SimpleNamespace(
        line_id=policy["line_id"], source_id=policy["source_id"], stream_id=policy["stream_id"]
    )
    source = _configured(db, scope, lock=True)
    if any(policy[name] != source[name] for name in pins):
        _block(db, policy, "SOURCE_CONFIGURATION_CHANGED", now)
        return None
    # Re-read after binding locks, since manual bind/cancel does not take the queue lock.
    link = (
        db.execute(
            text("SELECT * FROM work_order_execution_links WHERE work_order_id=:id"),
            {"id": entry["work_order_id"]},
        )
        .mappings()
        .first()
    )
    if link is not None:
        command = (
            db.execute(
                text("SELECT status,collision_latched FROM command_requests WHERE command_id=:id"),
                {"id": link["load_command_id"]},
            )
            .mappings()
            .one()
        )
        coherent = all(link[name] == policy[name] for name in ("line_id", "source_id", "stream_id"))
        coherent = coherent and (
            int(link["original_intent"]["expected_work_order_version"])
            == int(entry["expected_row_version"])
        )
        if (
            not coherent
            or command["collision_latched"]
            or command["status"]
            in {
                "FAILED",
                "REJECTED",
                "EXPIRED",
                "OUTCOME_UNKNOWN",
            }
        ):
            _block(db, policy, "ORIGINAL_LOAD_REQUIRES_HUMAN", now)
        elif command["status"] == "SUCCEEDED":
            _record_binding(db, policy, entry, link["receipt"], now, manual=True)
        else:
            _wait_reason(db, policy, "ORIGINAL_LOAD_PENDING")
        return None
    if order["status"] != "RELEASED" or int(order["row_version"]) != int(
        entry["expected_row_version"]
    ):
        _block(db, policy, "QUEUE_HEAD_CHANGED", now)
        return None
    reserved = (
        db.execute(
            text(
                "SELECT links.* FROM line_execution_reservations slots JOIN "
                "work_order_execution_links links ON links.execution_id=slots.execution_id "
                "WHERE slots.line_id=:line"
            ),
            {"line": policy["line_id"]},
        )
        .mappings()
        .first()
    )
    if reserved is None:
        _wait_reason(db, policy, "PRIOR_EXECUTION_REQUIRED")
        return None
    if any(reserved[name] != policy[name] for name in ("line_id", "source_id", "stream_id")):
        _block(db, policy, "PRIOR_EXECUTION_SCOPE_CHANGED", now)
        return None
    settled = reconcile_execution(
        db,
        reserved["execution_id"],
        authenticated_gateway_id=authenticated_gateway_id,
        authenticated_source_id=authenticated_source_id,
        authenticated_stream_id=authenticated_stream_id,
        now=now,
    )
    if settled.status == "PENDING":
        _wait_reason(db, policy, "PRIOR_EXECUTION_SETTLEMENT_PENDING")
        return None
    if settled.status != "SETTLED":
        _block(db, policy, "PRIOR_EXECUTION_NOT_SETTLED", now)
        return None
    try:
        snapshot = read_snapshot(
            db,
            line_id=scope.line_id,
            source_id=scope.source_id,
            stream_id=scope.stream_id,
            now=now,
            prefer_complete_capture=True,
        )
    except SnapshotNotFound:
        _wait_reason(db, policy, "FRESH_SOURCE_REQUIRED")
        return None
    values, reason = _safe_values(snapshot)
    if values is None:
        _wait_reason(db, policy, reason)
        return None
    if values.get("Line.ActiveExecutionId") != reserved["execution_id"]:
        _block(db, policy, "PHYSICAL_EXECUTION_MISMATCH", now)
        return None
    intent = BindIntent(
        command_id=entry["load_command_id"],
        line_id=scope.line_id,
        source_id=scope.source_id,
        stream_id=scope.stream_id,
        expected_work_order_version=int(entry["expected_row_version"]),
        expected_control_version=values["Line.ControlVersion"],
        auto_end_at_target=True,
        start_when_ready=False,
    )
    try:
        with db.begin_nested():
            receipt = bind_work_order(
                db, entry["work_order_id"], intent, principal=principal, clock=lambda: now
            )
            _record_binding(db, policy, entry, receipt, now)
    except (RegistrationConflict, CommandConflict):
        _block(db, policy, "BINDING_REQUIRES_HUMAN", now)
        return None
    return receipt
