"""Authenticated line queue reads and explicit, protected queue updates."""

import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from apps.api.auth import AuthorizationError, UserAuth
from apps.api.commands import CommandConflict, CommandScopeError
from apps.api.control_http import CommandPreflightConflict
from apps.api.line_queue import QueueUpdate, read_queue, update_queue
from apps.api.work_orders import RegistrationConflict, RegistrationNotFound, WorkOrdersUnavailable
from contracts.models import Identifier


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def mount_line_queue_routes(
    app: FastAPI, auth: UserAuth, *, session_cookie: str = "hmi_session"
) -> None:
    def scope(request, line_id):
        if request.query_params:
            raise HTTPException(422, "queue does not accept query parameters")
        try:
            return TypeAdapter(Identifier).validate_python(line_id)
        except ValidationError:
            raise HTTPException(422, "invalid queue line identity") from None

    async def mapped(operation):
        try:
            return await run_in_threadpool(operation)
        except (
            RegistrationConflict,
            CommandConflict,
            CommandScopeError,
            CommandPreflightConflict,
        ) as error:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "queue conflicts with current state or original intent",
                    "reason_code": error.reason_code,
                },
            )
        except RegistrationNotFound:
            raise HTTPException(404, "queue line or work order not found") from None
        except (WorkOrdersUnavailable, SQLAlchemyError):
            raise HTTPException(503, "line queue unavailable") from None

    @app.get("/api/v1/lines/{line_id}/queue")
    async def read(request: Request, line_id: str):
        line_id = scope(request, line_id)

        def load():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return read_queue(tx.connection, line_id)

        return await mapped(load)

    @app.put("/api/v1/lines/{line_id}/queue")
    async def update(request: Request, line_id: str):
        line_id = scope(request, line_id)
        if (
            len(request.headers.getlist("content-type")) != 1
            or request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "JSON is required")
        if (
            len(request.headers.getlist("origin")) != 1
            or len(request.headers.getlist("x-csrf-token")) != 1
        ):
            raise AuthorizationError("queue mutation protection required")
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, "queue request too large")
        try:
            json.loads(raw, object_pairs_hook=_unique_object)
            intent = QueueUpdate.model_validate_json(raw)
        except (ValueError, RecursionError):
            raise HTTPException(422, "invalid queue intent") from None

        def save():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie),
                permission="production_manage",
                csrf_token=request.headers.get("x-csrf-token"),
                origin=request.headers.get("origin"),
            ) as tx:
                return update_queue(
                    tx.connection, line_id, intent, principal=tx.principal, clock=auth._now
                )

        return await mapped(save)
