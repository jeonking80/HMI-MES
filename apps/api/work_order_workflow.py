"""Release and binding on a caller-owned transaction; never perform external control."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, text

from apps.api.auth import PERMISSIONS, AuthorizationError, Principal
from apps.api.commands import enqueue_command
from apps.api.control_http import _configured, _preflight
from apps.api.database import SUPPORTED_WORKFLOW_REVISIONS
from apps.api.ingestion import IngestionService
from apps.api.settlement import reconcile_execution
from apps.api.snapshot import SnapshotNotFound, read_snapshot
from apps.api.work_orders import (
    RegistrationConflict,
    RegistrationNotFound,
    WorkOrdersUnavailable,
    _lock,
    _ready,
)
from contracts.models import (
    UINT64_MAX,
    CommandRequest,
    ContractModel,
    Identifier,
    Parameters,
    PositiveUInt64,
    UInt64,
    UTCTimestamp,
)


class ReleaseIntent(ContractModel):
    release_id: Identifier
    expected_row_version: PositiveUInt64


class BindIntent(ContractModel):
    command_id: Identifier
    line_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    expected_work_order_version: PositiveUInt64
    expected_control_version: UInt64


class OriginalReleaseIntent(ReleaseIntent):
    work_order_id: Identifier
    actor: Identifier


class OriginalBindingIntent(BindIntent):
    work_order_id: Identifier
    actor: Identifier


class ReleaseReceipt(ContractModel):
    release_id: Identifier
    work_order_id: Identifier
    released_by: Identifier
    released_at: UTCTimestamp
    status: Literal["RELEASED"]
    row_version: PositiveUInt64


class BindingReceipt(ContractModel):
    work_order_id: Identifier
    execution_id: Identifier
    command_id: Identifier
    line_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    work_order_row_version: PositiveUInt64
    load_request: CommandRequest


class ReleaseRecovery(ContractModel):
    intent: OriginalReleaseIntent
    receipt: ReleaseReceipt


class BindingRecovery(ContractModel):
    intent: OriginalBindingIntent
    receipt: BindingReceipt


def recover_work_order_receipt(
    db: Connection,
    identity: str,
    original_id: str,
    *,
    kind: Literal["release", "binding"],
    principal: Principal,
) -> dict:
    """Read committed original evidence without inspecting current operational state."""
    identity = TypeAdapter(Identifier).validate_python(identity)
    original_id = TypeAdapter(Identifier).validate_python(original_id)
    _workflow_ready(db, principal, "read")
    if kind == "release":
        statement = (
            "SELECT original_intent,receipt FROM work_order_releases "
            "WHERE work_order_id=:order AND release_id=:original"
        )
        model = ReleaseRecovery
    elif kind == "binding":
        statement = (
            "SELECT original_intent,receipt FROM work_order_execution_links "
            "WHERE work_order_id=:order AND load_command_id=:original"
        )
        model = BindingRecovery
    else:
        raise ValueError("Unknown recovery kind")
    row = (
        db.execute(text(statement), {"order": identity, "original": original_id}).mappings().first()
    )
    if row is None:
        raise RegistrationNotFound("Original workflow receipt not found")
    result = {"intent": row["original_intent"], "receipt": row["receipt"]}
    try:
        recovered = model.model_validate(result)
    except ValidationError:
        raise WorkOrdersUnavailable("Original workflow receipt unavailable") from None
    original, receipt = recovered.intent, recovered.receipt
    coherent = original.work_order_id == receipt.work_order_id == identity
    if kind == "release":
        coherent = coherent and (
            original.release_id == receipt.release_id == original_id
            and original.actor == receipt.released_by
            and receipt.row_version == original.expected_row_version + 1
        )
    else:
        request = receipt.load_request
        coherent = coherent and (
            original.command_id == receipt.command_id == request.command_id == original_id
            and original.actor == request.requested_by
            and receipt.execution_id == request.execution_id
            and original.expected_control_version == request.expected_control_version
            and receipt.work_order_row_version == original.expected_work_order_version + 1
            and all(
                getattr(original, key) == getattr(receipt, key)
                for key in ("line_id", "source_id", "stream_id")
            )
            and request.command.kind == "LOAD_JOB"
            and request.command.args.work_order_id == identity
        )
    if not coherent:
        raise WorkOrdersUnavailable("Original workflow receipt unavailable")
    # Validate shape but return the stored JSON unchanged, including original timestamp spelling.
    return result


def _workflow_ready(db, principal, permission):
    if principal.must_change_password or not principal.roles & PERMISSIONS[permission]:
        raise AuthorizationError("Workflow permission denied")
    _ready(db)
    revisions = db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(revisions) != 1 or revisions[0] not in SUPPORTED_WORKFLOW_REVISIONS:
        raise WorkOrdersUnavailable("Binding schema required")


def _intent(identity, definition, principal):
    return {
        "work_order_id": identity,
        "actor": principal.login,
        **definition.model_dump(mode="json"),
    }


def _order_lock(db, identity, *, line=None):
    _lock(db, json.dumps(["mes-work-order", identity], separators=(",", ":")))
    row = (
        db.execute(text("SELECT * FROM work_orders WHERE id=:id"), {"id": identity})
        .mappings()
        .first()
    )
    if row is None:
        raise RegistrationNotFound("Work order not found")
    if line is not None and row["line_id"] != line:
        raise RegistrationConflict("Work-order line differs")
    db.execute(text("SELECT id FROM lines WHERE id=:id FOR UPDATE"), {"id": row["line_id"]})
    return row


def _event(db, identity, actor, action, now, evidence):
    return db.execute(
        text(
            "INSERT INTO mes_management_audit(actor,action,work_order_id,occurred_at,evidence) "
            "VALUES (:actor,:action,:id,:now,CAST(:evidence AS jsonb)) RETURNING id"
        ),
        {
            "actor": actor,
            "action": action,
            "id": identity,
            "now": now,
            "evidence": json.dumps(evidence),
        },
    ).scalar_one()


def _version(row):
    version = int(row["row_version"])
    if version == UINT64_MAX:
        raise RegistrationConflict("Work-order version exhausted")
    return version + 1


def release_work_order(
    db: Connection,
    identity: str,
    definition: ReleaseIntent,
    *,
    principal: Principal,
    clock: Callable[[], datetime],
) -> dict:
    from pydantic import TypeAdapter

    identity = TypeAdapter(Identifier).validate_python(identity)
    definition = ReleaseIntent.model_validate_json(definition.model_dump_json())
    _workflow_ready(db, principal, "production_manage")
    original = _intent(identity, definition, principal)
    _lock(db, json.dumps(["mes-release", definition.release_id], separators=(",", ":")))
    previous = (
        db.execute(
            text("SELECT * FROM work_order_releases WHERE release_id=:id"),
            {"id": definition.release_id},
        )
        .mappings()
        .first()
    )
    if previous is not None:
        if previous["original_intent"] != original:
            raise RegistrationConflict("Release identity belongs to another intent")
        return previous["receipt"]
    _order_lock(db, identity)
    order = (
        db.execute(text("SELECT * FROM work_orders WHERE id=:id FOR UPDATE"), {"id": identity})
        .mappings()
        .one()
    )
    if order["status"] != "DRAFT" or int(order["row_version"]) != definition.expected_row_version:
        raise RegistrationConflict("Release requires the original draft version")
    version = _version(order)
    now = TypeAdapter(UTCTimestamp).validate_python(clock())
    receipt = {
        "release_id": definition.release_id,
        "work_order_id": identity,
        "released_by": principal.login,
        "released_at": now.isoformat(),
        "status": "RELEASED",
        "row_version": str(version),
    }
    db.execute(
        text("UPDATE work_orders SET status='RELEASED',row_version=:version WHERE id=:id"),
        {"id": identity, "version": version},
    )
    audit = _event(
        db,
        identity,
        principal.login,
        "RELEASE_WORK_ORDER",
        now,
        {"intent": original, "receipt": receipt},
    )
    db.execute(
        text(
            "INSERT INTO work_order_releases VALUES "
            "(:release,:order,:actor,:version,CAST(:intent AS jsonb),CAST(:receipt AS "
            "jsonb),:audit,:now)"
        ),
        {
            "release": definition.release_id,
            "order": identity,
            "actor": principal.login,
            "version": definition.expected_row_version,
            "intent": json.dumps(original),
            "receipt": json.dumps(receipt),
            "audit": audit,
            "now": now,
        },
    )
    return receipt


def bind_work_order(
    db: Connection,
    identity: str,
    definition: BindIntent,
    *,
    principal: Principal,
    clock: Callable[[], datetime],
) -> dict:
    from pydantic import TypeAdapter

    identity = TypeAdapter(Identifier).validate_python(identity)
    definition = BindIntent.model_validate_json(definition.model_dump_json())
    _workflow_ready(db, principal, "load_job")
    original = _intent(identity, definition, principal)
    _lock(db, "api-command:" + definition.command_id)
    previous = (
        db.execute(
            text(
                "SELECT original_intent,receipt FROM work_order_execution_links WHERE "
                "load_command_id=:id"
            ),
            {"id": definition.command_id},
        )
        .mappings()
        .first()
    )
    if previous is not None:
        if previous["original_intent"] != original:
            raise RegistrationConflict("Binding command belongs to another intent")
        return previous["receipt"]
    if db.execute(
        text("SELECT 1 FROM command_requests WHERE command_id=:id"), {"id": definition.command_id}
    ).first():
        raise RegistrationConflict("Existing command cannot be adopted as a managed binding")
    _order_lock(db, identity, line=definition.line_id)
    scope = SimpleNamespace(
        line_id=definition.line_id, source_id=definition.source_id, stream_id=definition.stream_id
    )
    source = _configured(db, scope, lock=True)
    order = (
        db.execute(text("SELECT * FROM work_orders WHERE id=:id FOR UPDATE"), {"id": identity})
        .mappings()
        .one()
    )
    if (
        order["status"] != "RELEASED"
        or int(order["row_version"]) != definition.expected_work_order_version
    ):
        raise RegistrationConflict("Binding requires the released work-order version")
    if db.execute(
        text("SELECT 1 FROM work_order_execution_links WHERE work_order_id=:id"), {"id": identity}
    ).first():
        raise RegistrationConflict("Work order already owns its original execution")
    version = _version(order)
    now = TypeAdapter(UTCTimestamp).validate_python(clock())
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
        raise RegistrationConflict("Fresh source evidence required") from None
    values = {sample.tag_id: sample.reading.value for sample in snapshot.screen.view.samples}
    try:
        epoch = TypeAdapter(Identifier).validate_python(values.get("Line.CounterEpoch"))
    except ValueError:
        raise RegistrationConflict("Known original counter epoch required") from None
    reserved = (
        db.execute(
            text(
                "SELECT links.* FROM line_execution_reservations slots JOIN "
                "work_order_execution_links links "
                "ON links.execution_id=slots.execution_id WHERE slots.line_id=:line"
            ),
            {"line": scope.line_id},
        )
        .mappings()
        .first()
    )
    if reserved is not None:
        if (reserved["source_id"], reserved["stream_id"]) != (scope.source_id, scope.stream_id):
            raise RegistrationConflict("Reserved execution belongs to another source")
        active = values.get("Line.ActiveExecutionId") or None
        if active is not None and active != reserved["execution_id"]:
            raise RegistrationConflict("Physical execution differs from the line reservation")
        settled = reconcile_execution(
            db,
            reserved["execution_id"],
            authenticated_gateway_id=source["gateway_id"],
            authenticated_source_id=reserved["source_id"],
            authenticated_stream_id=reserved["stream_id"],
            now=now,
        )
        if settled.status != "SETTLED":
            raise RegistrationConflict("Reserved execution is not settled")
    recipe = (
        db.execute(
            text("SELECT * FROM recipe_versions WHERE id=:id"), {"id": order["recipe_version_id"]}
        )
        .mappings()
        .one()
    )
    execution_id = str(uuid4())
    parameters = Parameters(
        temperature_c=recipe["temperature_c"], speed_m_per_min=recipe["speed_m_per_min"]
    )
    IngestionService(db.engine).register_execution_on_connection(
        db,
        execution_id=execution_id,
        source_id=scope.source_id,
        stream_id=scope.stream_id,
        work_order_id=identity,
        recipe_version=recipe["id"],
        parameters=parameters,
        counter_epoch=epoch,
        target_quantity=int(order["target_quantity"]),
        now=now,
    )
    request = CommandRequest(
        command_id=definition.command_id,
        target_id="Line",
        execution_id=execution_id,
        requested_by=principal.login,
        requested_at=now,
        expires_at=now + timedelta(seconds=5),
        expected_control_version=definition.expected_control_version,
        command={
            "kind": "LOAD_JOB",
            "args": {
                "work_order_id": identity,
                "recipe_version": recipe["id"],
                "target_quantity": int(order["target_quantity"]),
                "parameters": parameters.model_dump(),
            },
        },
    )
    scope.request = request
    _preflight(db, scope, source, now)
    # Removing a verified previous slot is provisional until the entire binding commits.
    if reserved is not None:
        db.execute(
            text("DELETE FROM line_execution_reservations WHERE line_id=:line"),
            {"line": scope.line_id},
        )
    enqueue_command(
        db,
        request,
        principal=principal,
        line_id=scope.line_id,
        source_id=scope.source_id,
        stream_id=scope.stream_id,
        now=now,
        _managed_binding=True,
    )
    receipt = {
        "work_order_id": identity,
        "execution_id": execution_id,
        "command_id": request.command_id,
        "line_id": scope.line_id,
        "source_id": scope.source_id,
        "stream_id": scope.stream_id,
        "work_order_row_version": str(version),
        "load_request": request.model_dump(mode="json"),
    }
    db.execute(
        text(
            "INSERT INTO "
            "work_order_execution_links(execution_id,work_order_id,line_id,source_id,stream_id,"
            "load_command_id,original_intent,original_request,receipt,bound_by,bound_at) VALUES "
            "(:exec,:order,:line,:source,:stream,:command,CAST(:intent AS "
            "jsonb),CAST(:request AS jsonb),"
            "CAST(:receipt AS jsonb),:actor,:now)"
        ),
        {
            "exec": execution_id,
            "order": identity,
            "line": scope.line_id,
            "source": scope.source_id,
            "stream": scope.stream_id,
            "command": request.command_id,
            "intent": json.dumps(original),
            "request": request.model_dump_json(),
            "receipt": json.dumps(receipt),
            "actor": principal.login,
            "now": now,
        },
    )
    db.execute(
        text("INSERT INTO line_execution_reservations VALUES (:line,:exec)"),
        {"line": scope.line_id, "exec": execution_id},
    )
    db.execute(
        text("UPDATE work_orders SET row_version=:version WHERE id=:id"),
        {"id": identity, "version": version},
    )
    _event(
        db,
        identity,
        principal.login,
        "BIND_EXECUTION",
        now,
        {"intent": original, "receipt": receipt},
    )
    return receipt
