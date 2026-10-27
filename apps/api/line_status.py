"""One read of where a line is in its job cycle, for the operation control screen.

The screen combines this with its live tags; it never keeps its own copy of
which job is loaded, whether it settled, or whether a command is unresolved.
"""

from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, text
from starlette.concurrency import run_in_threadpool

from apps.api import runtime_control
from apps.api.auth import UserAuth
from contracts.models import Identifier

_ID = TypeAdapter(Identifier)

_LATEST = text("""
SELECT l.execution_id,l.work_order_id,l.bound_at,l.bound_by,
 l.original_intent->>'auto_end_at_target' AS auto_end,
 l.original_intent->>'start_when_ready' AS start_when_ready,
 p.target_quantity,r.status AS settlement,r.final_summary,
 t.good_quantity,t.reject_quantity
FROM work_order_execution_links l
LEFT JOIN execution_plans p ON p.execution_id=l.execution_id
LEFT JOIN run_reconciliations r ON r.execution_id=l.execution_id
LEFT JOIN execution_totals t ON t.execution_id=l.execution_id
WHERE l.line_id=:line AND l.source_id=:source AND l.stream_id=:stream
ORDER BY l.bound_at DESC, l.execution_id DESC LIMIT 1""")

# The same statuses the command intake treats as blocking a new start/job/settings.
_UNRESOLVED = text("""
SELECT command_id,kind,status FROM command_requests
WHERE line_id=:line AND (status IN ('REQUESTED','DELIVERED','ACCEPTED','OUTCOME_UNKNOWN')
 OR collision_latched)
ORDER BY created_at DESC LIMIT 1""")


def _flag(value):
    return None if value is None else value == "true"


def _count(value):
    return None if value is None else int(value)


def read_line_status(db: Connection, *, line_id, source_id, stream_id, now: datetime) -> dict:
    scope = {"line": line_id, "source": source_id, "stream": stream_id}
    managed = db.execute(text("SELECT to_regclass('runtime_ops.control')")).scalar() is not None
    power = runtime_control.view(db, now)["reported_state"] if managed else "NOT_MANAGED"
    row = db.execute(_LATEST, scope).mappings().first()
    execution = None
    if row is not None:
        final = row["final_summary"] or {}
        execution = {
            "execution_id": row["execution_id"],
            "work_order_id": row["work_order_id"],
            "bound_at": row["bound_at"].isoformat(),
            "bound_by": row["bound_by"],
            "target_quantity": _count(row["target_quantity"]),
            "auto_end": _flag(row["auto_end"]),
            "start_when_ready": _flag(row["start_when_ready"]),
            "settlement": row["settlement"] or "PENDING",
            "good_quantity": _count(final.get("good_quantity", row["good_quantity"])),
            "reject_quantity": _count(final.get("reject_quantity", row["reject_quantity"])),
            "end_reason": final.get("end_reason"),
            "ended_at": final.get("ended_at"),
        }
    unresolved = db.execute(_UNRESOLVED, scope).mappings().first()
    return {
        "power": power,
        "execution": execution,
        "unresolved_command": dict(unresolved) if unresolved else None,
    }


def mount_line_status_routes(app: FastAPI, auth: UserAuth, *, session_cookie="hmi_session"):
    names = {"line_id", "source_id", "stream_id"}

    @app.get("/api/v1/line-status")
    async def line_status(request: Request):
        if set(request.query_params) != names or any(
            len(request.query_params.getlist(name)) != 1 for name in names
        ):
            raise HTTPException(422, "line_id, source_id and stream_id are required")
        try:
            scope = {name: _ID.validate_python(request.query_params[name]) for name in names}
        except ValidationError:
            raise HTTPException(422, "invalid line scope") from None

        def perform():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return read_line_status(tx.connection, **scope, now=auth._now())

        return JSONResponse(await run_in_threadpool(perform), headers={"Cache-Control": "no-store"})
