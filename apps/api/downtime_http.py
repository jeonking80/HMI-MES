"""Authenticated downtime annotation and passive immutable history routes."""

from fastapi import FastAPI, HTTPException, Request
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from apps.api.auth import AuthorizationError, UserAuth
from apps.api.downtime import (
    DowntimeConflict,
    DowntimeIntent,
    DowntimeUnknown,
    annotate_downtime,
    read_downtime_history,
)
from contracts.models import Identifier, UTCTimestamp


def register_downtime_routes(app: FastAPI, auth: UserAuth, *, session_cookie="hmi_session"):
    async def mapped(operation):
        try:
            body = await run_in_threadpool(operation)
        except DowntimeUnknown:
            raise HTTPException(422, "unknown segment cannot receive a stop reason") from None
        except DowntimeConflict:
            raise HTTPException(
                409, "downtime annotation conflicts with original intent or segment"
            ) from None
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/downtime-reasons")
    async def write(request: Request):
        if request.query_params:
            raise HTTPException(422, "unexpected query parameters")
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "JSON is required")
        if (
            len(request.headers.getlist("origin")) != 1
            or len(request.headers.getlist("x-csrf-token")) != 1
        ):
            raise AuthorizationError("mutation protection required")
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, "request too large")
        try:
            definition = DowntimeIntent.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "invalid downtime annotation") from None

        def operation():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie),
                permission="downtime_annotate",
                csrf_token=request.headers.get("x-csrf-token"),
                origin=request.headers.get("origin"),
            ) as tx:
                return annotate_downtime(
                    tx.connection, definition, principal=tx.principal, now=auth._now()
                )

        return await mapped(operation)

    @app.get("/api/v1/downtime-reasons/history")
    async def history(request: Request):
        names = {"execution_id", "segment_from"}
        if set(request.query_params) != names or any(
            len(request.query_params.getlist(n)) != 1 for n in names
        ):
            raise HTTPException(422, "invalid history query")
        try:
            execution = TypeAdapter(Identifier).validate_python(
                request.query_params["execution_id"]
            )
            start = TypeAdapter(UTCTimestamp).validate_python(request.query_params["segment_from"])
        except ValidationError:
            raise HTTPException(422, "invalid history query") from None

        def operation():
            with auth.authorized_transaction(
                request.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return read_downtime_history(tx.connection, execution, start)

        return await mapped(operation)
