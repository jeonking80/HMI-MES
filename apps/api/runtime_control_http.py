"""Read the plant runtime on/off state; equipment admins request start or stop."""

import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from apps.api.auth import AuthorizationError, UserAuth
from apps.api.runtime_control import RuntimeControlUnavailable, request, view

PATH = "/api/v1/runtime-control"


def mount_runtime_control_routes(
    app: FastAPI, auth: UserAuth, *, session_cookie: str = "hmi_session"
) -> None:
    async def mapped(operation):
        try:
            result = await run_in_threadpool(operation)
        except RuntimeControlUnavailable:
            raise HTTPException(404, "runtime control not installed") from None
        except SQLAlchemyError:
            raise HTTPException(503, "runtime control unavailable") from None
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get(PATH)
    async def read(http: Request):
        if http.query_params:
            raise HTTPException(422, "no query parameters")

        def operation():
            with auth.authorized_transaction(
                http.cookies.get(session_cookie), permission="read", refresh_idle=False
            ) as tx:
                return view(tx.connection, auth._now())

        return await mapped(operation)

    @app.post(PATH)
    async def change(http: Request):
        if http.query_params:
            raise HTTPException(422, "no query parameters")
        if http.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
            "application/json"
        ):
            raise HTTPException(415, "JSON is required")
        if (
            len(http.headers.getlist("origin")) != 1
            or len(http.headers.getlist("x-csrf-token")) != 1
        ):
            raise AuthorizationError("runtime control mutation protection required")
        raw = await http.body()
        if len(raw) > 256:
            raise HTTPException(413, "runtime control request too large")
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(422, "invalid runtime control request") from None
        if (
            not isinstance(body, dict)
            or set(body) != {"action"}
            or body["action"]
            not in {
                "start",
                "stop",
            }
        ):
            raise HTTPException(422, "invalid runtime control request")

        def operation():
            with auth.authorized_transaction(
                http.cookies.get(session_cookie),
                permission="equipment_manage",
                csrf_token=http.headers.get("x-csrf-token"),
                origin=http.headers.get("origin"),
            ) as tx:
                return request(
                    tx.connection, body["action"], actor=tx.principal.login, now=auth._now()
                )

        return await mapped(operation)
