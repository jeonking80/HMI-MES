"""Work-order revision as cancel-and-replace, plus read-only order overview and history.

A released order is never edited in place: queue and PLC evidence keep pointing at the
original content. A revision cancels the unbound original and registers a new order in
one transaction. The link is the cancellation ID ``REVISE-<new id>``, which stays in the
original cancellation receipt, so no extra schema is needed.
"""

from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated

from pydantic import BeforeValidator, Field, TypeAdapter
from sqlalchemy import Connection, text

from apps.api.auth import Principal
from apps.api.work_order_cancel import CancellationIntent, cancel_work_order
from apps.api.work_order_workflow import ReleaseIntent, _workflow_ready, release_work_order
from apps.api.work_orders import (
    RegistrationConflict,
    RegistrationNotFound,
    WorkOrderDefinition,
    _date,
    register_work_order,
)
from contracts.conflict_codes import ConflictReason
from contracts.models import ContractModel, Identifier, PositiveUInt64

REVISE_PREFIX = "REVISE-"
_ID = TypeAdapter(Identifier)


class RevisionIntent(ContractModel):
    new_work_order_id: Identifier
    recipe_version_id: Identifier
    target_quantity: Annotated[int, Field(strict=True, gt=0, le=2**32 - 1)]
    planned_date: Annotated[date, BeforeValidator(_date)]
    expected_row_version: PositiveUInt64
    release_id: Identifier


def _cancellation_id(new_id: str) -> str:
    value = REVISE_PREFIX + new_id
    if len(value) > 128:
        raise RegistrationConflict("Revised work-order ID is too long")
    return value


def revise_work_order(
    db: Connection,
    identity: str,
    definition: RevisionIntent,
    *,
    principal: Principal,
    clock: Callable[[], datetime],
) -> dict:
    identity = _ID.validate_python(identity)
    definition = RevisionIntent.model_validate_json(definition.model_dump_json())
    _workflow_ready(db, principal, "production_manage")
    if definition.new_work_order_id == identity:
        raise RegistrationConflict("Revision requires a new work-order ID")
    original = (
        db.execute(text("SELECT * FROM work_orders WHERE id=:id"), {"id": identity})
        .mappings()
        .first()
    )
    if original is None:
        raise RegistrationNotFound("Work order not found")
    if db.execute(
        text("SELECT 1 FROM line_queue_entries WHERE work_order_id=:id AND state='WAITING'"),
        {"id": identity},
    ).first():
        raise RegistrationConflict(
            "Queued work order must leave the queue first",
            reason_code=ConflictReason.STATE_NOT_ALLOWED,
        )
    released = (
        db.execute(
            text("SELECT 1 FROM work_order_releases WHERE work_order_id=:id"), {"id": identity}
        ).first()
        is not None
    )
    now = clock()
    # Cancellation is idempotent by its ID and refuses bound or changed orders.
    cancellation = cancel_work_order(
        db,
        identity,
        CancellationIntent(
            cancellation_id=_cancellation_id(definition.new_work_order_id),
            expected_row_version=definition.expected_row_version,
        ),
        principal=principal,
        clock=lambda: now,
    )
    order = register_work_order(
        db,
        WorkOrderDefinition(
            id=definition.new_work_order_id,
            item_id=original["item_id"],
            line_id=original["line_id"],
            recipe_version_id=definition.recipe_version_id,
            target_quantity=definition.target_quantity,
            planned_date=definition.planned_date,
        ),
        actor=principal.login,
        now=now,
    )
    release = None
    if released:
        release = release_work_order(
            db,
            order.id,
            ReleaseIntent(release_id=definition.release_id, expected_row_version=1),
            principal=principal,
            clock=lambda: now,
        )
    return {
        "original_work_order_id": identity,
        "work_order_id": order.id,
        "cancellation": cancellation,
        "release": release,
    }


