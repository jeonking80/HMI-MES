"""In-place edit of unbound work orders, plus read-only order overview and history.

Content goes to the PLC only when an order is bound to an execution, so an unbound
order (DRAFT or RELEASED, not queued) is edited in place. Every edit increments the row
version, so a bind or queue entry prepared from older content is refused, and the
before/after values stay in the MES audit trail. Bound orders are records and never change.
"""

import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated

from pydantic import BeforeValidator, Field, TypeAdapter
from sqlalchemy import Connection, text

from apps.api.auth import Principal
from apps.api.work_order_workflow import _event, _order_lock, _version, _workflow_ready
from apps.api.work_orders import (
    RegistrationConflict,
    RegistrationNotFound,
    WorkOrdersUnavailable,
    _date,
    _lock,
)
from contracts.conflict_codes import ConflictReason
from contracts.models import ContractModel, Identifier, PositiveUInt64, UTCTimestamp

EDIT_REVISIONS = frozenset({"0025_mes_edit_audit", "0026_queue_reenroll"})
_ID = TypeAdapter(Identifier)
_FIELDS = ("recipe_version_id", "target_quantity", "planned_date")


class EditIntent(ContractModel):
    edit_id: Identifier
    expected_row_version: PositiveUInt64
    recipe_version_id: Identifier
    target_quantity: Annotated[int, Field(strict=True, gt=0, le=2**32 - 1)]
    planned_date: Annotated[date, BeforeValidator(_date)]


def _content(row) -> dict:
    return {
        "recipe_version_id": row["recipe_version_id"],
        "target_quantity": int(row["target_quantity"]),
        "planned_date": row["planned_date"].isoformat(),
    }


def edit_work_order(
    db: Connection,
    identity: str,
    definition: EditIntent,
    *,
    principal: Principal,
    clock: Callable[[], datetime],
) -> dict:
    identity = _ID.validate_python(identity)
    definition = EditIntent.model_validate_json(definition.model_dump_json())
    _workflow_ready(db, principal, "production_manage")
    if db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() not in (
        EDIT_REVISIONS
    ):
        raise WorkOrdersUnavailable("Edit audit schema required")
    after = {
        "recipe_version_id": definition.recipe_version_id,
        "target_quantity": definition.target_quantity,
        "planned_date": definition.planned_date.isoformat(),
    }
    _lock(db, json.dumps(["mes-edit", identity, definition.edit_id], separators=(",", ":")))
    previous = db.execute(
        text(
            "SELECT evidence FROM mes_management_audit WHERE action='EDIT_WORK_ORDER' "
            "AND work_order_id=:id AND evidence->>'edit_id'=:edit"
        ),
        {"id": identity, "edit": definition.edit_id},
    ).scalar_one_or_none()
    if previous is not None:
        # Exact replay of an applied edit returns its original receipt.
        if previous["after"] != after or previous["expected_row_version"] != str(
            definition.expected_row_version
        ):
            raise RegistrationConflict("Edit identity belongs to another intent")
        return previous
    _order_lock(db, identity)
    order = (
        db.execute(text("SELECT * FROM work_orders WHERE id=:id FOR UPDATE"), {"id": identity})
        .mappings()
        .one()
    )
    if (
        order["status"] not in {"DRAFT", "RELEASED"}
        or db.execute(
            text("SELECT 1 FROM work_order_execution_links WHERE work_order_id=:id"),
            {"id": identity},
        ).first()
    ):
        raise RegistrationConflict(
            "Only unbound work orders can be edited",
            reason_code=ConflictReason.STATE_NOT_ALLOWED,
        )
    if db.execute(
        text("SELECT 1 FROM line_queue_entries WHERE work_order_id=:id AND state='WAITING'"),
        {"id": identity},
    ).first():
        raise RegistrationConflict(
            "Queued work order must leave the queue first",
            reason_code=ConflictReason.STATE_NOT_ALLOWED,
        )
    if int(order["row_version"]) != definition.expected_row_version:
        raise RegistrationConflict(
            "Edit requires the current work-order version",
            reason_code=ConflictReason.WORK_ORDER_VERSION_CHANGED,
        )
    recipe_item = db.execute(
        text("SELECT item_id FROM recipe_versions WHERE id=:id"),
        {"id": definition.recipe_version_id},
    ).scalar_one_or_none()
    if recipe_item is None:
        raise RegistrationNotFound("Registered recipe version required")
    if recipe_item != order["item_id"]:
        raise RegistrationConflict("Recipe version belongs to another item")
    before = _content(order)
    if before == after:
        raise RegistrationConflict("Edit changes nothing")
    version = _version(order)
    now = TypeAdapter(UTCTimestamp).validate_python(clock())
    db.execute(
        text(
            "UPDATE work_orders SET recipe_version_id=:recipe,target_quantity=:target,"
            "planned_date=:date,row_version=:version WHERE id=:id"
        ),
        {
            "id": identity,
            "recipe": definition.recipe_version_id,
            "target": definition.target_quantity,
            "date": definition.planned_date,
            "version": version,
        },
    )
    receipt = {
        "edit_id": definition.edit_id,
        "work_order_id": identity,
        "edited_by": principal.login,
        "edited_at": now.isoformat(),
        "expected_row_version": str(definition.expected_row_version),
        "row_version": str(version),
        "before": before,
        "after": after,
    }
    _event(db, identity, principal.login, "EDIT_WORK_ORDER", now, receipt)
    return receipt


