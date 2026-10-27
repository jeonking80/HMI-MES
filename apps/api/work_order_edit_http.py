"""Authenticated in-place work-order edit, overview and history."""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from apps.api.auth import AuthorizationError, UserAuth
from apps.api.work_order_edit import (
    EditIntent,
    edit_work_order,
    work_order_history,
    work_order_overview,
)
from apps.api.work_orders import RegistrationConflict, RegistrationNotFound, WorkOrdersUnavailable
from contracts.models import Identifier


def mount_work_order_edit_routes(app: FastAPI, auth: UserAuth, *, session_cookie="hmi_session"):
    async def mapped(operation):
        try:
            return await run_in_threadpool(operation)
        except RegistrationConflict as error:
            raise HTTPException(409, f"work-order edit conflict: {error}") from None
        except RegistrationNotFound:
            raise HTTPException(404, "work order not found") from None
        except WorkOrdersUnavailable:
            raise HTTPException(503, "work orders unavailable") from None

    def query(request, name):
        if set(request.query_params) != {name} or len(request.query_params.getlist(name)) != 1:
            raise HTTPException(422, f"exactly one {name} is required")
        try:
            return TypeAdapter(Identifier).validate_python(request.query_params[name])
        except ValidationError:
            raise HTTPException(422, f"invalid {name}") from None

    @app.post("/api/v1/work-order-actions/edit")
    async def edit(request: Request):
        work_order_id = query(request, "work_order_id")
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "JSON is required")
        if (
            len(request.headers.getlist("origin")) != 1
            or len(request.headers.getlist("x-csrf-token")) != 1
        ):
            raise AuthorizationError("edit mutation protection required")
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, "edit request too large")
        try:
            definition = EditIntent.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "invalid edit intent") from None

        def write():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie),
                permission="production_manage",
                csrf_token=request.headers.get("x-csrf-token"),
                origin=request.headers.get("origin"),
            ) as tx:
                return edit_work_order(
                    tx.connection,
                    work_order_id,
                    definition,
                    principal=tx.principal,
                    clock=auth._now,
                )

        return await mapped(write)

    async def read(request, name, operation):
        identity = query(request, name)

        def perform():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return operation(tx.connection, identity, tx.principal)

        return JSONResponse(await mapped(perform), headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/work-order-overview")
    async def overview(request: Request):
        return await read(
            request,
            "line_id",
            lambda db, line, principal: work_order_overview(db, line_id=line, principal=principal),
        )

    @app.get("/api/v1/work-order-history")
    async def history(request: Request):
        return await read(
            request,
            "work_order_id",
            lambda db, identity, principal: work_order_history(db, identity, principal=principal),
        )