_OVERVIEW = """
SELECT w.id,w.item_id,w.line_id,w.recipe_version_id,w.target_quantity,w.planned_date,
 w.status,w.row_version,w.created_by,w.created_at,
 rel.released_at,rel.actor AS released_by,
 can.cancellation_id,can.cancelled_at,can.actor AS cancelled_by,
 rev.work_order_id AS revised_from,
 l.execution_id,l.source_id,l.stream_id,l.bound_at,l.bound_by,
 r.status AS settlement_status,r.final_summary,
 t.good_quantity,t.reject_quantity,
 q.position AS queue_position
FROM work_orders w
LEFT JOIN work_order_releases rel ON rel.work_order_id=w.id
LEFT JOIN work_order_cancellations can ON can.work_order_id=w.id
LEFT JOIN work_order_cancellations rev ON rev.cancellation_id='REVISE-'||w.id
LEFT JOIN work_order_execution_links l ON l.work_order_id=w.id
LEFT JOIN run_reconciliations r ON r.execution_id=l.execution_id
LEFT JOIN execution_totals t ON t.execution_id=l.execution_id
LEFT JOIN line_queue_entries q ON q.work_order_id=w.id AND q.state='WAITING'
"""


def _phase(row) -> str:
    if row["status"] == "CANCELLED":
        return "CANCELLED"
    if row["execution_id"] is not None:
        settled = row["settlement_status"] not in {None, "PENDING"}
        return "DONE" if settled else "RUNNING"
    return "WAITING" if row["status"] == "RELEASED" else "DRAFT"


def _count(value):
    return None if value is None else int(value)


def _summary(row) -> dict:
    cancellation = row["cancellation_id"]
    final = row["final_summary"] or {}
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "line_id": row["line_id"],
        "recipe_version_id": row["recipe_version_id"],
        "target_quantity": int(row["target_quantity"]),
        "planned_date": row["planned_date"].isoformat(),
        "status": row["status"],
        "row_version": str(int(row["row_version"])),
        "phase": _phase(row),
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat(),
        "released_at": row["released_at"].isoformat() if row["released_at"] else None,
        "released_by": row["released_by"],
        "cancelled_at": row["cancelled_at"].isoformat() if row["cancelled_at"] else None,
        "cancelled_by": row["cancelled_by"],
        "revised_to": cancellation[len(REVISE_PREFIX) :]
        if cancellation and cancellation.startswith(REVISE_PREFIX)
        else None,
        "revised_from": row["revised_from"],
        "queue_position": row["queue_position"],
        "execution": None
        if row["execution_id"] is None
        else {
            "execution_id": row["execution_id"],
            "source_id": row["source_id"],
            "stream_id": row["stream_id"],
            "bound_at": row["bound_at"].isoformat(),
            "bound_by": row["bound_by"],
            # Unsettled counts are live totals, not a final result.
            "settlement_status": row["settlement_status"] or "PENDING",
            "good_quantity": _count(final.get("good_quantity", row["good_quantity"])),
            "reject_quantity": _count(final.get("reject_quantity", row["reject_quantity"])),
            "end_reason": final.get("end_reason"),
            "ended_at": final.get("ended_at"),
        },
    }


def work_order_overview(db: Connection, *, line_id: str, principal: Principal) -> dict:
    line_id = _ID.validate_python(line_id)
    _workflow_ready(db, principal, "read")
    rows = (
        db.execute(
            text(
                _OVERVIEW + ' WHERE w.line_id=:line ORDER BY w.created_at DESC, w.id COLLATE "C"'
                " LIMIT 200"
            ),
            {"line": line_id},
        )
        .mappings()
        .all()
    )
    return {"items": [_summary(row) for row in rows]}


def work_order_history(db: Connection, identity: str, *, principal: Principal) -> dict:
    identity = _ID.validate_python(identity)
    _workflow_ready(db, principal, "read")
    row = db.execute(text(_OVERVIEW + " WHERE w.id=:id"), {"id": identity}).mappings().first()
    if row is None:
        raise RegistrationNotFound("Work order not found")
    events = (
        db.execute(
            text(
                "SELECT action,actor,occurred_at FROM mes_management_audit "
                "WHERE work_order_id=:id ORDER BY occurred_at,id"
            ),
            {"id": identity},
        )
        .mappings()
        .all()
    )
    return {
        "order": _summary(row),
        "events": [
            {
                "action": event["action"],
                "actor": event["actor"],
                "occurred_at": event["occurred_at"].isoformat(),
            }
            for event in events
        ],
    }