_OVERVIEW = """
SELECT w.id,w.item_id,w.line_id,w.recipe_version_id,w.target_quantity,w.planned_date,
 w.status,w.row_version,w.created_by,w.created_at,
 rel.released_at,rel.actor AS released_by,
 can.cancellation_id,can.cancelled_at,can.actor AS cancelled_by,
 l.execution_id,l.source_id,l.stream_id,l.bound_at,l.bound_by,
 r.status AS settlement_status,r.final_summary,
 t.good_quantity,t.reject_quantity,
 q.position AS queue_position
FROM work_orders w
LEFT JOIN work_order_releases rel ON rel.work_order_id=w.id
LEFT JOIN work_order_cancellations can ON can.work_order_id=w.id
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
    # Every actionable order (draft, or released and not yet bound) is listed however old;
    # finished and cancelled history fills the rest up to the 200 most recent.
    actionable = "(w.status='DRAFT' OR (w.status='RELEASED' AND l.execution_id IS NULL))"
    rows = (
        db.execute(
            text(
                _OVERVIEW + " WHERE w.line_id=:line AND " + actionable + " UNION ALL "
                "("
                + _OVERVIEW
                + " WHERE w.line_id=:line AND NOT "
                + actionable
                + ' ORDER BY w.created_at DESC, w.id COLLATE "C" LIMIT 200)'
            ),
            {"line": line_id},
        )
        .mappings()
        .all()
    )
    rows = sorted(rows, key=lambda row: row["id"])
    rows = sorted(rows, key=lambda row: row["created_at"], reverse=True)
    return {"items": [_summary(row) for row in rows]}


def _changes(evidence) -> list[dict]:
    before, after = evidence.get("before", {}), evidence.get("after", {})
    return [
        {"field": field, "before": before.get(field), "after": after.get(field)}
        for field in _FIELDS
        if before.get(field) != after.get(field)
    ]


def work_order_history(db: Connection, identity: str, *, principal: Principal) -> dict:
    identity = _ID.validate_python(identity)
    _workflow_ready(db, principal, "read")
    row = db.execute(text(_OVERVIEW + " WHERE w.id=:id"), {"id": identity}).mappings().first()
    if row is None:
        raise RegistrationNotFound("Work order not found")
    events = (
        db.execute(
            text(
                "SELECT action,actor,occurred_at,evidence FROM mes_management_audit "
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
                "changes": _changes(event["evidence"])
                if event["action"] == "EDIT_WORK_ORDER"
                else None,
            }
            for event in events
        ],
    }
