"""C1: cancel only unbound work orders, preserving every execution and original ID."""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, text

from apps.api.auth import Principal
from apps.api.database import SUPPORTED_CANCELLATION_REVISIONS
from apps.api.work_order_workflow import _event, _order_lock, _version, _workflow_ready
from apps.api.work_orders import (
    RegistrationConflict,
    RegistrationNotFound,
    WorkOrdersUnavailable,
    _lock,
)
from contracts.conflict_codes import ConflictReason
from contracts.models import ContractModel, Identifier, PositiveUInt64, UTCTimestamp


class CancellationIntent(ContractModel):
    cancellation_id: Identifier
    expected_row_version: PositiveUInt64


class OriginalCancellationIntent(CancellationIntent):
    work_order_id: Identifier
    actor: Identifier


class CancellationReceipt(ContractModel):
    cancellation_id: Identifier
    work_order_id: Identifier
    cancelled_by: Identifier
    cancelled_at: UTCTimestamp
    status: Literal["CANCELLED"]
    row_version: PositiveUInt64


class CancellationRecovery(ContractModel):
    intent: OriginalCancellationIntent
    receipt: CancellationReceipt


def _ready(db, principal, permission):
    _workflow_ready(db, principal, permission)
    revisions = db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(revisions) != 1 or revisions[0] not in SUPPORTED_CANCELLATION_REVISIONS:
        raise WorkOrdersUnavailable("Cancellation schema required")


def _validate(row, identity, cancellation_id):
    result = {"intent": row["original_intent"], "receipt": row["receipt"]}
    try:
        recovery = CancellationRecovery.model_validate(result)
        original, receipt = recovery.intent, recovery.receipt
        coherent = (
            original.work_order_id == receipt.work_order_id == row["work_order_id"] == identity
            and original.cancellation_id == receipt.cancellation_id == cancellation_id
            and original.actor == receipt.cancelled_by == row["actor"]
            and original.expected_row_version == int(row["expected_row_version"])
            and receipt.row_version == original.expected_row_version + 1
            and receipt.cancelled_at == row["cancelled_at"]
        )
    except (ValidationError, KeyError, TypeError, ValueError):
        coherent = False
    if not coherent:
        raise WorkOrdersUnavailable("Original cancellation receipt unavailable")
    return result


def cancel_work_order(
    db: Connection,
    identity: str,
    definition: CancellationIntent,
    *,
    principal: Principal,
    clock: Callable[[], datetime],
) -> dict:
    identity = TypeAdapter(Identifier).validate_python(identity)
    definition = CancellationIntent.model_validate_json(definition.model_dump_json())
    _ready(db, principal, "production_manage")
    original = {
        "work_order_id": identity,
        "actor": principal.login,
        **definition.model_dump(mode="json"),
    }
    _lock(db, json.dumps(["mes-cancel", definition.cancellation_id], separators=(",", ":")))
    previous = (
        db.execute(
            text("SELECT * FROM work_order_cancellations WHERE cancellation_id=:id"),
            {"id": definition.cancellation_id},
        )
        .mappings()
        .first()
    )
    if previous is not None:
        if previous["original_intent"] != original:
            raise RegistrationConflict("Cancellation identity belongs to another intent")
        return _validate(previous, identity, definition.cancellation_id)["receipt"]
    _order_lock(db, identity)
    order = (
        db.execute(text("SELECT * FROM work_orders WHERE id=:id FOR UPDATE"), {"id": identity})
        .mappings()
        .one()
    )
    if db.execute(
        text("SELECT 1 FROM work_order_execution_links WHERE work_order_id=:id"), {"id": identity}
    ).first():
        raise RegistrationConflict(
            "Bound execution cancellation is not implemented",
            reason_code=ConflictReason.WORK_ORDER_ALREADY_BOUND,
        )
    if order["status"] not in {"DRAFT", "RELEASED"}:
        raise RegistrationConflict(
            "Cancellation requires the original unbound work-order version",
            reason_code=ConflictReason.STATE_NOT_ALLOWED,
        )
    if int(order["row_version"]) != definition.expected_row_version:
        raise RegistrationConflict(
            "Cancellation requires the original unbound work-order version",
            reason_code=ConflictReason.WORK_ORDER_VERSION_CHANGED,
        )
    version = _version(order)
    now = TypeAdapter(UTCTimestamp).validate_python(clock())
    receipt = {
        "cancellation_id": definition.cancellation_id,
        "work_order_id": identity,
        "cancelled_by": principal.login,
        "cancelled_at": now.isoformat(),
        "status": "CANCELLED",
        "row_version": str(version),
    }
    db.execute(
        text("UPDATE work_orders SET status='CANCELLED',row_version=:v WHERE id=:id"),
        {"id": identity, "v": version},
    )
    event_id = _event(
        db,
        identity,
        principal.login,
        "CANCEL_WORK_ORDER",
        now,
        {"intent": original, "receipt": receipt},
    )
    db.execute(
        text(
            "INSERT INTO work_order_cancellations(cancellation_id,work_order_id,actor,"
            "expected_row_version,original_intent,receipt,cancel_event_id,cancelled_at) "
            "VALUES(:id,:order,:actor,:version,CAST(:intent AS jsonb),CAST(:receipt AS jsonb),"
            ":event,:now)"
        ),
        {
            "id": definition.cancellation_id,
            "order": identity,
            "actor": principal.login,
            "version": definition.expected_row_version,
            "intent": json.dumps(original),
            "receipt": json.dumps(receipt),
            "event": event_id,
            "now": now,
        },
    )
    return receipt


def recover_cancellation(
    db: Connection, identity: str, cancellation_id: str, *, principal: Principal
) -> dict:
    identity = TypeAdapter(Identifier).validate_python(identity)
    cancellation_id = TypeAdapter(Identifier).validate_python(cancellation_id)
    _ready(db, principal, "read")
    row = (
        db.execute(
            text(
                "SELECT * FROM work_order_cancellations WHERE cancellation_id=:id "
                "AND work_order_id=:order"
            ),
            {"id": cancellation_id, "order": identity},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RegistrationNotFound("Cancellation not found in exact work order")
    return _validate(row, identity, cancellation_id)
