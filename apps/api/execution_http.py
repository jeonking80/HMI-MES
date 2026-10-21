"""Authenticated passive exact-scope execution evidence endpoint."""

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from apps.api.auth import UserAuth
from apps.api.execution_read import ExecutionLookup, ExecutionNotFound, read_execution
from apps.api.work_orders import WorkOrdersUnavailable


def mount_execution_routes(app: FastAPI, auth: UserAuth, *, session_cookie="hmi_session") -> None:
    async def read(request: Request, execution_id: str | None = None):
        required = {"line_id", "source_id", "stream_id"}
        if execution_id is None:
            required.add("execution_id")
        if set(request.query_params) != required or any(
            len(request.query_params.getlist(key)) != 1 for key in required
        ):
            raise HTTPException(422, "exact execution scope is required")
        try:
            values = dict(request.query_params)
            if execution_id is not None:
                values["execution_id"] = execution_id
            lookup = ExecutionLookup.model_validate(values)
        except ValidationError:
            raise HTTPException(422, "invalid execution identity or scope") from None

        def perform():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return read_execution(tx.connection, lookup)

        try:
            return await run_in_threadpool(perform)
        except ExecutionNotFound:
            raise HTTPException(404, "execution not found in exact scope") from None
        except WorkOrdersUnavailable:
            raise HTTPException(503, "execution evidence unavailable") from None

    @app.get("/api/v1/executions/{execution_id}")
    async def execution(execution_id: str, request: Request):
        return await read(request, execution_id)

    @app.get("/api/v1/execution-record")
    async def record(request: Request):
        return await read(request)
